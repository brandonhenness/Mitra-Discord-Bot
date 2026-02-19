# mitra_bot/storage/cache_store.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

CACHE_PATH = Path("cache.json")


def read_cache_json() -> Dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_cache_json(data: Dict[str, Any]) -> None:
    CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")


def ensure_ups_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mirrors the defaults from bot.py so behavior does not change.
    """
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


def read_cache_with_defaults() -> Dict[str, Any]:
    """
    Load cache.json and apply schema defaults/migrations. Writes back if updated.
    """
    data = read_cache_json()
    before = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    data = ensure_ups_config(data)

    after = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if after != before:
        try:
            write_cache_json(data)
        except Exception:
            logging.exception("Failed to write cache defaults to cache.json")

    return data


# -----------------------------
# Admins / Subscribers
# -----------------------------

def load_admins() -> Set[int]:
    data = read_cache_json()
    admins_list = data.get("admins", [])
    out: Set[int] = set()
    for x in admins_list:
        try:
            out.add(int(x))
        except Exception:
            pass
    return out


def load_subscribers() -> Set[int]:
    try:
        logging.info("Loading subscribers from cache file...")
        data = read_cache_json()
        if "subscribers" not in data:
            logging.warning("No subscribers found in cache file.")
            return set()
        return set(data.get("subscribers", []))
    except Exception:
        logging.exception("Failed to load subscribers from cache.json")
        return set()


async def save_subscribers(subscribers_set: Set[int]) -> None:
    data = read_cache_json()
    data["subscribers"] = list(subscribers_set)
    write_cache_json(data)
    logging.info(
        "Subscribers saved to cache file: %s",
        ", ".join([str(s) for s in subscribers_set]),
    )


# -----------------------------
# Public IP caching
# -----------------------------

async def load_ip() -> Optional[str]:
    try:
        data = read_cache_json()
        return data.get("ip")
    except Exception:
        logging.exception("Failed to load ip from cache.json")
        return None


async def save_ip(ip: str) -> None:
    data = read_cache_json()
    data["ip"] = ip
    write_cache_json(data)
    logging.info("IP address saved to cache file: %s", ip)


# -----------------------------
# UPS convenience helpers
# -----------------------------

def get_ups_config() -> Dict[str, Any]:
    data = read_cache_with_defaults()
    ups = data.get("ups", {})
    return ups if isinstance(ups, dict) else {}


def set_ups_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Patch UPS config keys and persist. Returns the new UPS config dict.
    """
    data = read_cache_with_defaults()
    ups = data.get("ups", {})
    if not isinstance(ups, dict):
        ups = {}
    ups.update(patch)
    data["ups"] = ups
    write_cache_json(data)
    return ups


# -----------------------------
# Power action helpers
# -----------------------------

def get_power_restart_notice() -> Optional[Dict[str, Any]]:
    data = read_cache_json()
    notice = data.get("power_restart_notice")
    return notice if isinstance(notice, dict) else None


def set_power_restart_notice(notice: Dict[str, Any]) -> None:
    data = read_cache_json()
    data["power_restart_notice"] = notice
    write_cache_json(data)


def clear_power_restart_notice() -> None:
    data = read_cache_json()
    if "power_restart_notice" in data:
        del data["power_restart_notice"]
        write_cache_json(data)
