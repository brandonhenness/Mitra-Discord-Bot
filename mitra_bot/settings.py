# mitra_bot/settings.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mitra_bot.storage.cache_store import read_cache_with_defaults, write_cache_json


@dataclass(frozen=True)
class UPSSettings:
    enabled: bool = True
    poll_seconds: int = 30

    warn_time_to_empty_seconds: int = 600
    critical_time_to_empty_seconds: int = 180

    auto_shutdown_enabled: bool = False
    auto_shutdown_action: str = "shutdown"  # shutdown|restart
    auto_shutdown_delay_seconds: int = 0
    auto_shutdown_force: bool = False

    log_enabled: bool = True
    log_file: str = "ups_stats.jsonl"
    graph_default_hours: int = 6

    timezone: str = "UTC"


@dataclass(frozen=True)
class AppSettings:
    token: str
    channel_id: Optional[int]
    ip_poll_seconds: int
    ups: UPSSettings

    admin_role_name: str
    ip_subscriber_role_name: str


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _coerce_channel_id(cfg: Dict[str, Any]) -> Optional[int]:
    raw = cfg.get("channel_id")
    if raw is None:
        raw = cfg.get("channel")

    if raw is None:
        return None

    try:
        return int(raw)
    except Exception:
        return None


def load_settings(*, interactive_token: bool = True) -> AppSettings:
    """
    Load settings from cache.json + env overrides.
    Optionally prompt for token if missing.
    """
    cfg = read_cache_with_defaults()

    env_token = os.getenv("MITRA_TOKEN") or os.getenv("DISCORD_TOKEN")
    token = (env_token or cfg.get("token") or "").strip()

    if not token and interactive_token:
        token = input("Please enter your Discord bot token: ").strip()
        cfg["token"] = token
        write_cache_json(cfg)

    if not token:
        raise RuntimeError("Discord token is missing (cache.json or env var).")

    ip_poll_seconds = _coerce_int(cfg.get("ip_poll_seconds", 900), 900)

    ups_cfg = cfg.get("ups", {}) if isinstance(cfg.get("ups"), dict) else {}
    ups = UPSSettings(
        enabled=_coerce_bool(ups_cfg.get("enabled", True), True),
        poll_seconds=_coerce_int(ups_cfg.get("poll_seconds", 30), 30),
        warn_time_to_empty_seconds=_coerce_int(
            ups_cfg.get("warn_time_to_empty_seconds", 600), 600
        ),
        critical_time_to_empty_seconds=_coerce_int(
            ups_cfg.get("critical_time_to_empty_seconds", 180), 180
        ),
        auto_shutdown_enabled=_coerce_bool(
            ups_cfg.get("auto_shutdown_enabled", False), False
        ),
        auto_shutdown_action=str(ups_cfg.get("auto_shutdown_action", "shutdown")),
        auto_shutdown_delay_seconds=_coerce_int(
            ups_cfg.get("auto_shutdown_delay_seconds", 0), 0
        ),
        auto_shutdown_force=_coerce_bool(
            ups_cfg.get("auto_shutdown_force", False), False
        ),
        log_enabled=_coerce_bool(ups_cfg.get("log_enabled", True), True),
        log_file=str(ups_cfg.get("log_file", "ups_stats.jsonl")),
        graph_default_hours=_coerce_int(ups_cfg.get("graph_default_hours", 6), 6),
        timezone=str(ups_cfg.get("timezone", "UTC")),
    )

    admin_role_name = str(cfg.get("admin_role_name", "Mitra Admin"))
    ip_subscriber_role_name = str(
        cfg.get("ip_subscriber_role_name", "Mitra IP Subscriber")
    )

    return AppSettings(
        token=token,
        channel_id=_coerce_channel_id(cfg),
        ip_poll_seconds=ip_poll_seconds,
        ups=ups,
        admin_role_name=admin_role_name,
        ip_subscriber_role_name=ip_subscriber_role_name,
    )
