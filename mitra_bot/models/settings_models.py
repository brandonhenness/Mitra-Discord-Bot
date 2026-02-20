from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UPSSettingsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    poll_seconds: int = 30
    warn_time_to_empty_seconds: int = 600
    critical_time_to_empty_seconds: int = 180
    auto_shutdown_enabled: bool = False
    auto_shutdown_action: str = "shutdown"
    auto_shutdown_delay_seconds: int = 0
    auto_shutdown_force: bool = False
    log_enabled: bool = True
    log_file: str = "ups_stats.jsonl"
    graph_default_hours: int = 6
    timezone: str = "UTC"


class AppSettingsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    channel_id: Optional[int] = Field(default=None)
    channel: Optional[int] = Field(default=None)
    ip_poll_seconds: int = 900
    ups: UPSSettingsModel = Field(default_factory=UPSSettingsModel)
    admin_role_name: str = "Mitra Admin"
    ip_subscriber_role_name: str = "Mitra IP Subscriber"

    @field_validator("channel_id", "channel", mode="before")
    @classmethod
    def parse_channel_id(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @property
    def resolved_channel_id(self) -> Optional[int]:
        return self.channel_id if self.channel_id is not None else self.channel
