from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict

SCHEMA_VERSION = 1


def get_state_path() -> Path:
    raw = (os.getenv("MITRA_STATE_PATH") or "state.db").strip()
    return Path(raw)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            version = self._get_meta_int(conn, "schema_version", default=0)
            if version < 1:
                self._set_meta_int(conn, "schema_version", 1)
            conn.commit()

    def _get_meta_int(self, conn: sqlite3.Connection, key: str, *, default: int) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except Exception:
            return default

    def _set_meta_int(self, conn: sqlite3.Connection, key: str, value: int) -> None:
        conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(int(value))),
        )

    def get_meta(self, key: str) -> str | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def get_json(self, key: str, default: Any) -> Any:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM state_kv WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def set_json(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO state_kv(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, payload),
            )
            conn.commit()

    def delete_key(self, key: str) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute("DELETE FROM state_kv WHERE key = ?", (key,))
            conn.commit()

    def read_all(self) -> Dict[str, Any]:
        with self._lock, closing(self._connect()) as conn:
            out: Dict[str, Any] = {}
            for row in conn.execute("SELECT key, value FROM state_kv"):
                key = str(row["key"])
                try:
                    out[key] = json.loads(row["value"])
                except Exception:
                    continue
            return out


_STORE: StateStore | None = None


def get_state_store() -> StateStore:
    global _STORE
    if _STORE is None or _STORE.path != get_state_path():
        _STORE = StateStore(get_state_path())
    return _STORE


def reset_state_store_for_tests() -> None:
    global _STORE
    _STORE = None
