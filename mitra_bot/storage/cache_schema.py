from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _snowflake_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(int(value))
    except Exception:
        return None


class UPSConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

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


class TodoTaskModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = 0
    title: str = "Untitled"
    notes: str = ""
    status: str = "open"
    done: bool = False
    assignee_ids: list[str] = Field(default_factory=list)
    assignee_id: Optional[str] = None
    thread_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "open"
        if value in {"open", "in_progress", "done"}:
            return value
        return "open"

    @field_validator("assignee_ids", mode="before")
    @classmethod
    def _normalize_assignee_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for raw in value:
            sf = _snowflake_str(raw)
            if sf is not None:
                out.append(sf)
        return out

    @field_validator("assignee_id", "thread_id", "created_by", mode="before")
    @classmethod
    def _normalize_optional_snowflake(cls, value: Any) -> Optional[str]:
        return _snowflake_str(value)


class TodoGuildRecordModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    category_id: Optional[str] = None
    hub_channel_id: Optional[str] = None
    hub_message_id: Optional[str] = None

    @field_validator("category_id", "hub_channel_id", "hub_message_id", mode="before")
    @classmethod
    def _normalize_optional_snowflake(cls, value: Any) -> Optional[str]:
        return _snowflake_str(value)


class TodoListRecordModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    guild_id: Optional[str] = None
    board_message_id: Optional[str] = None
    tasks: list[TodoTaskModel] = Field(default_factory=list)

    @field_validator("guild_id", "board_message_id", mode="before")
    @classmethod
    def _normalize_optional_snowflake(cls, value: Any) -> Optional[str]:
        return _snowflake_str(value)


class TodoConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    guilds: Dict[str, TodoGuildRecordModel] = Field(default_factory=dict)
    lists: Dict[str, TodoListRecordModel] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, value: Any) -> Dict[str, Any]:
        data = dict(value) if isinstance(value, dict) else {}

        guilds = data.get("guilds")
        if not isinstance(guilds, dict):
            guilds = {}
        lists = data.get("lists")
        if not isinstance(lists, dict):
            lists = {}

        old_categories = data.get("categories", {})
        old_hubs = data.get("hubs", {})
        old_hub_messages = data.get("hub_messages", {})
        old_board_messages = data.get("board_messages", {})
        old_tasks = data.get("tasks", {})

        if isinstance(old_categories, dict):
            for g, cat_id in old_categories.items():
                rec = guilds.get(str(g), {})
                if not isinstance(rec, dict):
                    rec = {}
                rec.setdefault("category_id", cat_id)
                rec.setdefault("hub_channel_id", None)
                rec.setdefault("hub_message_id", None)
                guilds[str(g)] = rec

        if isinstance(old_hubs, dict):
            for g, hub_id in old_hubs.items():
                rec = guilds.get(str(g), {})
                if not isinstance(rec, dict):
                    rec = {}
                rec.setdefault("category_id", None)
                rec["hub_channel_id"] = hub_id
                rec.setdefault("hub_message_id", None)
                guilds[str(g)] = rec

        if isinstance(old_hub_messages, dict):
            for g, msg_id in old_hub_messages.items():
                rec = guilds.get(str(g), {})
                if not isinstance(rec, dict):
                    rec = {}
                rec.setdefault("category_id", None)
                rec.setdefault("hub_channel_id", None)
                rec["hub_message_id"] = msg_id
                guilds[str(g)] = rec

        if isinstance(old_board_messages, dict):
            for ch, msg_id in old_board_messages.items():
                rec = lists.get(str(ch), {})
                if not isinstance(rec, dict):
                    rec = {}
                rec.setdefault("guild_id", None)
                rec["board_message_id"] = msg_id
                rec.setdefault("tasks", [])
                lists[str(ch)] = rec

        if isinstance(old_tasks, dict):
            for ch, rows in old_tasks.items():
                rec = lists.get(str(ch), {})
                if not isinstance(rec, dict):
                    rec = {}
                rec.setdefault("guild_id", None)
                rec.setdefault("board_message_id", None)
                rec["tasks"] = rows if isinstance(rows, list) else []
                lists[str(ch)] = rec

        data["guilds"] = guilds
        data["lists"] = lists
        return data

    @model_validator(mode="after")
    def _heal_single_guild_lists(self) -> "TodoConfigModel":
        if len(self.guilds) != 1:
            return self
        only_guild_id = next(iter(self.guilds.keys()))
        for rec in self.lists.values():
            if rec.guild_id is None or rec.guild_id not in self.guilds:
                rec.guild_id = only_guild_id
        return self


class CloudflareConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    api_token: Optional[str] = None
    api_key: Optional[str] = None
    email: Optional[str] = None
    zone_id: Optional[str] = None
    record_ids: list[str] = Field(default_factory=list)
    enabled: Optional[bool] = None

    @field_validator("record_ids", mode="before")
    @classmethod
    def _normalize_record_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for raw in value:
            if raw is None:
                continue
            out.append(str(raw))
        return out


class NotificationsConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    guild_channels: Dict[str, str] = Field(default_factory=dict)

    @field_validator("guild_channels", mode="before")
    @classmethod
    def _normalize_guild_channels(cls, value: Any) -> Dict[str, str]:
        if not isinstance(value, dict):
            return {}
        out: Dict[str, str] = {}
        for raw_guild_id, raw_channel_id in value.items():
            guild_id = _snowflake_str(raw_guild_id)
            channel_id = _snowflake_str(raw_channel_id)
            if guild_id is None or channel_id is None:
                continue
            out[guild_id] = channel_id
        return out


class PowerRestartNoticeModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    channel_id: Optional[str] = None
    guild_id: Optional[str] = None
    message_id: Optional[str] = None
    requested_by_user_id: Optional[str] = None
    confirmed_by_user_id: Optional[str] = None

    @field_validator(
        "channel_id",
        "guild_id",
        "message_id",
        "requested_by_user_id",
        "confirmed_by_user_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_snowflake(cls, value: Any) -> Optional[str]:
        return _snowflake_str(value)


class CacheModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    ups: UPSConfigModel = Field(default_factory=UPSConfigModel)
    todo_config: TodoConfigModel = Field(default_factory=TodoConfigModel)
    cloudflare: CloudflareConfigModel = Field(default_factory=CloudflareConfigModel)
    notifications: NotificationsConfigModel = Field(default_factory=NotificationsConfigModel)
    power_restart_notice: Optional[PowerRestartNoticeModel] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, value: Any) -> Dict[str, Any]:
        data = dict(value) if isinstance(value, dict) else {}

        cloudflare = data.get("cloudflare")
        if not isinstance(cloudflare, dict):
            cloudflare = {}
        for key in ("api_token", "api_key", "email", "zone_id", "record_ids", "enabled"):
            if key not in cloudflare and key in data:
                cloudflare[key] = data.get(key)
        data["cloudflare"] = cloudflare

        if "todo_config" not in data or not isinstance(data.get("todo_config"), dict):
            data["todo_config"] = {}

        # Drop legacy top-level todo keys that caused confusion.
        data.pop("todo_channel_id", None)
        data.pop("todo_category_id", None)
        data.pop("todo_board_messages", None)
        return data


def normalize_cache_data(data: Dict[str, Any]) -> Dict[str, Any]:
    model = CacheModel.model_validate(data if isinstance(data, dict) else {})
    return model.model_dump(mode="json", exclude_none=False)
