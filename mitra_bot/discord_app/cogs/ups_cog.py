# mitra_bot/discord_app/cogs/ups_cog.py
from __future__ import annotations

from typing import Any, Dict, Optional

import discord
from discord.ext import commands
from pathlib import Path

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.services.ups.tripplite_client import TrippliteUPSClient
from mitra_bot.services.ups.ups_log import UPSLogStore
from mitra_bot.services.ups.ups_graph import build_ups_status_graph
from mitra_bot.services.ups.ups_service import UPSConfig, UPSService
from mitra_bot.storage.cache_store import get_ups_config, set_ups_config


def _fmt_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "Unknown"
    try:
        s = int(seconds)
    except Exception:
        return "Unknown"

    if s < 0:
        s = 0

    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60

    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _get_nested(d: Dict[str, Any], path: str, default=None):
    """
    Read either nested dicts or flattened keys.
    Example paths:
      "status.ac present"
      "input.voltage"
      "output.power"
    Also tolerates old keys like status['ac present'].
    """
    if not isinstance(d, dict):
        return default

    # Flattened key support: "status.ac present" or "input.voltage"
    if path in d:
        return d.get(path, default)

    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default

        # Try exact key
        if part in cur:
            cur = cur[part]
            continue

        # For the status dict, keys often include spaces (old format)
        # Example: "ac present" is a key under status
        # We already pass "status.ac present" which splits into "status","ac present"
        # so this branch is mostly for robustness.
        found = False
        for k in cur.keys():
            if str(k).strip().lower() == part.strip().lower():
                cur = cur[k]
                found = True
                break
        if not found:
            return default

    return cur


class UPSCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

        ups_cfg = get_ups_config()

        self.client = TrippliteUPSClient()
        self.log_store = UPSLogStore(
            log_file=str(ups_cfg.get("log_file", "ups_stats.jsonl")),
            timezone_name=str(ups_cfg.get("timezone", "UTC")),
            history_limit=int(ups_cfg.get("history_limit", 5000)),
        )

        self.log_store.preload_recent(hours=24)

        self.service = UPSService(
            client=self.client,
            log_store=self.log_store,
            config=self._build_service_config(ups_cfg),
        )

    ups = discord.SlashCommandGroup(
        name="ups",
        description="UPS monitoring controls",
    )

    def _build_service_config(self, ups_cfg: Dict[str, Any]) -> UPSConfig:
        return UPSConfig(
            enabled=bool(ups_cfg.get("enabled", True)),
            warn_time_to_empty_seconds=int(
                ups_cfg.get("warn_time_to_empty_seconds", 600)
            ),
            critical_time_to_empty_seconds=int(
                ups_cfg.get("critical_time_to_empty_seconds", 180)
            ),
            auto_shutdown_enabled=bool(ups_cfg.get("auto_shutdown_enabled", False)),
            auto_shutdown_action=str(ups_cfg.get("auto_shutdown_action", "shutdown")),
            auto_shutdown_delay_seconds=int(
                ups_cfg.get("auto_shutdown_delay_seconds", 0)
            ),
            auto_shutdown_force=bool(ups_cfg.get("auto_shutdown_force", False)),
        )

    def _reload_from_cache(self) -> Dict[str, Any]:
        ups_cfg = get_ups_config()

        # Update log store configuration
        self.log_store.log_path = (
            Path(str(ups_cfg.get("log_file", "ups_stats.jsonl"))).expanduser().resolve()
        )
        self.log_store.timezone_name = str(ups_cfg.get("timezone", "UTC"))

        # Update service config
        self.service.config = self._build_service_config(ups_cfg)
        return ups_cfg

    @ups.command(name="enable", description="Enable UPS monitoring")
    async def enable(self, ctx: discord.ApplicationContext):
        if ensure_admin(ctx):
            return

        set_ups_config({"enabled": True})
        self._reload_from_cache()
        await ctx.respond("UPS monitoring enabled.", ephemeral=True)

    @ups.command(name="disable", description="Disable UPS monitoring")
    async def disable(self, ctx: discord.ApplicationContext):
        if ensure_admin(ctx):
            return

        set_ups_config({"enabled": False})
        self._reload_from_cache()
        await ctx.respond("UPS monitoring disabled.", ephemeral=True)

    @ups.command(
        name="timezone", description="Set the timezone for UPS graph timestamps"
    )
    async def timezone(
        self,
        ctx: discord.ApplicationContext,
        tz: str = discord.Option(
            str,
            description="IANA timezone (examples: UTC, America/Los_Angeles, Europe/Berlin)",
            required=True,
        ),
    ):
        if ensure_admin(ctx):
            return

        # Validate timezone string
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
        except Exception:
            await ctx.respond(
                "Invalid timezone. Use an IANA name like `UTC` or `America/Los_Angeles`.",
                ephemeral=True,
            )
            return

        set_ups_config({"timezone": tz})
        self._reload_from_cache()

        await ctx.respond(f"UPS timezone set to `{tz}`.", ephemeral=True)

    @ups.command(name="status", description="Show UPS status and a recent graph")
    async def status(
        self,
        ctx: discord.ApplicationContext,
        hours: Optional[int] = discord.Option(
            int,
            description="How many hours of history to graph (default from settings).",
            required=False,
            default=None,
            min_value=1,
            max_value=168,
        ),
    ):
        if ensure_admin(ctx):
            return

        await ctx.defer(ephemeral=True)

        ups_cfg = self._reload_from_cache()
        tz_name = str(ups_cfg.get("timezone", "UTC"))
        window_hours = int(hours or ups_cfg.get("graph_default_hours", 6))

        if not self.client.available:
            await ctx.respond(
                "UPS monitoring is unavailable (tripplite not installed).",
                ephemeral=True,
            )
            return

        # Get a live snapshot for rich stats (this is what your old command did)
        try:
            live = self.client.get_status()
        except Exception:
            live = {}

        # Graph from recent log (also used as fallback for a few values)
        recent_rows = self.log_store.get_recent(hours=window_hours)

        # If we do not have enough points, automatically widen the window.
        if len(recent_rows) < 2 and window_hours < 24:
            recent_rows = self.log_store.get_recent(hours=24)
            window_hours = 24

        # Pull values from either schema (new/old)
        status = _get_nested(live, "status", {}) or {}

        on_battery = bool(_get_nested(live, "on_battery", False))
        batt_percent = _get_nested(live, "battery_percent", None)
        health = _get_nested(live, "health", None)

        # time-to-empty: support all known key variants
        tte = None
        for key in (
            "time_to_empty_seconds",
            "time_to_empty_s",
            "time_to_empty",
            "time to empty",
            "status.time_to_empty_seconds",
            "status.time_to_empty_s",
            "status.time_to_empty",
            "status.time to empty",
        ):
            tte = _safe_int(_get_nested(live, key, None))
            if tte is not None:
                break

        # Fallback to latest log sample if live snapshot does not contain runtime
        if tte is None and recent_rows:
            last = recent_rows[-1]
            for key in ("time_to_empty_seconds", "time_to_empty_s", "time_to_empty", "time to empty"):
                tte = _safe_int(last.get(key))
                if tte is not None:
                    break

        # Input/output details
        in_v = _get_nested(live, "input_voltage", None)
        if in_v is None:
            in_v = _get_nested(live, "input.voltage", None)
        in_hz = _get_nested(live, "input.frequency", None)

        out_v = _get_nested(live, "output.voltage", None)
        out_w = _get_nested(live, "output.power", None)

        # Old status flags
        def _flag(key: str):
            return status.get(key) if isinstance(status, dict) else None

        ac_present = _flag("ac present")
        charging = _flag("charging")
        discharging = _flag("discharging")
        fully_charged = _flag("fully charged")
        needs_replacement = _flag("needs replacement")
        shutdown_imminent = _flag("shutdown imminent")

        color = discord.Color.orange() if on_battery else discord.Color.green()
        embed = discord.Embed(
            title="UPS Status",
            description=f"Monitoring: `enabled={ups_cfg.get('enabled', True)}` | `poll={ups_cfg.get('poll_seconds', 30)}s`",
            color=color,
        )
        embed.add_field(name="On Battery", value=f"`{on_battery}`", inline=True)
        embed.add_field(name="Battery", value=f"`{batt_percent}%`" if batt_percent is not None else "`Unknown`", inline=True)
        embed.add_field(name="Time To Empty", value=f"`{_fmt_seconds(tte)}`", inline=True)
        embed.add_field(name="Health", value=f"`{health if health is not None else 'Unknown'}`", inline=True)
        embed.add_field(
            name="Input",
            value=f"`V={in_v if in_v is not None else 'Unknown'} Hz={in_hz if in_hz is not None else 'Unknown'}`",
            inline=True,
        )
        embed.add_field(
            name="Output",
            value=f"`V={out_v if out_v is not None else 'Unknown'} W={out_w if out_w is not None else 'Unknown'}`",
            inline=True,
        )

        flags = []
        if ac_present is not None:
            flags.append(f"AC present=`{ac_present}`")
        if charging is not None:
            flags.append(f"Charging=`{charging}`")
        if discharging is not None:
            flags.append(f"Discharging=`{discharging}`")
        if fully_charged is not None:
            flags.append(f"Fully charged=`{fully_charged}`")
        if needs_replacement is not None:
            flags.append(f"Needs replacement=`{needs_replacement}`")
        if shutdown_imminent is not None:
            flags.append(f"Shutdown imminent=`{shutdown_imminent}`")
        if flags:
            embed.add_field(name="Flags", value="\n".join(flags), inline=False)

        embed.set_footer(text=f"Window: last {window_hours}h | Timezone: {tz_name}")

        graph = build_ups_status_graph(
            recent_rows,
            hours=window_hours,
            timezone_name=tz_name,
        )

        if graph:
            file = discord.File(graph, filename="ups_status.png")
            await ctx.respond(file=file, embed=embed, ephemeral=True)
        else:
            await ctx.respond(
                embed=embed,
                content="No graph data available yet.",
                ephemeral=True,
            )

    def poll_for_event(self):
        """
        Called by the UPS monitor background loop.
        """
        self._reload_from_cache()
        return self.service.poll()
