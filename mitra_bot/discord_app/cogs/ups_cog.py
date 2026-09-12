# mitra_bot/discord_app/cogs/ups_cog.py

import re
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.discord_app.server_target import resolve_server
from mitra_bot.discord_app.node_commands import node_operation
from mitra_bot.services.peer_service import PeerError
from mitra_bot.services.ups.tripplite_client import TrippliteUPSClient
from mitra_bot.services.ups.ups_log import UPSLogStore
from mitra_bot.services.ups.ups_graph import build_ups_status_graph
from mitra_bot.services.ups.ups_service import UPSConfig, UPSService
from mitra_bot.storage.storage_store import get_ups_config, set_ups_config


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


def _parse_duration_to_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None

    # Numeric (seconds)
    s_int = _safe_int(value)
    if s_int is not None:
        return s_int

    if not isinstance(value, str):
        return None

    text = value.strip().lower()
    if not text:
        return None

    # hh:mm:ss or mm:ss
    if ":" in text:
        parts = [p.strip() for p in text.split(":")]
        if all(p.isdigit() for p in parts):
            nums = [int(p) for p in parts]
            if len(nums) == 2:
                return nums[0] * 60 + nums[1]
            if len(nums) == 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]

    # Text with units, e.g. "12 minutes", "1h 5m 10s"
    total = 0
    matched = False
    for num_s, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text):
        try:
            num = float(num_s)
        except Exception:
            continue

        if unit.startswith("h"):
            total += int(num * 3600)
            matched = True
        elif unit.startswith("m"):
            total += int(num * 60)
            matched = True
        elif unit.startswith("s"):
            total += int(num)
            matched = True

    if matched:
        return total

    # Last resort: first number interpreted as seconds
    m = re.search(r"\d+(?:\.\d+)?", text)
    if m:
        try:
            return int(float(m.group(0)))
        except Exception:
            return None

    return None


