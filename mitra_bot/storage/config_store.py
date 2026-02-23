from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w


class UPSFileConfigModel(BaseModel):
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


class CloudflareFileConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    zone_id: str | None = None
    record_ids: list[str] = Field(default_factory=list)


class BotFileConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    channel_id: int | None = None
    ip_poll_seconds: int = 900
    admin_role_name: str = "Mitra Admin"
    ip_subscriber_role_name: str = "Mitra IP Subscriber"

    @field_validator("channel_id", mode="before")
    @classmethod
    def _coerce_channel_id(cls, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None


class FileConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bot: BotFileConfigModel = Field(default_factory=BotFileConfigModel)
    ups: UPSFileConfigModel = Field(default_factory=UPSFileConfigModel)
    cloudflare: CloudflareFileConfigModel = Field(default_factory=CloudflareFileConfigModel)


def get_config_path() -> Path:
    raw = (os.getenv("MITRA_CONFIG_PATH") or "config.toml").strip()
    return Path(raw)


def read_config_dict() -> Dict[str, Any]:
    path = get_config_path()
    if not path.exists():
        return FileConfigModel().model_dump(mode="json")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return FileConfigModel.model_validate(data).model_dump(mode="json")


def write_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = FileConfigModel.model_validate(data).model_dump(mode="json")
    normalized = _drop_none(normalized)
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(normalized), encoding="utf-8")
    return normalized


def ensure_config_file() -> Dict[str, Any]:
    current = read_config_dict()
    return write_config_dict(current)


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value if v is not None]
    return value
