import os
import platform
import asyncio
import subprocess
import signal
import sys
import json
import logging
import hashlib
import time
from typing import Optional, Tuple, List, Set, Dict, Any
from datetime import datetime, timezone
from collections import deque
from zoneinfo import ZoneInfo
from datetime import timedelta

import requests
import aiohttp

import discord
from discord.ext import commands, tasks

# ---- Optional dependency: tripplite ----
try:
    from tripplite import Battery
    TRIPPLITE_AVAILABLE = True
except Exception:
    Battery = None  # type: ignore
    TRIPPLITE_AVAILABLE = False

# ---- Matplotlib (headless) ----
import matplotlib
matplotlib.use("Agg")  # headless-safe
import matplotlib.pyplot as plt  # noqa: E402
from io import BytesIO  # noqa: E402
import matplotlib.dates as mdates

MITRA_ADMIN_ROLE_NAME = "Mitra Admins"
MITRA_ADMIN_GATE_PERMS = discord.Permissions(manage_messages=True)  # choose your gate

# Pycord supports slash commands via @bot.slash_command
bot = commands.Bot(command_prefix='$', intents=discord.Intents.default())


class LogColors:
    GRAY = "\x1b[90m"
    BRIGHT_BLUE = "\x1b[94m"
    YELLOW = "\x1b[33;1m"
    RED = "\x1b[31;1m"
    MAGENTA = "\x1b[35m"
    RESET = "\x1b[0m"
    CYAN = "\x1b[36;1m"


