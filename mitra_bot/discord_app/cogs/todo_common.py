from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import discord
from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "..."


def status_emoji(status: str) -> str:
    if status == "in_progress":
        return "🟡"
    if status == "done":
        return "✅"
    return "⬜"


def status_label(status: str) -> str:
    if status == "in_progress":
        return "In Progress"
    if status == "done":
        return "Done"
    return "Open"


@dataclass
class TodoItem:
    id: int
    title: str
    notes: str
    status: str
    assignee_ids: List[int]
    thread_id: Optional[int]
    created_by: int
    created_at: str


class TodoItemPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    title: str = "Untitled"
    notes: str = ""
    status: str = "open"
    done: bool = False
    assignee_ids: List[int] = Field(default_factory=list)
    assignee_id: Optional[int] = None
    thread_id: Optional[int] = None
    created_by: int = 0
    created_at: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        if not isinstance(value, str):
            return "open"
        if value in {"open", "in_progress", "done"}:
            return value
        return "open"

    @field_validator("assignee_ids", mode="before")
    @classmethod
    def normalize_assignees(cls, value: object) -> List[int]:
        if not isinstance(value, list):
            return []
        out: List[int] = []
        for raw in value:
            try:
                out.append(int(raw))
            except Exception:
                pass
        return out


def to_item(raw: dict) -> TodoItem:
    payload = TodoItemPayload.model_validate(raw if isinstance(raw, dict) else {})
    assignee_ids = list(payload.assignee_ids)
    if not assignee_ids and payload.assignee_id is not None:
        assignee_ids = [payload.assignee_id]
    return TodoItem(
        id=payload.id,
        title=payload.title,
        notes=payload.notes,
        status=payload.status,
        assignee_ids=assignee_ids,
        thread_id=payload.thread_id,
        created_by=payload.created_by,
        created_at=payload.created_at,
    )


def to_raw(item: TodoItem) -> dict:
    payload = TodoItemPayload(
        id=item.id,
        title=item.title,
        notes=item.notes,
        status=item.status,
        done=item.status == "done",
        assignee_ids=item.assignee_ids,
        assignee_id=item.assignee_ids[0] if item.assignee_ids else None,
        thread_id=item.thread_id,
        created_by=item.created_by,
        created_at=item.created_at,
    )
    return payload.model_dump(mode="json")


def assignee_mentions(item: TodoItem) -> str:
    if not item.assignee_ids:
        return "_Unassigned_"
    return ", ".join(f"<@{uid}>" for uid in item.assignee_ids)


def build_task_embed(item: TodoItem) -> discord.Embed:
    embed = discord.Embed(
        title=f"Task #{item.id}: {item.title}",
        color=discord.Color.blurple(),
        description=item.notes or "_No notes_",
    )
    embed.add_field(
        name="Status",
        value=f"{status_emoji(item.status)} {status_label(item.status)}",
        inline=True,
    )
    embed.add_field(name="Assignees", value=assignee_mentions(item), inline=True)
    embed.add_field(name="Created By", value=f"<@{item.created_by}>", inline=True)
    embed.add_field(name="Created At", value=item.created_at, inline=False)
    return embed
