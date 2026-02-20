# mitra_bot/settings.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from mitra_bot.models.settings_models import AppSettingsModel
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

    parsed = AppSettingsModel.model_validate(cfg)

    ups = UPSSettings(
        enabled=parsed.ups.enabled,
        poll_seconds=parsed.ups.poll_seconds,
        warn_time_to_empty_seconds=parsed.ups.warn_time_to_empty_seconds,
        critical_time_to_empty_seconds=parsed.ups.critical_time_to_empty_seconds,
        auto_shutdown_enabled=parsed.ups.auto_shutdown_enabled,
        auto_shutdown_action=parsed.ups.auto_shutdown_action,
        auto_shutdown_delay_seconds=parsed.ups.auto_shutdown_delay_seconds,
        auto_shutdown_force=parsed.ups.auto_shutdown_force,
        log_enabled=parsed.ups.log_enabled,
        log_file=parsed.ups.log_file,
        graph_default_hours=parsed.ups.graph_default_hours,
        timezone=parsed.ups.timezone,
    )

    return AppSettings(
        token=token,
        channel_id=parsed.resolved_channel_id,
        ip_poll_seconds=parsed.ip_poll_seconds,
        ups=ups,
        admin_role_name=parsed.admin_role_name,
        ip_subscriber_role_name=parsed.ip_subscriber_role_name,
    )
