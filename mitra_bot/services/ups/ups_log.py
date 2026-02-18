# mitra_bot/services/ups/ups_log.py
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Ensure the row has a standard 'ts' field (ISO UTC string).
    Also tolerates old key names (timestamp/time).
    """
    if not isinstance(row, dict):
        return None

    # Normalize timestamp key
    ts = row.get("ts")
    if not ts:
        ts = row.get("timestamp") or row.get("time") or row.get("datetime")

    if isinstance(ts, datetime):
        ts = ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if not isinstance(ts, str) or not ts.strip():
        return None

    row["ts"] = ts.strip()

    # Optional: normalize common alternate metric keys if your old file used different names
    if "time_to_empty" in row and "time_to_empty_seconds" not in row:
        try:
            row["time_to_empty_seconds"] = int(row["time_to_empty"])
        except Exception:
            pass

    return row


class UPSLogStore:
    def __init__(
        self,
        *,
        log_file: str,
        timezone_name: str = "UTC",
        history_limit: int = 5000,
    ) -> None:
        self.log_path = Path(log_file).expanduser().resolve()
        self.timezone_name = timezone_name
        self.history: Deque[Dict[str, Any]] = deque(maxlen=history_limit)

    def append(self, row: Dict[str, Any]) -> None:
        """
        Append a row to in-memory history and write JSONL.
        Ensures 'ts' exists for graphing.
        """
        if not isinstance(row, dict):
            return

        # Ensure timestamp exists
        if not row.get("ts") and not row.get("timestamp") and not row.get("time"):
            row["ts"] = _utc_now_iso()

        norm = _normalize_row(dict(row))
        if not norm:
            return

        self.history.append(norm)

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(norm, ensure_ascii=False) + "\n")
        except Exception:
            logging.exception("Failed writing UPS log row to %s", self.log_path)

    def preload_recent(self, hours: int = 24) -> None:
        """
        Load the last N hours from the JSONL file into memory.
        """
        if not self.log_path.exists():
            logging.warning("UPS log file not found for preload: %s", self.log_path)
            return

        logging.info("Preloading UPS log from: %s", self.log_path)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        loaded = 0
        try:
            with self.log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        row = json.loads(line)
                    except Exception:
                        continue

                    norm = _normalize_row(row)
                    if not norm:
                        continue

                    dt = _parse_ts(norm["ts"])
                    if not dt:
                        continue

                    if dt >= cutoff:
                        self.history.append(norm)
                        loaded += 1
        except Exception:
            logging.exception("Failed preloading UPS log from %s", self.log_path)

        logging.info("UPS preload complete. Rows loaded: %s", loaded)

    def get_recent(self, *, hours: int) -> List[Dict[str, Any]]:
        """
        Return rows from memory within the last N hours.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out: List[Dict[str, Any]] = []

        for row in list(self.history):
            ts = row.get("ts")
            if not isinstance(ts, str):
                continue
            dt = _parse_ts(ts)
            if not dt:
                continue
            if dt >= cutoff:
                out.append(row)

        return out
