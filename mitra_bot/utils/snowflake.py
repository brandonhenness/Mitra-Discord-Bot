from __future__ import annotations

from typing import Any, Optional


def to_snowflake_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(int(value))
    except Exception:
        return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def to_int_optional(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
