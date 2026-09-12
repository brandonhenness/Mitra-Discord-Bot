"""Indexed UPS statistics with restart-safe, non-destructive JSONL migration."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .ups_rows import _normalize_row, _utc_now_iso


def normalized(row):
    value = _normalize_row(dict(row))
    if value is None:
        raise ValueError("Invalid UPS row")
    stamp = datetime.fromisoformat(value["ts"].replace("Z","+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    value["ts"] = stamp.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    return value,stamp.timestamp(),json.dumps(value,ensure_ascii=False,allow_nan=False)


class UPSLogStore:
    def __init__(self, *, log_file="ups_stats.db", database_file=None, timezone_name="UTC", history_limit=5000):
        self.timezone_name = timezone_name
        self.history_limit = max(2,int(history_limit))
        self._lock = threading.RLock()
        self._configured = None
        self.configure(log_file=log_file,database_file=database_file)

    def configure(self, *, log_file="ups_stats.db", database_file=None):
        source = Path(log_file).expanduser().resolve()
        destination = Path(database_file).expanduser().resolve() if database_file else source
        if destination.suffix.lower() in {".jsonl",".ndjson"}:
            destination = destination.with_suffix(".db")
        legacy = source if source.suffix.lower() in {".jsonl",".ndjson"} else destination.with_suffix(".jsonl")
        with self._lock:
            if self._configured == (destination,legacy):
                return
            destination.parent.mkdir(parents=True,exist_ok=True)
            self.log_path = destination  # Compatibility attribute; always points to SQLite.
            with closing(self._connect()) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS ups_samples (
                        id INTEGER PRIMARY KEY, ts REAL NOT NULL, battery_percent REAL,
                        input_v REAL, output_v REAL, output_w REAL, time_to_empty_s REAL,
                        on_battery INTEGER, ac_present INTEGER, health_pct REAL,
                        payload TEXT NOT NULL, legacy_key TEXT UNIQUE);
                    CREATE INDEX IF NOT EXISTS ups_samples_time ON ups_samples(ts,id);
                    CREATE TABLE IF NOT EXISTS ups_imports (source TEXT PRIMARY KEY, size INTEGER,
                        mtime INTEGER, imported INTEGER, skipped INTEGER);
                """)
            self._import_legacy(legacy)
            self._configured = (destination,legacy)

    def _connect(self):
        db = sqlite3.connect(self.log_path,timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _insert(db, row, ts, payload, key=None):
        def number(*names):
            for name in names:
                try:
                    value = float(row[name])
                    if math.isfinite(value):
                        return value
                except (ValueError,TypeError,KeyError):
                    pass
            return None
        return db.execute("""INSERT OR IGNORE INTO ups_samples
            (ts,battery_percent,input_v,output_v,output_w,time_to_empty_s,on_battery,ac_present,health_pct,payload,legacy_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ts,number("battery_percent"),number("input_v","input_voltage"),number("output_v"),number("output_w"),
             number("time_to_empty_s","time_to_empty_seconds","time_to_empty"),number("on_battery"),number("ac_present"),
             number("health_pct","health"),payload,key)).rowcount

    def _import_legacy(self, source):
        if not source.is_file():
            return
        stat = source.stat()
        imported = skipped = 0
        with closing(self._connect()) as db:
            previous = db.execute("SELECT size,mtime FROM ups_imports WHERE source=?",(str(source),)).fetchone()
            if previous == (stat.st_size,stat.st_mtime_ns):
                return
            with source.open("rb") as stream:
                for line_number,line in enumerate(stream,1):
                    if not line.strip():
                        continue
                    try:
                        row,ts,payload = normalized(json.loads(line.decode("utf-8-sig")))
                    except (ValueError,TypeError,UnicodeError):
                        skipped += 1
                        continue
                    key = hashlib.sha256(str(source).encode()+b":"+str(line_number).encode()+b":"+line).hexdigest()
                    imported += self._insert(db,row,ts,payload,key)
                    if line_number % 500 == 0:
                        db.commit()  # An interrupted import resumes idempotently by stable source-row keys.
            with db:
                db.execute("INSERT OR REPLACE INTO ups_imports VALUES (?,?,?,?,?)",
                           (str(source),stat.st_size,stat.st_mtime_ns,imported,skipped))
        logging.info("UPS database migration: imported %s rows, skipped %s invalid rows; original retained at %s",imported,skipped,source)

    def append(self,row):
        if not isinstance(row,dict):
            return
        value = dict(row)
        if not any(value.get(name) for name in ("ts","timestamp","time","datetime")):
            value["ts"] = _utc_now_iso()
        try:
            value,ts,payload = normalized(value)
            with self._lock, closing(self._connect()) as db, db:
                self._insert(db,value,ts,payload)
        except Exception:
            # Storage failure must not disable UPS power protection.
            logging.exception("Failed to persist UPS statistics in %s",self.log_path)

    def preload_recent(self,hours=24):
        """Compatibility hook: history is queried from SQLite, not preloaded into RAM."""

    def get_recent(self, *, hours, limit=None):
        if not 0 < hours <= 24*366:
            raise ValueError("UPS history window must be between 0 and 8784 hours")
        limit = self.history_limit if limit is None else max(2,int(limit))
        cutoff = datetime.now(timezone.utc).timestamp()-hours*3600
        with self._lock, closing(self._connect()) as db:
            # Return a bounded sample spanning the ENTIRE window, including both endpoints.
            rows = db.execute("""WITH ranked AS (
                SELECT payload,ROW_NUMBER() OVER (ORDER BY ts,id) AS n,COUNT(*) OVER () AS total
                FROM ups_samples WHERE ts>=? AND ts<=?)
                SELECT payload FROM ranked WHERE total<=? OR n=1 OR n=total
                OR (n-1) % MAX(1,CAST((total-1+?-2)/(?-1) AS INTEGER))=0 ORDER BY n""",
                (cutoff,datetime.now(timezone.utc).timestamp(),limit,limit,limit)).fetchall()
        return [json.loads(row[0]) for row in rows]
