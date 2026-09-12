# mitra_bot/services/ups/ups_log.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, model_validator


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


class UPSLogRowModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("UPS log row must be an object.")

        row = dict(value)
        ts = row.get("ts")
        if not ts:
            ts = row.get("timestamp") or row.get("time") or row.get("datetime")

        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        if not isinstance(ts, str) or not ts.strip():
            raise ValueError("UPS log row is missing timestamp.")

        row["ts"] = ts.strip()

        if "time_to_empty" in row and "time_to_empty_seconds" not in row:
            try:
                row["time_to_empty_seconds"] = int(row["time_to_empty"])
            except Exception:
                pass

        return row


def _normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        model = UPSLogRowModel.model_validate(row)
    except Exception:
        return None
    return model.model_dump(mode="json")