def _find_runtime_value(obj: Any) -> Optional[Any]:
    runtime_key_variants = {
        "time_to_empty_seconds",
        "time_to_empty_s",
        "time_to_empty",
        "time to empty",
    }

    def _norm(key: str) -> str:
        return " ".join(key.strip().lower().replace("_", " ").split())

    want = {_norm(k) for k in runtime_key_variants}

    def _walk(node: Any) -> Optional[Any]:
        if isinstance(node, dict):
            for k, v in node.items():
                if _norm(str(k)) in want:
                    return v
            for v in node.values():
                found = _walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item)
                if found is not None:
                    return found
        return None

    return _walk(obj)


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
            log_file=str(ups_cfg.get("log_file", "ups_stats.db")),
            database_file=ups_cfg.get("database_file"),
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
        self.log_store.configure(log_file=str(ups_cfg.get("log_file", "ups_stats.db")),database_file=ups_cfg.get("database_file"))
        self.log_store.timezone_name = str(ups_cfg.get("timezone", "UTC"))

        # Update service config
        self.service.config = self._build_service_config(ups_cfg)
        return ups_cfg

    def apply_settings(self, payload):
        if set(payload) == {"enabled"} and type(payload["enabled"]) is bool:
            message = f"UPS monitoring {'enabled' if payload['enabled'] else 'disabled'}."
        elif set(payload) == {"timezone"} and isinstance(payload["timezone"], str):
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(payload["timezone"])
            except (ValueError, ZoneInfoNotFoundError):
                raise PeerError("Invalid timezone. Use an IANA name like UTC or America/Los_Angeles.")
            message = f"UPS timezone set to `{payload['timezone']}`."
        else:
            raise PeerError("Invalid UPS settings")
        set_ups_config(payload)
        self._reload_from_cache()
        return {"message": message}

    async def _save_settings(self, ctx, server, payload):
        await ctx.defer(ephemeral=True)
        try:
            target = resolve_server(self.bot, server)
            result = await node_operation(self.bot, target, "ups_settings", payload)
            message = f"**{target}**: {result['message']}"
        except PeerError as exc:
            message = f"UPS setting could not be confirmed: {exc} Check the selected node before retrying."
        await ctx.respond(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @ups.command(name="monitoring", description="Enable or disable UPS monitoring")
    async def monitoring(
        self,
        ctx: discord.ApplicationContext,
        enabled: bool = discord.Option(
            bool,
            description="Set true to enable monitoring, false to disable.",
            required=True,
        ),
        server: str = discord.Option(str, description="Server ID (default: configured state owner)", required=False, default=None),
    ):
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await self._save_settings(ctx, server, {"enabled": bool(enabled)})

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
        server: str = discord.Option(str, description="Server ID (default: configured state owner)", required=False, default=None),
    ):
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
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

        await self._save_settings(ctx, server, {"timezone": tz})

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
        server: str = discord.Option(str, description="Server ID from /servers list (default: configured state owner)", required=False, default=None),
    ):
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.defer(ephemeral=True)

        try:
            target = resolve_server(self.bot, server)
        except PeerError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return
        mesh = getattr(self.bot, "peer_service", None)
        if mesh is not None and target != mesh.config.node_id:
            await self._remote_status(ctx, mesh, target, hours)
            return

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
            live = await asyncio.to_thread(self.client.get_status)
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

        # time-to-empty: support all known key variants and string formats
        tte: Optional[int] = None
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
            tte = _parse_duration_to_seconds(_get_nested(live, key, None))
            if tte is not None:
                break

        if tte is None:
            tte = _parse_duration_to_seconds(_find_runtime_value(live))

        # Fallback to latest log sample if live snapshot does not contain runtime
        if tte is None and recent_rows:
            last = recent_rows[-1]
            for key in (
                "time_to_empty_seconds",
                "time_to_empty_s",
                "time_to_empty",
                "time to empty",
            ):
                tte = _parse_duration_to_seconds(last.get(key))
                if tte is not None:
                    break
            if tte is None:
                tte = _parse_duration_to_seconds(_find_runtime_value(last))

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
            title=f"UPS Status — {target}",
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
            embed.set_image(url="attachment://ups_status.png")
            await ctx.respond(embed=embed, file=file, ephemeral=True)
        else:
            await ctx.respond(
                embed=embed,
                content="No graph data available yet.",
                ephemeral=True,
            )

    def peer_snapshot(self, node_id: str) -> dict:
        cfg = self._reload_from_cache()
        live = {}
        if self.client.available:
            try:
                live = self.client.get_status()
            except Exception:
                pass
        return dict(
            node_id=node_id, captured_at=int(time.time()), available=self.client.available,
            live=live if isinstance(live, dict) else {},
            rows=self.log_store.get_recent(hours=168)[-5000:],
            timezone=str(cfg.get("timezone", "UTC")), enabled=bool(cfg.get("enabled", True)),
            poll_seconds=int(cfg.get("poll_seconds", 30)),
        )

    async def _remote_status(self, ctx, mesh, target, hours):
        try:
            snapshot, stale = await mesh.snapshot(target)
        except PeerError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return
        window = int(hours or 6)
        # An offline graph is relative to capture time, so old data stays visible.
        end = datetime.fromtimestamp(snapshot["captured_at"], tz=timezone.utc)
        cutoff = end - timedelta(hours=window)
        rows = []
        for row in snapshot["rows"]:
            try:
                ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
                if cutoff <= ts <= end:
                    rows.append(row)
            except (ValueError, TypeError):
                continue
        live = snapshot["live"]
        embed = discord.Embed(
            title=f"UPS Status — {target}",
            description=("**OFFLINE / UNREACHABLE — cached data**" if stale else "Live peer response")
                        + f"\nCaptured <t:{snapshot['captured_at']}:F>",
            color=discord.Color.orange() if stale else discord.Color.green(),
        )
        for name, key in (("On Battery", "on_battery"), ("Battery (%)", "battery_percent"), ("Health", "health")):
            embed.add_field(name=name, value=str(live.get(key, "Unknown"))[:1024])
        embed.add_field(name="UPS support", value="Available" if snapshot["available"] else "Unavailable")
        embed.set_footer(text=f"{len(rows)} samples | Last {window}h before capture | {snapshot['timezone']}")
        graph = build_ups_status_graph(rows, hours=window, timezone_name=snapshot["timezone"])
        if graph:
            embed.set_image(url="attachment://ups_status.png")
            await ctx.respond(embed=embed, file=discord.File(graph, filename="ups_status.png"), ephemeral=True)
        else:
            await ctx.respond(embed=embed, content="No graph data available." if not rows else None, ephemeral=True)

    def poll_for_event(self):
        """
        Called by the UPS monitor background loop.
        """
        self._reload_from_cache()
        return self.service.poll()
