# mitra_bot/settings.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from getpass import getpass

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mitra_bot.storage.config_store import read_config_dict


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
    log_file: str = "ups_stats.db"
    database_file: Optional[str] = None
    graph_default_hours: int = 6

    timezone: str = "UTC"


@dataclass(frozen=True)
class AppSettings:
    token: str
    cloudflare_api_token: Optional[str]
    channel_id: Optional[int]
    ip_poll_seconds: int
    ups: UPSSettings

    admin_role_name: str
    ip_subscriber_role_name: str
    infrastructure_guild_ids: tuple[int, ...] = ()


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    token: Optional[str] = Field(
        default=None,
        validation_alias="DISCORD_APPLICATION_TOKEN",
    )
    cloudflare_api_token: Optional[str] = Field(
        default=None,
        validation_alias="CLOUDFLARE_API_TOKEN",
    )


def load_settings(*, interactive_token: bool = True) -> AppSettings:
    """
    Load settings from config.toml + env overrides.
    Optionally prompt for token if missing.
    """
    cfg = read_config_dict()
    bot_cfg = cfg.get("bot", {}) if isinstance(cfg.get("bot"), dict) else {}
    ups_cfg = cfg.get("ups", {}) if isinstance(cfg.get("ups"), dict) else {}

    env = EnvSettings()
    token = (env.token or "").strip()
    cloudflare_api_token = (env.cloudflare_api_token or "").strip() or None

    if not token and interactive_token:
        token = getpass("Please enter your Discord bot token (hidden): ").strip()

    if not token:
        raise RuntimeError("Discord token is missing (set DISCORD_APPLICATION_TOKEN).")

    ups = UPSSettings(
        enabled=bool(ups_cfg.get("enabled", True)),
        poll_seconds=int(ups_cfg.get("poll_seconds", 30)),
        warn_time_to_empty_seconds=int(ups_cfg.get("warn_time_to_empty_seconds", 600)),
        critical_time_to_empty_seconds=int(ups_cfg.get("critical_time_to_empty_seconds", 180)),
        auto_shutdown_enabled=bool(ups_cfg.get("auto_shutdown_enabled", False)),
        auto_shutdown_action=str(ups_cfg.get("auto_shutdown_action", "shutdown")),
        auto_shutdown_delay_seconds=int(ups_cfg.get("auto_shutdown_delay_seconds", 0)),
        auto_shutdown_force=bool(ups_cfg.get("auto_shutdown_force", False)),
        log_enabled=bool(ups_cfg.get("log_enabled", True)),
        log_file=str(ups_cfg.get("log_file", "ups_stats.db")),
        database_file=ups_cfg.get("database_file"),
        graph_default_hours=int(ups_cfg.get("graph_default_hours", 6)),
        timezone=str(ups_cfg.get("timezone", "UTC")),
    )

    channel_id = bot_cfg.get("channel_id")
    try:
        channel_id = int(channel_id) if channel_id is not None else None
    except Exception:
        channel_id = None

    return AppSettings(
        token=token,
        cloudflare_api_token=cloudflare_api_token,
        channel_id=channel_id,
        ip_poll_seconds=int(bot_cfg.get("ip_poll_seconds", 900)),
        ups=ups,
        admin_role_name=str(bot_cfg.get("admin_role_name", "Mitra Admin")),
        ip_subscriber_role_name=str(bot_cfg.get("ip_subscriber_role_name", "Mitra Alerts")),
        infrastructure_guild_ids=tuple(bot_cfg["infrastructure_guild_ids"]
            if bot_cfg.get("infrastructure_guild_ids") is not None
            else ([bot_cfg["guild_id"]] if bot_cfg.get("guild_id") else [])),
    )
