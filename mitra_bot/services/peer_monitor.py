"""Observer-owned availability records, independent probes and bounded replication.

SQLite is used only on the event-loop thread. Network I/O never holds a transaction.
The power journal and legacy application settings are deliberately separate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    subject: str = Field(max_length=48)
    start: float
    ts: float
    up: bool
    gateway: bool | None
    rtt: float | None = Field(default=None, ge=0, le=120000)
    boot: str | None = Field(default=None, max_length=64)
    uptime: int | None = Field(default=None, ge=0)
    state: Literal["unknown", "reachable", "suspect", "unreachable", "recovering"]
    reason: str = Field(default="", max_length=100)
    skew: bool = False


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    subject: str = Field(max_length=48)
    kind: Literal["peer", "discord"]
    start: float
    detected: float
    recovered: float | None = None
    last_success: float | None = None
    reason: str = Field(max_length=100)


class MonitorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    health_interval: int = Field(default=10, ge=5, le=300)
    health_timeout: int = Field(default=3, ge=1, le=30)
    health_failures: int = Field(default=3, ge=2, le=20)
    health_down_seconds: int = Field(default=30, ge=10, le=3600)
    health_startup_grace: int = Field(default=60, ge=30, le=3600)
    health_recovery_successes: int = Field(default=2, ge=2, le=20)
    health_cooldown: int = Field(default=300, ge=0, le=86400)
    health_raw_days: int = Field(default=7, ge=1, le=30)
    health_history_days: int = Field(default=90, ge=7, le=365)
    health_incident_days: int = Field(default=365, ge=7, le=730)


class Setting(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[0-9]{1,20}$")
    guild: int = Field(ge=1)
    subject: str = Field(max_length=48)  # '*' is this guild's destination
    channel: int | None = Field(default=None, ge=1)
    role: int | None = Field(default=None, ge=1)
    enabled: bool = True
    policy: MonitorPolicy | None = None
    maintenance_until: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    maintenance_reason: str = Field(default="", max_length=200)
    message_id: int | None = Field(default=None, ge=1)
    dashboard_interval: int = Field(default=300, ge=60, le=3600)
    dashboard_hours: int = Field(default=24, ge=1, le=2160)
    dashboard_page: int = Field(default=0, ge=0, le=8)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: int = Field(ge=1)
    kind: Literal["sample", "incident", "setting"]
    value: dict


class HistoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    records: list[Record] = Field(max_length=500)
    cursor: int = Field(ge=0)
    gap: bool
    settings: list[Setting] = Field(max_length=1000)


class MonitorStore:
    def __init__(self, db, node, members):
        self.db, self.node, self.members = db, node, set(members)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS health_sequence (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER);
            INSERT OR IGNORE INTO health_sequence VALUES (1,0);
            CREATE TABLE IF NOT EXISTS health_events (observer TEXT, seq INTEGER, kind TEXT, ts REAL, value TEXT,
                PRIMARY KEY(observer,seq));
            CREATE INDEX IF NOT EXISTS health_events_time ON health_events(ts);
            CREATE INDEX IF NOT EXISTS health_events_subject ON health_events
                (observer,kind,json_extract(value,'$.subject'),ts);
            CREATE TABLE IF NOT EXISTS health_latest (observer TEXT, subject TEXT, seq INTEGER, value TEXT,
                PRIMARY KEY(observer,subject));
            CREATE TABLE IF NOT EXISTS health_last_success (observer TEXT, subject TEXT, seq INTEGER, value TEXT,
                PRIMARY KEY(observer,subject));
            CREATE TABLE IF NOT EXISTS health_watermarks (observer TEXT, subject TEXT, ts REAL,
                PRIMARY KEY(observer,subject));
            CREATE TABLE IF NOT EXISTS health_incidents (observer TEXT, id TEXT, seq INTEGER, value TEXT,
                PRIMARY KEY(observer,id));
            CREATE INDEX IF NOT EXISTS health_incidents_subject ON health_incidents
                (observer,json_extract(value,'$.subject'),json_extract(value,'$.kind'),json_extract(value,'$.recovered'));
            CREATE TABLE IF NOT EXISTS health_settings (guild INTEGER, subject TEXT, revision TEXT, value TEXT,
                PRIMARY KEY(guild,subject));
            CREATE TABLE IF NOT EXISTS health_cursors (observer TEXT PRIMARY KEY, seq INTEGER, gap INTEGER);
            CREATE TABLE IF NOT EXISTS health_rollups (observer TEXT, subject TEXT, bucket INTEGER,
                up REAL, down REAL, gateway_up REAL, gateway_down REAL, rtt_total REAL, rtt_count REAL,
                PRIMARY KEY(observer,subject,bucket));
            CREATE TABLE IF NOT EXISTS health_outbox (id TEXT PRIMARY KEY, incident TEXT, guild INTEGER,
                kind TEXT, due REAL, attempts INTEGER DEFAULT 0, message TEXT, delivered REAL, error TEXT);
        """)

    def settings(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT value FROM health_settings")]

    def setting(self, guild, subject):
        row = self.db.execute("SELECT value FROM health_settings WHERE guild=? AND subject=?", (guild, subject)).fetchone()
        return json.loads(row[0]) if row else None

    def _setting(self, value):
        setting = Setting.model_validate(value)
        if setting.subject not in {"*", "@monitoring", "@dashboard"} and setting.subject not in self.members:
            raise ValueError("Unknown setting subject")
        old = self.setting(setting.guild, setting.subject)
        # Snowflakes order concurrent configuration; equal revisions must agree.
        if old and int(old["revision"]) == int(setting.revision) and Setting.model_validate(old).model_dump() != setting.model_dump():
            raise ValueError("Conflicting setting revision")
        if old is None or int(setting.revision) > int(old["revision"]):
            self.db.execute("INSERT OR REPLACE INTO health_settings VALUES (?,?,?,?)",
                            (setting.guild, setting.subject, setting.revision, json.dumps(setting.model_dump())))

    def append(self, kind, value):
        seq = self.db.execute("UPDATE health_sequence SET value=value+1 WHERE id=1 RETURNING value").fetchone()[0]
        self._insert(self.node, Record(seq=seq, kind=kind, value=value))
        return seq

    def _insert(self, observer, record):
        if observer not in self.members:
            raise ValueError("Unknown observer")
        model = {"sample": Sample, "incident": Incident, "setting": Setting}[record.kind]
        value = model.model_validate(record.value).model_dump()
        if value["subject"] not in {"*", "@monitoring", "@dashboard"} and value["subject"] not in self.members:
            raise ValueError("Unknown subject")
        ts = value.get("ts", value.get("recovered") or value.get("detected", time.time()))
        if record.kind == "sample" and not 0 <= value["ts"] - value["start"] <= 750:
            raise ValueError("Invalid observation interval")
        inserted = self.db.execute("INSERT OR IGNORE INTO health_events VALUES (?,?,?,?,?)",
                                   (observer, record.seq, record.kind, ts, json.dumps(value))).rowcount
        if not inserted:
            return
        if record.kind == "setting":
            self._setting(value)
        elif record.kind == "incident":
            self.db.execute("""INSERT INTO health_incidents VALUES (?,?,?,?)
                ON CONFLICT(observer,id) DO UPDATE SET seq=excluded.seq,value=excluded.value WHERE excluded.seq>seq""",
                            (observer, value["id"], record.seq, json.dumps(value)))
        else:
            self.db.execute("""INSERT INTO health_watermarks VALUES (?,?,?)
                ON CONFLICT(observer,subject) DO UPDATE SET ts=MAX(ts,excluded.ts)""",
                            (observer, value["subject"], value["ts"]))
            self.db.execute("""INSERT INTO health_latest VALUES (?,?,?,?)
                ON CONFLICT(observer,subject) DO UPDATE SET seq=excluded.seq,value=excluded.value WHERE excluded.seq>seq""",
                (observer, value["subject"], record.seq, json.dumps(value)))
            if value["up"]:
                self.db.execute("""INSERT INTO health_last_success VALUES (?,?,?,?)
                    ON CONFLICT(observer,subject) DO UPDATE SET seq=excluded.seq,value=excluded.value WHERE excluded.seq>seq""",
                                (observer, value["subject"], record.seq, json.dumps(value)))
            self._rollup(observer, value)

    def _rollup(self, observer, sample):
        start, end = sample["start"], sample["ts"]
        while start < end:
            bucket = int(start // 300) * 300
            stop = min(end, bucket + 300)
            duration = stop - start
            up = duration if sample["up"] else 0
            down = duration - up
            gu = duration if sample["gateway"] is True else 0
            gd = duration if sample["gateway"] is False else 0
            rtt = sample["rtt"]
            self.db.execute("""INSERT INTO health_rollups VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(observer,subject,bucket) DO UPDATE SET up=up+excluded.up,down=down+excluded.down,
                gateway_up=gateway_up+excluded.gateway_up,gateway_down=gateway_down+excluded.gateway_down,
                rtt_total=rtt_total+excluded.rtt_total,rtt_count=rtt_count+excluded.rtt_count""",
                (observer, sample["subject"], bucket, up, down, gu, gd,
                 (rtt or 0) * duration, duration if rtt is not None else 0))
            start = stop

    def latest(self, observer, subject):
        row = self.db.execute("SELECT value FROM health_latest WHERE observer=? AND subject=?", (observer, subject)).fetchone()
        return json.loads(row[0]) if row else None

    def last_success(self, observer, subject):
        row = self.db.execute("SELECT value FROM health_last_success WHERE observer=? AND subject=?", (observer, subject)).fetchone()
        return json.loads(row[0]) if row else None

    def incidents(self, observer, subject=None, limit=20, offset=0):
        rows = self.db.execute("""SELECT value FROM health_incidents WHERE observer=?
            AND (? IS NULL OR json_extract(value,'$.subject')=?) ORDER BY seq DESC LIMIT ? OFFSET ?""",
            (observer, subject, subject, limit, offset))
        return [json.loads(r[0]) for r in rows]

    def export(self, after):
        rows = self.db.execute("SELECT seq,kind,value FROM health_events WHERE observer=? AND seq>? ORDER BY seq LIMIT 500",
                               (self.node, after)).fetchall()
        high = self.db.execute("SELECT value FROM health_sequence WHERE id=1").fetchone()[0]
        cursor = rows[-1][0] if len(rows) == 500 else high
        allowed = self.members | {"*", "@monitoring", "@dashboard"}
        records = [dict(seq=r[0], kind=r[1], value=json.loads(r[2])) for r in rows]
        records = [record for record in records if record["value"]["subject"] in allowed]
        gap = cursor > after and len(records) != cursor - after
        return dict(records=records, cursor=cursor, gap=gap,
                    settings=[s for s in self.settings() if s["subject"] in allowed])

    def import_page(self, observer, raw):
        page = HistoryPage.model_validate(raw)
        before = self.cursor(observer)
        if page.cursor < before or any(r.seq > page.cursor for r in page.records):
            raise ValueError("Invalid history cursor")
        if [r.seq for r in page.records] != sorted(set(r.seq for r in page.records)):
            raise ValueError("History must be strictly ordered")
        with self.db:
            for record in page.records:
                if record.seq > before:
                    self._insert(observer, record)
            for setting in page.settings:
                self._setting(setting.model_dump())
            self.db.execute("""INSERT INTO health_cursors VALUES (?,?,?) ON CONFLICT(observer)
                DO UPDATE SET seq=excluded.seq,gap=MAX(gap,excluded.gap)""", (observer, page.cursor, int(page.gap)))

    def cursor(self, observer):
        row = self.db.execute("SELECT seq FROM health_cursors WHERE observer=?", (observer,)).fetchone()
        return row[0] if row else 0

    def series(self, observer, subject, start, end):
        rows = self.db.execute("""SELECT bucket,up,down,gateway_up,gateway_down,rtt_total,rtt_count
            FROM health_rollups WHERE observer=? AND subject=? AND bucket>=? AND bucket<? ORDER BY bucket""",
            (observer, subject, int(start // 300) * 300, end)).fetchall()
        # Boundary buckets are proportional estimates; coverage always remains visible.
        result = []
        for bucket, up, down, gu, gd, rt, rc in rows:
            fraction = max(0, min(end, bucket + 300) - max(start, bucket)) / 300
            item = dict(ts=bucket, up=up*fraction, down=down*fraction,
                        gateway_up=gu*fraction, gateway_down=gd*fraction, rtt=rt/rc if rc else None)
            if fraction < 1:
                samples = self.db.execute("""SELECT value FROM health_events WHERE observer=? AND kind='sample'
                    AND ts>? AND ts<? AND json_extract(value,'$.subject')=?""", (observer, bucket, bucket+1050, subject)).fetchall()
                if samples:
                    exact_up = exact_down = exact_gu = exact_gd = 0.0
                    for (raw,) in samples:
                        sample = json.loads(raw)
                        duration = max(0, min(end, bucket+300, sample["ts"])-max(start,bucket,sample["start"]))
                        exact_up += duration if sample["up"] else 0
                        exact_down += duration if not sample["up"] else 0
                        exact_gu += duration if sample["gateway"] is True else 0
                        exact_gd += duration if sample["gateway"] is False else 0
                    item.update(up=exact_up, down=exact_down, gateway_up=exact_gu, gateway_down=exact_gd)
            result.append(item)
        return result

    def prune(self, config, now):
        # Bounded deletes avoid long pauses in the command/probe event loop.
        with self.db:
            self.db.execute("DELETE FROM health_events WHERE rowid IN (SELECT rowid FROM health_events WHERE ts<? LIMIT 5000)",
                            (now - config.health_raw_days * 86400,))
            self.db.execute("DELETE FROM health_rollups WHERE rowid IN (SELECT rowid FROM health_rollups WHERE bucket<? LIMIT 5000)",
                            (now - config.health_history_days * 86400,))
            cutoff = now - config.health_incident_days * 86400
            self.db.execute("""DELETE FROM health_incidents WHERE rowid IN (SELECT rowid FROM health_incidents
                WHERE json_extract(value,'$.recovered')<? LIMIT 5000)""", (cutoff,))
            self.db.execute("DELETE FROM health_outbox WHERE delivered<?", (cutoff,))


class PeerMonitor:
    def __init__(self, mesh):
        self.mesh, self.cfg = mesh, mesh.config
        self.store = MonitorStore(mesh.db, self.cfg.node_id, [self.cfg.node_id, *mesh.peers])
        self.runtime = {}
        self.started = time.monotonic()
        self.tasks = []
        self.alert_sink = None
        self.dashboard_sink = None
        self.dashboard_error = None
        self.apply_policy()

    def apply_policy(self):
        setting = self.store.setting(1, "@monitoring")
        if setting and setting["policy"]:
            for key, value in MonitorPolicy.model_validate(setting["policy"]).model_dump().items():
                setattr(self.cfg, key, value)

    def start(self):
        self.tasks = [asyncio.create_task(self._probes()), asyncio.create_task(self._replicate()),
                      asyncio.create_task(self._alerts()), asyncio.create_task(self._dashboards())]

    def maintenance(self, subject, now=None):
        value = self.store.setting(1, subject)
        now = time.time() if now is None else now
        return value if value and value.get("maintenance_until", 0) > now else None

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    def record(self, subject, health, reason="", *, now=None, mono=None, rtt=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        if subject not in self.runtime:
            watermark = self.store.db.execute("SELECT ts FROM health_watermarks WHERE observer=? AND subject=?",
                                               (self.cfg.node_id, subject)).fetchone()
            self.runtime[subject] = {"previous": None, "mono": mono, "states": {},
                                     "watermark": watermark[0] if watermark else now}
        rt = self.runtime[subject]
        previous = rt["previous"]
        elapsed = mono - rt["mono"]
        continuous = previous is not None and 0 < elapsed <= self.cfg.health_interval * 2.5 and abs(now-previous-elapsed) < 2
        start = min(now, max(previous, rt["watermark"])) if continuous else now
        if not continuous:
            rt["states"] = {}  # A suspended observer cannot count missed probes.
        with self.store.db:
            state = self._transition(subject, "peer", health is not None, reason, now, mono, rt)
            if health is not None:
                self._transition(subject, "discord", health["discord_connected"], "gateway disconnected", now, mono, rt)
            else:
                rt["states"].pop("discord", None)  # No gateway evidence while peer is unreachable.
            sample = Sample(subject=subject, start=start, ts=now, up=health is not None,
                            gateway=health["discord_connected"] if health else None, rtt=rtt,
                            boot=health["boot_id"] if health else None,
                            uptime=health["process_uptime_seconds"] if health else None,
                            state=state, reason=reason,
                            skew=bool(health and abs(now-health["captured_at"]) > 60))
            self.store.append("sample", sample.model_dump())
        rt["previous"], rt["mono"] = now, mono
        rt["watermark"] = max(rt["watermark"], now)

    def _transition(self, subject, kind, ok, reason, now, mono, runtime):
        state = runtime["states"].setdefault(kind, dict(failures=0, successes=0, first=mono, first_wall=now,
                                                       last_success=None, last_ok_mono=None))
        row = self.store.db.execute("""SELECT value FROM health_incidents WHERE observer=?
            AND json_extract(value,'$.subject')=? AND json_extract(value,'$.kind')=?
            AND json_extract(value,'$.recovered') IS NULL LIMIT 1""", (self.cfg.node_id, subject, kind)).fetchone()
        opened = json.loads(row[0]) if row else None
        if ok:
            state["failures"] = 0
            state["successes"] += 1
            state["last_success"] = now
            state["last_ok_mono"] = mono
            if opened:
                if state["successes"] < self.cfg.health_recovery_successes:
                    return "recovering"
                opened["recovered"] = now
                self.store.append("incident", opened)
                self._queue(opened, "recovery", now)
            return "reachable"
        state["successes"] = 0
        if state["failures"] == 0:
            state["first"], state["first_wall"] = mono, now
        state["failures"] += 1
        if opened:
            return "unreachable"
        last = self.store.last_success(self.cfg.node_id, subject)
        known = state["last_success"] is not None or (last is not None and (kind == "peer" or last["gateway"] is True))
        grace = known or mono - self.started >= self.cfg.health_startup_grace
        anchor = state["last_ok_mono"] if state["last_ok_mono"] is not None else state["first"]
        if grace and state["failures"] >= self.cfg.health_failures and mono-anchor >= self.cfg.health_down_seconds:
            incident = Incident(id=uuid.uuid4().hex, subject=subject, kind=kind, start=state["first_wall"],
                                detected=now, last_success=state["last_success"] or (last["ts"] if known and last else None),
                                reason=reason if known else "never observed reachable: " + reason)
            self.store.append("incident", incident.model_dump())
            self._queue(incident.model_dump(), "outage", now)
            return "unreachable"
        return "suspect"

    def _queue(self, incident, kind, now):
        reporters = sorted(n for n in self.store.members if n != incident["subject"])
        if self.cfg.node_id == incident["subject"]:
            return  # Other nodes report this node's loss; no self-offline announcements.
        rank = reporters.index(self.cfg.node_id)
        for setting in self.store.settings():
            if setting["subject"] != "*" or not setting["enabled"] or not setting["channel"]:
                continue
            key = f"{incident['id']}:{setting['guild']}:{kind}"
            self.store.db.execute("INSERT OR IGNORE INTO health_outbox(id,incident,guild,kind,due) VALUES (?,?,?,?,?)",
                                  (key, incident["id"], setting["guild"], kind, now + rank*15))
            if kind == "recovery" and self.maintenance(incident["subject"], now):
                self.store.db.execute("UPDATE health_outbox SET delivered=?,message='maintenance' WHERE incident=? AND delivered IS NULL",
                                      (now, incident["id"]))

    async def probe(self, target):
        from mitra_bot.services.peer_service import Health, PeerError
        started = time.monotonic()
        try:
            if target == self.cfg.node_id:
                health = self.mesh.local_health()
            else:
                health = Health.model_validate(await self.mesh.request(target, "health", {}, timeout=self.cfg.health_timeout)).model_dump()
            if health["node_id"] != target:
                raise ValueError("Health identity mismatch")
            rtt = (time.monotonic() - started) * 1000
            self.mesh.health[target], self.mesh.online[target] = health, True
            self.record(target, health, rtt=rtt)
        except (PeerError, ValueError, OSError) as exc:
            cause = exc.__cause__ or exc
            reason = ("certificate validation failed" if isinstance(cause, ssl.SSLCertVerificationError)
                      else "TLS authentication failed" if isinstance(cause, ssl.SSLError)
                      else "probe timed out" if isinstance(cause, TimeoutError)
                      else "connection failed" if isinstance(cause, OSError)
                      else "identity or protocol rejected")
            self.mesh.online[target] = False
            self.record(target, None, reason)

    async def _probes(self):
        semaphore = asyncio.Semaphore(8)
        async def loop(target):
            while True:
                self.apply_policy()
                began = time.monotonic()
                try:
                    async with semaphore:
                        await self.probe(target)
                except Exception:
                    logging.exception("Peer health collection failed for %s", target)
                await asyncio.sleep(max(0.1, self.cfg.health_interval - (time.monotonic()-began)))
        await asyncio.gather(*(loop(target) for target in sorted(self.store.members)))

    async def replicate_once(self, target):
        # Bound catch-up work per turn; direct observer ownership is authenticated by TLS.
        for _ in range(4):
            before = self.store.cursor(target)
            page = await self.mesh.request(target, "history", {"after": before})
            self.store.import_page(target, page)
            if len(page["records"]) < 500:
                break

    async def _replicate(self):
        semaphore = asyncio.Semaphore(4)
        async def sync(target):
            async with semaphore:
                try:
                    await self.replicate_once(target)
                except Exception:
                    logging.debug("Health history replication pending for %s", target, exc_info=True)
        while True:
            await asyncio.gather(*(sync(target) for target in self.mesh.peers))
            self.store.prune(self.cfg, time.time())
            await asyncio.sleep(15)

    async def flush_alerts(self):
        if self.alert_sink is None:
            return
        now = time.time()
        rows = self.store.db.execute("""SELECT id,incident,guild,kind,attempts FROM health_outbox
            WHERE delivered IS NULL AND due<=? ORDER BY due LIMIT 20""", (now,)).fetchall()
        for key, incident_id, guild, kind, attempts in rows:
            if kind == "recovery" and self.store.db.execute(
                "SELECT 1 FROM health_outbox WHERE incident=? AND guild=? AND kind='outage' AND delivered IS NULL",
                (incident_id, guild)).fetchone():
                with self.store.db:
                    self.store.db.execute("UPDATE health_outbox SET due=? WHERE id=?", (now+15,key))
                continue  # Deliver a delayed summary first, never recovery before the outage.
            row = self.store.db.execute("SELECT value FROM health_incidents WHERE observer=? AND id=?",
                                        (self.cfg.node_id, incident_id)).fetchone()
            if not row:
                continue
            incident = json.loads(row[0])
            if self.maintenance(incident["subject"], now):
                with self.store.db:
                    self.store.db.execute("UPDATE health_outbox SET due=? WHERE id=?", (now+5,key))
                continue
            setting = self.store.setting(guild, "*")
            role = self.store.setting(guild, incident["subject"])
            try:
                if incident["subject"] not in self.store.members or not setting or not setting["enabled"]:
                    message = "disabled"
                elif kind == "recovery" and self.store.db.execute(
                    "SELECT 1 FROM health_outbox WHERE incident=? AND guild=? AND kind='outage' AND message LIKE 'summary:%'",
                    (incident_id, guild)).fetchone():
                    message = "included in summary"
                else:
                    message = await self.alert_sink(key, kind, incident, setting, role)
                    if kind == "outage" and incident["recovered"] is not None:
                        message = "summary:" + str(message)
                with self.store.db:
                    self.store.db.execute("UPDATE health_outbox SET delivered=?,message=?,error=NULL WHERE id=?",
                                          (time.time(), str(message), key))
            except Exception as exc:
                with self.store.db:
                    self.store.db.execute("UPDATE health_outbox SET attempts=attempts+1,due=?,error=? WHERE id=?",
                        (time.time()+min(3600, 15*2**min(attempts,8)), type(exc).__name__, key))
                logging.warning("Peer alert delivery pending: %s", type(exc).__name__, exc_info=True)

    async def _alerts(self):
        while True:
            try:
                await self.flush_alerts()
            except Exception:
                logging.exception("Peer alert outbox failed")
            await asyncio.sleep(5)

    async def _dashboards(self):
        while True:
            if self.dashboard_sink:
                try:
                    await self.dashboard_sink()
                    self.dashboard_error = None
                except Exception as exc:
                    self.dashboard_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    logging.warning("Shared dashboard refresh pending: %s", type(exc).__name__)
            await asyncio.sleep(10)