class CustomFormatter(logging.Formatter):
    FORMAT = "[" + LogColors.GRAY + "%(asctime)s" + LogColors.RESET + "] [%(levelname)-8s" + LogColors.RESET + "] %(name)s" + LogColors.RESET + ": %(message)s"

    COLOR_FORMAT = {
        logging.DEBUG: FORMAT.replace("%(levelname)-8s", LogColors.CYAN + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.INFO: FORMAT.replace("%(levelname)-8s", LogColors.BRIGHT_BLUE + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.WARNING: FORMAT.replace("%(levelname)-8s", LogColors.YELLOW + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.ERROR: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.CRITICAL: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s")
    }

    def format(self, record):
        log_fmt = self.COLOR_FORMAT.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


async def ensure_admin_role_and_sync(guild: discord.Guild) -> Optional[discord.Role]:
    """Ensure Mitra Admins role exists and matches cache.json admins allow-list."""
    if not guild.me or not guild.me.guild_permissions.manage_roles:
        logging.warning(f"Missing Manage Roles in guild '{guild.name}' ({guild.id}); cannot auto-manage admin role.")
        return None

    # Find or create the role
    role = discord.utils.get(guild.roles, name=MITRA_ADMIN_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(
                name=MITRA_ADMIN_ROLE_NAME,
                permissions=MITRA_ADMIN_GATE_PERMS,
                reason="Mitra bot: create admin role for command visibility gating",
            )
            logging.info(f"Created role '{MITRA_ADMIN_ROLE_NAME}' in guild '{guild.name}'.")
        except Exception as ex:
            logging.warning(f"Failed to create admin role in guild '{guild.name}': {ex}")
            return None

    # Update permissions if changed
    try:
        if role.permissions != MITRA_ADMIN_GATE_PERMS:
            await role.edit(permissions=MITRA_ADMIN_GATE_PERMS, reason="Mitra bot: keep admin role perms in sync")
    except Exception as ex:
        logging.warning(f"Failed to update admin role permissions in guild '{guild.name}': {ex}")

    # Sync membership from cache.json allow-list
    try:
        for user_id in list(admins):
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if member and role not in member.roles:
                await member.add_roles(role, reason="Mitra bot: add admin role from allow-list")
        # Optional: remove role from members not in allow-list
        for member in role.members:
            if member.id not in admins:
                await member.remove_roles(role, reason="Mitra bot: remove admin role (not in allow-list)")
    except Exception as ex:
        logging.warning(f"Failed syncing admin role members in guild '{guild.name}': {ex}")

    return role


def signal_handler(signum, frame):
    logging.info("Application received a signal to close.")
    sys.exit(0)


def get_valid_token() -> str:
    while True:
        token = input("Please enter the bot token (72 characters): ").strip()
        if len(token) == 72:
            return token
        logging.warning("Invalid input. The bot token must be exactly 72 characters.")


def get_valid_channel_id() -> int:
    while True:
        channel_id = input("Please enter the channel ID (18 digits): ").strip()
        if channel_id.isdigit() and len(channel_id) == 18:
            return int(channel_id)
        logging.warning("Invalid input. The channel ID must be exactly 18 digits.")


def _read_cache_json() -> Dict[str, Any]:
    try:
        with open("cache.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache_json(data: Dict[str, Any]) -> None:
    with open("cache.json", "w") as f:
        json.dump(data, f)


def _ensure_ups_config(data: Dict[str, Any]) -> Dict[str, Any]:
    ups = data.get("ups")
    if not isinstance(ups, dict):
        ups = {}

    ups.setdefault("enabled", True)
    ups.setdefault("poll_seconds", 30)

    # Alert thresholds (seconds remaining)
    ups.setdefault("warn_time_to_empty_seconds", 600)      # 10 minutes
    ups.setdefault("critical_time_to_empty_seconds", 180)  # 3 minutes

    # Optional automatic shutdown control (OFF by default)
    ups.setdefault("auto_shutdown_enabled", False)
    ups.setdefault("auto_shutdown_action", "shutdown")     # shutdown or restart
    ups.setdefault("auto_shutdown_delay_seconds", 0)
    ups.setdefault("auto_shutdown_force", False)

    # Logging
    ups.setdefault("log_enabled", True)
    ups.setdefault("log_file", "ups_stats.jsonl")
    ups.setdefault("graph_default_hours", 6)               # /ups status default window

    # Timezone for timestamps in logs and graphs (default: UTC)
    ups.setdefault("timezone", "UTC")  # e.g. "America/Los_Angeles"

    data["ups"] = ups
    return data


def load_connection_data() -> Tuple[str, int, str, str, str, List[str]]:
    try:
        data = {}
        logging.info("Loading cache file...")
        try:
            with open("cache.json", "r") as file:
                data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            logging.warning("Cache file not found or invalid. Creating cache file...")

        token = data.get("token", "")
        channel = data.get("channel", 0)
        api_key = data.get("api_key", "")
        email = data.get("email", "")
        zone_id = data.get("zone_id", "")
        record_ids = data.get("record_ids", [])

        if not token or len(token) != 72:
            logging.warning("Invalid token in cache file, please enter a valid token.")
            token = get_valid_token()
            data["token"] = token

        if not channel or not (isinstance(channel, int) and len(str(channel)) == 18):
            logging.warning("Invalid channel ID in cache file, please enter a valid channel ID.")
            channel = get_valid_channel_id()
            data["channel"] = channel

        if not api_key:
            logging.warning("Invalid API key in cache file, please enter a valid API key.")
            api_key = input("Please enter your Cloudflare API key: ").strip()
            data["api_key"] = api_key

        if not email:
            logging.warning("Invalid email in cache file, please enter a valid email.")
            email = input("Please enter your Cloudflare email: ").strip()
            data["email"] = email

        if not zone_id:
            logging.warning("Invalid zone ID in cache file, please enter a valid zone ID.")
            zones = get_zones(api_key, email)
            if zones:
                for i, zone in enumerate(zones):
                    print(f"{i}: {zone['name']}")
                zone_index = int(input("Select a zone by entering the corresponding number: "))
                selected_zone = zones[zone_index]
                zone_id = selected_zone["id"]
                data["zone_id"] = zone_id

        if len(record_ids) == 0:
            logging.warning("Invalid record IDs in cache file, please enter valid record IDs.")
            records = get_dns_records(api_key, email, zone_id)
            record_ids_temp: List[str] = []
            if records:
                while True:
                    for i, record in enumerate(records):
                        print(f"{i}: {record['type']} {record['name']} -> {record['content']}")
                    record_index = int(input("Select a record to update by entering the corresponding number (or -1 to finish): "))
                    if record_index == -1:
                        break
                    record_ids_temp.append(records[record_index]["id"])
                record_ids = record_ids_temp
                data["record_ids"] = record_ids

        existing = _read_cache_json()
        existing.update(data)
        existing = _ensure_ups_config(existing)
        _write_cache_json(existing)

        logging.info("Cache file loaded")
        return token, channel, api_key, email, zone_id, record_ids
    except ValueError:
        logging.error("Failed to load cache file.")
        raise


async def get_ip() -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.ipify.org?format=json") as resp:
            resp.raise_for_status()
            payload = await resp.json()
            return payload["ip"]


async def load_ip() -> Optional[str]:
    try:
        with open("cache.json", "r") as file:
            data = json.load(file)
            return data.get("ip")
    except FileNotFoundError:
        return None


async def save_ip(ip: str) -> None:
    data = _read_cache_json()
    data["ip"] = ip
    _write_cache_json(data)
    logging.info(f"IP address saved to cache file: {ip}")


async def save_subscribers(subscribers_set: Set[int]) -> None:
    data = _read_cache_json()
    data["subscribers"] = list(subscribers_set)
    _write_cache_json(data)
    logging.info(f"Subscribers saved to cache file: {', '.join([str(s) for s in subscribers_set])}")


def load_subscribers() -> Set[int]:
    try:
        logging.info("Loading subscribers from cache file...")
        with open("cache.json", "r") as file:
            data = json.load(file)
            if "subscribers" not in data:
                logging.warning("No subscribers found in cache file.")
                return set()
            return set(data.get("subscribers", []))
    except FileNotFoundError:
        logging.error("Cache file not found.")
        return set()


def get_zones(api_key: str, email: str):
    url = "https://api.cloudflare.com/client/v4/zones"
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["result"]
    print("Failed to retrieve zones.")
    return None


def get_dns_records(api_key: str, email: str, zone_id: str):
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["result"]
    print("Failed to retrieve DNS records.")
    return None


async def update_dns_record(api_key: str, email: str, zone_id: str, record_id: str, ip: str):
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        logging.error(f"Failed to retrieve DNS record {record_id}.")
        return
    record = response.json()["result"]
    record["content"] = ip
    response = requests.put(url, headers=headers, json=record)
    if response.status_code == 200:
        logging.info(f"DNS record {record['name']} updated successfully!")
    else:
        logging.error(f"Failed to update DNS record {record['name']}.")


def _commands_fingerprint() -> str:
    payloads = []
    for cmd in bot.application_commands:
        try:
            d = cmd.to_dict()
        except Exception:
            d = {
                "name": getattr(cmd, "name", ""),
                "description": getattr(cmd, "description", ""),
                "type": getattr(cmd, "type", None),
            }
        payloads.append(d)

    payloads.sort(key=lambda x: (x.get("type", 1), x.get("name", "")))
    raw = json.dumps(payloads, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_admins() -> Set[int]:
    data = _read_cache_json()
    admins_list = data.get("admins", [])
    out: Set[int] = set()
    for x in admins_list:
        try:
            out.add(int(x))
        except Exception:
            pass
    return out


def is_admin_user(user_id: int) -> bool:
    return user_id in admins


def build_power_command(action: str, delay: int, force: bool) -> List[str]:
    system = platform.system().lower()
    action = (action or "").lower().strip()
    delay = max(0, int(delay))

    if system == "windows":
        if action == "cancel":
            return ["shutdown", "/a"]

        if action == "restart":
            args = ["shutdown", "/r"]
        elif action == "shutdown":
            args = ["shutdown", "/s"]
        else:
            raise RuntimeError(f"Unsupported power action: {action}")

        if force:
            args.append("/f")

        args.extend(["/t", str(delay)])
        return args

    if system in ("linux", "darwin"):
        if action == "cancel":
            return ["shutdown", "-c"]

        if action == "restart":
            flag = "-r"
        elif action == "shutdown":
            flag = "-h"
        else:
            raise RuntimeError(f"Unsupported power action: {action}")

        if delay <= 0:
            return ["shutdown", flag, "now"]

        minutes = max(1, (delay + 59) // 60)
        return ["shutdown", flag, f"+{minutes}"]

    raise RuntimeError(f"Unsupported OS: {platform.system()}")


async def execute_power_action(action: str, delay: int, force: bool) -> None:
    args = build_power_command(action, delay, force)
    logging.warning(f"Power action executing. OS={platform.system()} action={action} args={args}")
    subprocess.run(args, check=True)


# ---------------- UPS Monitoring (TrippLite) ----------------

ups_battery = None
ups_last_state: Optional[Dict[str, Any]] = None
ups_last_alert: Dict[str, float] = {}

# Keep some history in-memory for fast graphing (we also log to file)
UPS_HISTORY_MAX_POINTS = 24 * 60 * 2  # up to ~24h at 30s polling
ups_history = deque(maxlen=UPS_HISTORY_MAX_POINTS)  # each element is dict row


def _ups_cfg() -> Dict[str, Any]:
    data = _read_cache_json()
    data = _ensure_ups_config(data)
    _write_cache_json(data)
    return data.get("ups", {})


def _ups_tzinfo() -> timezone:
    cfg = _ups_cfg()
    name = str(cfg.get("timezone", "UTC"))
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _ups_set_enabled(enabled: bool) -> None:
    data = _read_cache_json()
    data = _ensure_ups_config(data)
    data["ups"]["enabled"] = bool(enabled)
    _write_cache_json(data)


def _fmt_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "unknown"
    s = max(0, int(seconds))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _throttle_ok(key: str, min_seconds: int) -> bool:
    now = time.time()
    last = ups_last_alert.get(key, 0.0)
    if now - last >= min_seconds:
        ups_last_alert[key] = now
        return True
    return False


async def _send_ups_alert(message: str) -> None:
    try:
        ch = bot.get_channel(CHANNEL)
        if ch:
            await ch.send(message)
    except Exception as ex:
        logging.warning(f"Failed to send UPS alert to channel: {ex}")

    for subscriber in list(subscribers):
        try:
            user = await bot.fetch_user(subscriber)
            if user:
                await user.send(message)
        except discord.NotFound:
            subscribers.discard(subscriber)
            await save_subscribers(subscribers)
        except Exception:
            pass


def _ups_read_blocking() -> Dict[str, Any]:
    global ups_battery
    if not TRIPPLITE_AVAILABLE:
        raise RuntimeError("tripplite library is not installed.")

    if ups_battery is None:
        ups_battery = Battery()
        ups_battery.open()

    try:
        return ups_battery.get()
    except OSError as e:
        logging.warning(f"UPS read error (OSError). Reopening connection. {e}")
        try:
            ups_battery.close()
        except Exception:
            pass
        ups_battery = Battery()
        ups_battery.open()
        return ups_battery.get()


async def _ups_poll_once() -> Optional[Dict[str, Any]]:
    cfg = _ups_cfg()
    if not cfg.get("enabled", True):
        return None

    if not TRIPPLITE_AVAILABLE:
        return None

    try:
        state = await asyncio.to_thread(_ups_read_blocking)
        return state
    except Exception as ex:
        logging.warning(f"UPS poll failed: {ex}")
        return None


def _ups_get_flag(state: Optional[Dict[str, Any]], key: str) -> Optional[bool]:
    if not state:
        return None
    try:
        return bool(state.get("status", {}).get(key))
    except Exception:
        return None


def _ups_get_time_to_empty(state: Optional[Dict[str, Any]]) -> Optional[int]:
    if not state:
        return None
    try:
        v = state.get("time to empty")
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _log_ups_state(state: Dict[str, Any]) -> None:
    cfg = _ups_cfg()
    if not cfg.get("log_enabled", True):
        return

    path = str(cfg.get("log_file", "ups_stats.jsonl"))
    ts = datetime.now(timezone.utc).isoformat()

    status = state.get("status", {}) or {}
    inp = state.get("input", {}) or {}
    outp = state.get("output", {}) or {}

    row = {
        "ts": ts,
        "ac_present": bool(status.get("ac present")) if "ac present" in status else None,
        "charging": bool(status.get("charging")) if "charging" in status else None,
        "discharging": bool(status.get("discharging")) if "discharging" in status else None,
        "shutdown_imminent": bool(status.get("shutdown imminent")) if "shutdown imminent" in status else None,
        "needs_replacement": bool(status.get("needs replacement")) if "needs replacement" in status else None,
        "health_pct": _safe_float(state.get("health")),
        "time_to_empty_s": _safe_float(state.get("time to empty")),
        "input_v": _safe_float(inp.get("voltage")),
        "input_hz": _safe_float(inp.get("frequency")),
        "output_v": _safe_float(outp.get("voltage")),
        "output_w": _safe_float(outp.get("power")),
    }

    ups_history.append(row)

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception as ex:
        logging.warning(f"Failed to write UPS stats log '{path}': {ex}")


def _parse_ts_to_dt(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Handles: "2026-02-16T21:00:00+00:00" and "...Z"
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _preload_ups_history_from_log(hours: int = 24) -> None:
    cfg = _ups_cfg()
    path = str(cfg.get("log_file", "ups_stats.jsonl"))
    if not os.path.exists(path):
        logging.info(f"UPS log file not found for preload: {path}")
        return

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    loaded = 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    dt = _parse_ts_to_dt(r.get("ts", ""))
                    if dt and dt >= cutoff_dt:
                        ups_history.append(r)
                        loaded += 1
                except Exception:
                    continue
        logging.info(f"Preloaded {loaded} UPS history row(s) from {path} (last {hours}h).")
    except Exception as ex:
        logging.warning(f"Failed to preload UPS history: {ex}")


def _read_ups_log_recent(hours: int) -> List[Dict[str, Any]]:
    cfg = _ups_cfg()
    path = str(cfg.get("log_file", "ups_stats.jsonl"))
    cutoff = datetime.now(timezone.utc).timestamp() - (max(1, int(hours)) * 3600)

    rows: List[Dict[str, Any]] = []

    # Prefer in-memory if it already covers the window.
    try:
        for r in list(ups_history):
            ts = r.get("ts")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.timestamp() >= cutoff:
                rows.append(r)
        if rows:
            return rows
    except Exception:
        rows = []

    # Fallback: read file (tail-ish, but safe)
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts = r.get("ts")
                    if not ts:
                        continue
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.timestamp() >= cutoff:
                        rows.append(r)
                except Exception:
                    continue
    except Exception:
        return []

    return rows


def _build_ups_status_graph(rows: List[Dict[str, Any]], hours: int) -> Optional[BytesIO]:
    if not rows:
        return None

    import math
    import matplotlib.dates as mdates
    from matplotlib.dates import ConciseDateFormatter

    # --- Read timezone from cache.json (default UTC) ---
    tz_name = "UTC"
    try:
        cfg = _ups_cfg()
        tz_name = str(cfg.get("timezone", "UTC")).strip() or "UTC"
    except Exception:
        tz_name = "UTC"

    # --- Resolve tzinfo ---
    tzinfo = None
    try:
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = None  # fallback below

    # ---- Parse timestamps (stored as UTC) -> convert -> strip tz for plotting ----
    def _parse_to_local_naive(ts: str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            if tzinfo is not None:
                dt = dt.astimezone(tzinfo)
            else:
                dt = dt.astimezone(timezone.utc)

            # IMPORTANT: strip tzinfo so matplotlib shows "local wall time"
            return dt.replace(tzinfo=None)
        except Exception:
            return None

    parsed: List[Tuple[datetime, Dict[str, Any]]] = []
    for r in rows:
        dt = _parse_to_local_naive(r.get("ts", ""))
        if dt:
            parsed.append((dt, r))

    if len(parsed) < 2:
        return None

    parsed.sort(key=lambda x: x[0])
    xs = [dt for dt, _ in parsed]

    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return float("nan")

    tte = [_to_float(r.get("time_to_empty_s")) / 60.0 for _, r in parsed]  # minutes
    out_w = [_to_float(r.get("output_w")) for _, r in parsed]
    in_v = [_to_float(r.get("input_v")) for _, r in parsed]

    def _mean_std(series):
        vals = [v for v in series if not math.isnan(v)]
        if len(vals) < 2:
            return None, None
        mean = sum(vals) / len(vals)
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
        return mean, std

    # ---- Discord Dark Theme Colors ----
    FIG_BG = "#2B2D31"
    AX_BG  = "#313338"
    TEXT   = "#DBDEE1"
    GRID   = "#4E5058"
    ACCENT = "#5865F2"

    fig = plt.figure(figsize=(12, 7), dpi=220)
    fig.patch.set_facecolor(FIG_BG)

    ax1 = fig.add_subplot(311)
    ax2 = fig.add_subplot(312, sharex=ax1)
    ax3 = fig.add_subplot(313, sharex=ax1)

    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(AX_BG)
        ax.tick_params(colors=TEXT, labelsize=9)
        ax.yaxis.label.set_color(TEXT)
        ax.xaxis.label.set_color(TEXT)
        ax.grid(True, color=GRID, alpha=0.25, linewidth=0.8)
        for spine in ax.spines.values():
            spine.set_color(GRID)

    def _plot(ax, y, label, units):
        ax.plot(xs, y, linewidth=2.2, color=ACCENT, alpha=0.95)

        mean, std = _mean_std(y)
        if mean is not None:
            ax.axhline(mean, linestyle="--", linewidth=1.2, color=TEXT, alpha=0.55)
            if std and std > 0:
                ax.axhspan(mean - std, mean + std, color=TEXT, alpha=0.08)

        ax.set_ylabel(f"{label}\n({units})", fontsize=10)

        vals = [v for v in y if not math.isnan(v)]
        if len(vals) >= 2:
            vmin, vmax = min(vals), max(vals)
            if vmin == vmax:
                pad = 1.0 if vmin == 0 else abs(vmin) * 0.05
                ax.set_ylim(vmin - pad, vmax + pad)

    _plot(ax1, tte, "Runtime", "min")
    _plot(ax2, out_w, "Output", "W")
    _plot(ax3, in_v,  "Input",  "V")

    # ---- Better time axis formatting ----
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    formatter = ConciseDateFormatter(locator)
    ax3.xaxis.set_major_locator(locator)
    ax3.xaxis.set_major_formatter(formatter)

    for ax in (ax1, ax2):
        plt.setp(ax.get_xticklabels(), visible=False)

    label_tz = tz_name if tzinfo is not None else "UTC"
    ax3.set_xlabel(f"Time ({label_tz}) | last {hours}h", fontsize=10)

    fig.suptitle("Mitra UPS Status", fontsize=14, fontweight="bold", color=TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=FIG_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def _ups_handle_state(state: Dict[str, Any]) -> None:
    global ups_last_state
    cfg = _ups_cfg()

    ac_present = _ups_get_flag(state, "ac present")
    shutdown_imminent = _ups_get_flag(state, "shutdown imminent")
    needs_replacement = _ups_get_flag(state, "needs replacement")
    tte = _ups_get_time_to_empty(state)

    prev = ups_last_state
    prev_ac = _ups_get_flag(prev, "ac present") if prev else None
    prev_shutdown_imminent = _ups_get_flag(prev, "shutdown imminent") if prev else None
    prev_needs_replacement = _ups_get_flag(prev, "needs replacement") if prev else None

    # Log before alerts so your charts always include the newest point
    _log_ups_state(state)

    if ac_present is False and prev_ac is True:
        await _send_ups_alert(
            "⚠️ **UPS Alert: AC power LOST (running on battery).**\n"
            f"Time to empty: **{_fmt_seconds(tte)}**"
        )

    if ac_present is True and prev_ac is False:
        await _send_ups_alert("✅ **UPS Alert: AC power RESTORED (back on mains).**")

    if shutdown_imminent is True and prev_shutdown_imminent is not True:
        await _send_ups_alert(
            "🚨 **UPS Alert: SHUTDOWN IMMINENT.**\n"
            f"Time to empty: **{_fmt_seconds(tte)}**"
        )

    if needs_replacement is True and prev_needs_replacement is not True:
        await _send_ups_alert("🛑 **UPS Alert: Battery reports it NEEDS REPLACEMENT.**")

    warn_t = int(cfg.get("warn_time_to_empty_seconds", 600))
    crit_t = int(cfg.get("critical_time_to_empty_seconds", 180))

    if ac_present is False and tte is not None:
        if tte <= crit_t and _throttle_ok("tte_critical", 120):
            await _send_ups_alert(
                f"🚨 **UPS Critical: low runtime remaining** ({_fmt_seconds(tte)}).\n"
                f"Threshold: {crit_t}s"
            )
        elif tte <= warn_t and _throttle_ok("tte_warn", 300):
            await _send_ups_alert(
                f"⚠️ **UPS Warning: runtime getting low** ({_fmt_seconds(tte)}).\n"
                f"Threshold: {warn_t}s"
            )

    if cfg.get("auto_shutdown_enabled", False):
        should_act = False
        if shutdown_imminent is True:
            should_act = True
        if ac_present is False and tte is not None and tte <= crit_t:
            should_act = True

        if should_act and _throttle_ok("auto_shutdown", 3600):
            action = str(cfg.get("auto_shutdown_action", "shutdown")).lower().strip()
            delay = int(cfg.get("auto_shutdown_delay_seconds", 0))
            force = bool(cfg.get("auto_shutdown_force", False))
            await _send_ups_alert(f"🛑 **UPS Auto Action:** initiating `{action}` in {delay}s (force={force}).")
            try:
                await execute_power_action(action, delay, force)
            except Exception as ex:
                logging.error(f"UPS auto action failed: {ex}")

    ups_last_state = state


@tasks.loop(seconds=30)
async def monitor_ups() -> None:
    cfg = _ups_cfg()
    poll_seconds = int(cfg.get("poll_seconds", 30))
    if monitor_ups.seconds != poll_seconds:
        try:
            monitor_ups.change_interval(seconds=poll_seconds)
        except Exception:
            pass

    if not cfg.get("enabled", True):
        return

    state = await _ups_poll_once()
    if state is None:
        return

    await _ups_handle_state(state)


# ---------------- Existing IP monitor ----------------

@tasks.loop(minutes=60)
async def check_ip() -> None:
    logging.info("Checking IP address...")
    try:
        current_ip = await get_ip()
        stored_ip = await load_ip()

        if current_ip != stored_ip:
            logging.info(f"IP address changed from {stored_ip} to {current_ip}")

            for subscriber in list(subscribers):
                try:
                    user = await bot.fetch_user(subscriber)
                    if user:
                        await user.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                        logging.info(f"IP change alert sent to user {subscriber}")
                    else:
                        logging.warning(f"Failed to send IP change alert to user {subscriber}: user not found")
                except discord.NotFound:
                    logging.warning(f"Subscriber {subscriber} not found. Removing from subscriber list.")
                    subscribers.discard(subscriber)
                    await save_subscribers(subscribers)
                except Exception as ex:
                    logging.warning(f"Failed DM to subscriber {subscriber}: {ex}")

            channel = bot.get_channel(CHANNEL)
            if channel:
                await channel.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                logging.info(f"IP change alert sent to channel {CHANNEL}")
            else:
                logging.warning(f"Channel {CHANNEL} not found (bot may not share a guild/cache yet).")

            for record in RECORD_IDS:
                await update_dns_record(API_KEY, EMAIL, ZONE_ID, record, current_ip)

            await save_ip(current_ip)

    except Exception as e:
        logging.error(f"Failed to check IP address: {e}")
    finally:
        logging.info("IP address check complete")
        logging.info("Waiting 60 minutes before checking again...")


# ---------------- Discord Events ----------------

@bot.event
async def on_ready() -> None:
    logging.info(f"Logged in as {bot.user.name} - {bot.user.id}")

    try:
        _preload_ups_history_from_log(hours=24)
    except Exception as ex:
        logging.warning(f"UPS history preload failed: {ex}")

    if not check_ip.is_running():
        check_ip.start()

    cfg = _ups_cfg()
    if cfg.get("enabled", True):
        if not TRIPPLITE_AVAILABLE:
            logging.warning("UPS monitoring enabled in cache.json, but tripplite is not installed. Skipping UPS monitor.")
        else:
            if not monitor_ups.is_running():
                monitor_ups.start()
            logging.info(f"UPS monitoring started. poll_seconds={cfg.get('poll_seconds', 30)}")
    else:
        logging.info("UPS monitoring is disabled (cache.json ups.enabled=false).")

    logging.info(f"Local slash commands loaded: {len(bot.application_commands)}")
    for c in bot.application_commands:
        logging.info(f" - /{c.name}")

    try:
        cache = _read_cache_json()
        cache = _ensure_ups_config(cache)
        force_sync = bool(cache.get("force_sync", False))

        current_fp = _commands_fingerprint()
        last_fp = cache.get("commands_fingerprint")

        if force_sync or last_fp != current_fp:
            await bot.sync_commands()
            cache["commands_fingerprint"] = current_fp
            cache["force_sync"] = False
            _write_cache_json(cache)
            logging.info("Slash commands synced.")
        else:
            logging.info("Slash commands unchanged; skipping sync.")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")


# ---------------- Slash Commands ----------------

@bot.slash_command(name="ip", description="Get Mitra's external IP address.")
async def ip(ctx: discord.ApplicationContext) -> None:
    logging.info(f"IP command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        current_ip = await get_ip()
        await ctx.respond(f"IP address:\n```{current_ip}```", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to get IP address:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get IP address: {e}")


@bot.slash_command(name="ping", description="Get the bot's latency.")
async def ping(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Ping command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        await ctx.respond(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to get latency:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get latency: {e}")


@bot.slash_command(name="subscribe", description="Subscribe to get IP change alerts sent my direct message.")
async def subscribe(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Subscribe command called by {ctx.user.id}, {ctx.user.name}")
    try:
        if ctx.user.id in subscribers:
            await ctx.respond("You are already subscribed to IP change alerts.", ephemeral=True)
            return

        subscribers.add(ctx.user.id)
        await ctx.respond("Your subscription to IP change alerts has been confirmed.", ephemeral=True)

        await ctx.user.send(
            "You have subscribed to IP change alerts. You will be notified here when Mitra's IP address changes.\n\n"
            "To unsubscribe, use the `/unsubscribe` command.\n\n"
            f"The current IP address is:\n```{await get_ip()}```"
        )
        await save_subscribers(subscribers)

    except Exception as e:
        await ctx.respond(f"Failed to subscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to subscribe: {e}")


@bot.slash_command(name="unsubscribe", description="Unsubscribe to stop getting IP change alerts sent my direct message.")
async def unsubscribe(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Unsubscribe command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        if ctx.user.id not in subscribers:
            await ctx.respond("You are not subscribed to IP change alerts.", ephemeral=True)
            return

        subscribers.remove(ctx.user.id)
        await save_subscribers(subscribers)
        await ctx.respond("You have unsubscribed from IP change alerts.", ephemeral=True)

    except Exception as e:
        await ctx.respond(f"Failed to unsubscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to unsubscribe: {e}")


@bot.slash_command(name="subscribers", description="List all subscribers.")
async def list_subscribers(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Subscribers command called by {ctx.user.id}, {ctx.user.name}")
    try:
        if len(subscribers) == 0:
            await ctx.respond("No subscribers.", ephemeral=True)
            return

        subscriber_names = []
        bad_subscribers = []
        for subscriber in list(subscribers):
            try:
                user = await bot.fetch_user(subscriber)
                if user:
                    subscriber_names.append(f"{user.id}: {user.name}")
                else:
                    bad_subscribers.append(subscriber)
            except discord.NotFound:
                bad_subscribers.append(subscriber)

        if bad_subscribers:
            for s in bad_subscribers:
                subscribers.discard(s)
            await save_subscribers(subscribers)

        if not subscriber_names:
            await ctx.respond("No subscribers.", ephemeral=True)
        else:
            await ctx.respond(f"Subscribers:\n```{', '.join(subscriber_names)}```", ephemeral=True)

    except Exception as e:
        await ctx.respond(f"Failed to list subscribers:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to list subscribers: {e}")


# ---- Power command group: /power restart|shutdown|cancel ----
power = bot.create_group(
    "power",
    "Admin power controls: restart/shutdown/cancel (admins only).",
    default_member_permissions=MITRA_ADMIN_GATE_PERMS,
    dm_permission=False,
)


@power.command(name="restart", description="Restart the server running this bot (admins only).")
async def power_restart(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(str, description="Type RESTART exactly to confirm.", required=True),
    mode: str = discord.Option(
        str,
        description="Restart timing. immediate = now, delayed = wait then restart.",
        choices=["immediate", "delayed"],
        default="immediate"
    ),
    delay: int = discord.Option(
        int,
        description="Delay in seconds (only used when mode=delayed). Example: 60",
        default=0,
        min_value=0,
        max_value=86400
    ),
    force: bool = discord.Option(
        bool,
        description="Force close apps without warning (Windows only).",
        default=False
    ),
) -> None:
    logging.info(f"/power restart requested by {ctx.user.id} ({ctx.user.name}) mode={mode} delay={delay} force={force}")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power restart denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "RESTART":
        logging.warning(f"/power restart denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type RESTART exactly.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode == "immediate":
        delay = 0
    else:
        if delay < 1:
            await ctx.respond("For delayed mode, delay must be at least 1 second.", ephemeral=True)
            return

    logging.warning(f"/power restart authorized: user_id={ctx.user.id} mode={mode} delay={delay} force={force}")
    await ctx.respond(f"Server restart scheduled.\nMode: {mode}\nDelay: {delay} seconds\nForce: {force}", ephemeral=True)

    async def _do():
        try:
            await execute_power_action("restart", delay, force)
        except Exception as ex:
            logging.error(f"Restart execution failed: {ex}")

    asyncio.create_task(_do())


@power.command(name="shutdown", description="Shut down (power off) the server running this bot (admins only).")
async def power_shutdown(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(str, description="Type SHUTDOWN exactly to confirm.", required=True),
    mode: str = discord.Option(
        str,
        description="Shutdown timing. immediate = now, delayed = wait then shut down.",
        choices=["immediate", "delayed"],
        default="immediate"
    ),
    delay: int = discord.Option(
        int,
        description="Delay in seconds (only used when mode=delayed). Example: 60",
        default=0,
        min_value=0,
        max_value=86400
    ),
    force: bool = discord.Option(
        bool,
        description="Force close apps without warning (Windows only).",
        default=False
    ),
) -> None:
    logging.info(f"/power shutdown requested by {ctx.user.id} ({ctx.user.name}) mode={mode} delay={delay} force={force}")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power shutdown denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "SHUTDOWN":
        logging.warning(f"/power shutdown denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type SHUTDOWN exactly.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode == "immediate":
        delay = 0
    else:
        if delay < 1:
            await ctx.respond("For delayed mode, delay must be at least 1 second.", ephemeral=True)
            return

    logging.warning(f"/power shutdown authorized: user_id={ctx.user.id} mode={mode} delay={delay} force={force}")
    await ctx.respond(f"Server shutdown scheduled.\nMode: {mode}\nDelay: {delay} seconds\nForce: {force}", ephemeral=True)

    async def _do():
        try:
            await execute_power_action("shutdown", delay, force)
        except Exception as ex:
            logging.error(f"Shutdown execution failed: {ex}")

    asyncio.create_task(_do())


@power.command(name="cancel", description="Cancel a pending shutdown/restart (admins only).")
async def power_cancel(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(str, description="Type CANCEL exactly to confirm.", required=True),
) -> None:
    logging.info(f"/power cancel requested by {ctx.user.id} ({ctx.user.name})")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power cancel denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "CANCEL":
        logging.warning(f"/power cancel denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type CANCEL exactly.", ephemeral=True)
        return

    await ctx.respond("Attempting to cancel any pending shutdown/restart.", ephemeral=True)

    try:
        await execute_power_action("cancel", 0, False)
        logging.warning(f"/power cancel executed by user_id={ctx.user.id}")
    except Exception as ex:
        logging.error(f"Cancel failed: {ex}")
        await ctx.respond(f"Cancel failed:\n```{ex}```", ephemeral=True)


# ---- UPS command group: /ups status|enable|disable ----
ups_group = bot.create_group(
    "ups",
    "UPS monitoring commands (admins only).",
    default_member_permissions=MITRA_ADMIN_GATE_PERMS,
    dm_permission=False,
)


@ups_group.command(name="enable", description="Enable UPS monitoring (admins only).")
async def ups_enable(ctx: discord.ApplicationContext) -> None:
    if not is_admin_user(ctx.user.id):
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    _ups_set_enabled(True)

    if not TRIPPLITE_AVAILABLE:
        await ctx.respond("UPS monitoring enabled in cache.json, but tripplite is not installed. Run: pip install tripplite", ephemeral=True)
        return

    if not monitor_ups.is_running():
        monitor_ups.start()

    await ctx.respond("UPS monitoring is now **enabled**.", ephemeral=True)
    logging.info(f"UPS monitoring enabled by user_id={ctx.user.id}")


@ups_group.command(name="disable", description="Disable UPS monitoring (admins only).")
async def ups_disable(ctx: discord.ApplicationContext) -> None:
    if not is_admin_user(ctx.user.id):
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    _ups_set_enabled(False)

    # Stop the loop if it is running
    if monitor_ups.is_running():
        monitor_ups.stop()

    await ctx.respond("UPS monitoring is now **disabled**.", ephemeral=True)
    logging.info(f"UPS monitoring disabled by user_id={ctx.user.id}")


@ups_group.command(name="status", description="Show the latest UPS status + chart (admins only).")
async def ups_status(
    ctx: discord.ApplicationContext,
    hours: int = discord.Option(
        int,
        description="How many hours of history to graph. Example: 6",
        required=False,
        default=0,
        min_value=1,
        max_value=168
    )
) -> None:
    if not is_admin_user(ctx.user.id):
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    cfg = _ups_cfg()

    if not TRIPPLITE_AVAILABLE:
        await ctx.respond("tripplite is not installed. UPS monitoring is unavailable.", ephemeral=True)
        return

    if hours <= 0:
        try:
            hours = int(cfg.get("graph_default_hours", 6))
        except Exception:
            hours = 6

    # Ensure we have a recent state for the summary
    state = ups_last_state
    if state is None:
        state = await _ups_poll_once()
        if state is not None:
            await _ups_handle_state(state)  # logs it too

    if state is None:
        await ctx.respond("No UPS data available. Check USB connection and permissions.", ephemeral=True)
        return

    status = state.get("status", {}) or {}
    tte = _ups_get_time_to_empty(state)
    health = state.get("health", None)
    inp = state.get("input", {}) or {}
    outp = state.get("output", {}) or {}

    # Build graph from history
    rows = _read_ups_log_recent(hours)
    graph_buf = _build_ups_status_graph(rows, hours)

    summary = (
        "**UPS Status**\n"
        f"Enabled: `{cfg.get('enabled', True)}` | Poll: `{cfg.get('poll_seconds', 30)}s`\n"
        f"AC present: `{status.get('ac present')}`\n"
        f"Charging: `{status.get('charging')}` | Discharging: `{status.get('discharging')}`\n"
        f"Fully charged: `{status.get('fully charged')}`\n"
        f"Needs replacement: `{status.get('needs replacement')}`\n"
        f"Shutdown imminent: `{status.get('shutdown imminent')}`\n"
        f"Health: `{health}`\n"
        f"Time to empty: `{_fmt_seconds(tte)}`\n"
        f"Input: `V={inp.get('voltage')} Hz={inp.get('frequency')}`\n"
        f"Output: `V={outp.get('voltage')} W={outp.get('power')}`\n"
    )

    if graph_buf is None:
        await ctx.respond(summary + "\n(No graph data available yet.)", ephemeral=True)
        return

    file = discord.File(fp=graph_buf, filename="ups_status.png")
    await ctx.respond(summary, file=file, ephemeral=True)


@ups_group.command(name="timezone", description="Set the timezone used for UPS graphs (admins only). Example: America/Los_Angeles")
async def ups_timezone(
    ctx: discord.ApplicationContext,
    tz: str = discord.Option(str, description="IANA timezone. Examples: UTC, America/Los_Angeles, Europe/Berlin", required=True),
) -> None:
    if not is_admin_user(ctx.user.id):
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    try:
        ZoneInfo(tz)  # validate
    except Exception:
        await ctx.respond("Invalid timezone. Use an IANA name like `UTC` or `America/Los_Angeles`.", ephemeral=True)
        return

    data = _read_cache_json()
    data = _ensure_ups_config(data)
    data["ups"]["timezone"] = tz
    _write_cache_json(data)

    await ctx.respond(f"UPS graph timezone set to `{tz}`.", ephemeral=True)


# ---------------- Error handlers ----------------

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: Exception):
    if isinstance(error, commands.errors.CommandNotFound):
        return
    raise error


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.errors.CommandNotFound):
        return
    raise error


# ---------------- Main ----------------

if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.setFormatter(CustomFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    file_handler = logging.FileHandler("bot.log")
    log_formatter = logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    TOKEN, CHANNEL, API_KEY, EMAIL, ZONE_ID, RECORD_IDS = load_connection_data()

    admins = load_admins()
    logging.info(f"Loaded {len(admins)} admin(s): {', '.join(str(a) for a in admins) if admins else 'none'}")

    subscribers = load_subscribers()
    logging.info(f"Loaded {len(subscribers)} subscriber(s): {', '.join(str(s) for s in subscribers) if subscribers else 'none'}")

    cfg = _ups_cfg()
    logging.info(
        "UPS config: "
        f"enabled={cfg.get('enabled')} "
        f"poll_seconds={cfg.get('poll_seconds')} "
        f"warn_time_to_empty_seconds={cfg.get('warn_time_to_empty_seconds')} "
        f"critical_time_to_empty_seconds={cfg.get('critical_time_to_empty_seconds')} "
        f"log_enabled={cfg.get('log_enabled')} "
        f"log_file={cfg.get('log_file')} "
        f"graph_default_hours={cfg.get('graph_default_hours')} "
        f"auto_shutdown_enabled={cfg.get('auto_shutdown_enabled')}"
    )

    if cfg.get("enabled", True) and not TRIPPLITE_AVAILABLE:
        logging.warning("UPS monitoring is enabled but tripplite is not installed. Run: pip install tripplite")

    logging.info("Starting bot...")
    bot.run(TOKEN)