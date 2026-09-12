"""Direct mutually authenticated TLS RPC. No broker or elected coordinator.

Only configured certificate identities can read telemetry. Power permission is
granted separately. Destructive requests are journaled BEFORE execution and are
never retried automatically, including after a process restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import ssl
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mitra_bot.peer_config import Peer, PeerConfig

MAX_FRAME = 4 * 1024 * 1024
TIMEOUT = 10


class PeerError(RuntimeError):
    pass


class PowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["restart", "shutdown", "cancel"]
    delay_seconds: int = Field(default=0, ge=0, le=86400)
    force: bool = False
    requester_id: str = Field(pattern=r"^[0-9]{1,20}$")
    confirmer_id: str = Field(pattern=r"^[0-9]{1,20}$")
    decision: Literal["execute", "cancel"] = "execute"


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel_id: int | None = Field(default=None, ge=1)
    user_id: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1, max_length=1900)
    mention_ip_subscribers: bool = False


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    network_id: str
    source: str
    target: str
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    issued_at: int
    application_key: str = ""
    operation: Literal["snapshot", "power", "health", "notify", "history", "monitor_settings"]
    payload: dict[str, Any]


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    captured_at: int
    available: bool
    live: dict[str, Any]
    rows: list[dict[str, Any]] = Field(max_length=5000)
    timezone: str
    enabled: bool
    poll_seconds: int


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    boot_id: str
    captured_at: int
    process_uptime_seconds: int = Field(ge=0)
    discord_connected: bool
    protocol_version: Literal[1] = 1
    sample_sequence: int = Field(default=0, ge=0)
    last_discord_connected_at: int | None = None


async def read_frame(reader: asyncio.StreamReader) -> dict:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise PeerError("Invalid peer frame size")
    result = json.loads(await reader.readexactly(size))
    if not isinstance(result, dict):
        raise PeerError("Invalid peer message")
    return result


async def write_frame(writer: asyncio.StreamWriter, value: dict) -> None:
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise PeerError("Peer response exceeds size limit")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()


def certificate_fingerprint(writer: asyncio.StreamWriter) -> str:
    obj = writer.get_extra_info("ssl_object")
    if obj is None:
        raise PeerError("TLS required")
    return hashlib.sha256(obj.getpeercert(binary_form=True)).hexdigest()


class PeerService:
    def __init__(
        self, config: PeerConfig, *,
        snapshot: Callable[[], dict],
        power: Callable[[PowerRequest], Awaitable[str]],
    ) -> None:
        self.config = config
        self.snapshot_provider = snapshot
        self.power_executor = power
        self.peers = {p.node_id: p for p in config.peers}
        self.online: dict[str, bool] = {}
        self.server = None
        self.poll_task = None
        self.handlers: set[asyncio.Task] = set()
        self.db = None
        self.client_ssl = None
        self.notification_sink = None
        self.application_key = ""
        self.signing_key = b""
        self.health_provider = lambda: {"discord_connected": False}
        self.boot_id = uuid.uuid4().hex
        self.started_at = time.monotonic()
        self.health: dict[str, dict] = {}
        self.power_lock = asyncio.Lock()
        self.monitor = None
        self.monitor_alert_sink = None
        self.monitor_dashboard_sink = None
        self.peer_certificate_expiry = {}
        self.health_sequence = 0
        self.last_discord_connected_at = None
        self.was_discord_connected = False

    @property
    def is_state_owner(self):
        return self.config.node_id == self.config.resolved_state_owner

    def configure_discord_identity(self, token):
        self.signing_key = hashlib.sha256((self.config.network_id + ":power:" + token).encode()).digest()
        self.application_key = hashlib.sha256((self.config.network_id + ":identity:" + token).encode()).hexdigest()

    def resolve(self, server: str | None) -> str:
        if server is None:
            return self.config.resolved_state_owner
        if server != self.config.node_id and server not in self.peers:
            raise PeerError(f"Unknown server '{server}'. Use /servers list.")
        return server

    def _tls_contexts(self):
        cfg = self.config
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.verify_mode = ssl.CERT_REQUIRED
        server.load_verify_locations(cafile=cfg.ca_file)
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Identity is pinned to a configured certificate before sending any data.
        # CA validation, expiry, and key possession are still checked by TLS.
        client.check_hostname = False
        client.load_verify_locations(cafile=cfg.ca_file)
        for ctx in (server, client):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
        return server, client

    async def start(self) -> None:
        server_ssl, self.client_ssl = self._tls_contexts()
        Path(self.config.state_file).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.config.state_file)
        self.db.execute("CREATE TABLE IF NOT EXISTS power_operations (id TEXT PRIMARY KEY, digest TEXT, result TEXT, created INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots (node TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS identity (network TEXT, node TEXT)")
        identity = self.db.execute("SELECT network, node FROM identity").fetchone()
        if identity and identity != (self.config.network_id, self.config.node_id):
            self.db.close()
            self.db = None
            raise PeerError("Peer database belongs to another network or node; configure a new state_file")
        if not identity:
            self.db.execute("INSERT INTO identity VALUES (?, ?)", (self.config.network_id, self.config.node_id))
        self.db.commit()
        try:
            self.server = await asyncio.start_server(
                self._accept, self.config.listen_host, self.config.listen_port,
                ssl=server_ssl, ssl_handshake_timeout=TIMEOUT,
            )
        except BaseException:
            self.db.close()
            self.db = None
            raise
        from mitra_bot.services.peer_monitor import PeerMonitor
        self.monitor = PeerMonitor(self)
        self.monitor.alert_sink = self.monitor_alert_sink
        self.monitor.dashboard_sink = self.monitor_dashboard_sink
        self.monitor.start()
        self.poll_task = asyncio.create_task(self._poll())

    async def close(self) -> None:
        if self.monitor:
            await self.monitor.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        # Let in-flight power operations finish journaling before closing storage.
        if self.handlers:
            await asyncio.gather(*list(self.handlers), return_exceptions=True)
        if self.db:
            self.db.close()
            self.db = None

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if len(self.handlers) >= 32:
            writer.close()
            return
        self.handlers.add(task)
        try:
            fingerprint = certificate_fingerprint(writer)
            peer = next((p for p in self.peers.values() if p.fingerprint == fingerprint), None)
            if peer is None:
                raise PeerError("Unapproved certificate")
            raw = await asyncio.wait_for(read_frame(reader), TIMEOUT)
            result = await self._dispatch(peer, raw)
            await asyncio.wait_for(write_frame(writer, {"ok": True, "result": result}), TIMEOUT)
        except (PeerError, ValidationError, ValueError) as exc:
            logging.warning("Peer request rejected: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(write_frame(writer, {"ok": False, "error": "Peer request rejected"}), TIMEOUT)
            except Exception:
                pass
        except Exception:
            logging.debug("Peer connection failed", exc_info=True)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass
            self.handlers.discard(task)

    async def _dispatch(self, peer: Peer, raw: dict) -> dict:
        msg = Envelope.model_validate(raw)
        if (msg.network_id != self.config.network_id or msg.application_key != self.application_key or msg.source != peer.node_id
                or msg.target != self.config.node_id or abs(time.time() - msg.issued_at) > 60):
            raise PeerError("Wrong network, identity, target, or expired request")
        if msg.operation == "health":
            if msg.payload:
                raise PeerError("Unexpected health parameters")
            return self.local_health()
        if msg.operation == "history":
            if set(msg.payload) != {"after"} or type(msg.payload["after"]) is not int or msg.payload["after"] < 0:
                raise PeerError("Invalid history cursor")
            return self.monitor.store.export(msg.payload["after"])
        if msg.operation == "monitor_settings":
            from mitra_bot.services.peer_monitor import Setting
            setting = Setting.model_validate(msg.payload)
            with self.db:
                self.monitor.store._setting(setting.model_dump())
            return {"saved": True}
        if msg.operation == "notify":
            if self.notification_sink is None:
                raise PeerError("Notifications are unavailable")
            notification = Notification.model_validate(msg.payload)
            await self.notification_sink(peer.node_id, notification)
            return {"delivered": True}
        if msg.operation == "snapshot":
            if msg.payload:
                raise PeerError("Unexpected snapshot parameters")
            return await self._local_snapshot()
        if not peer.allow_power:
            raise PeerError("Peer has no power permission")
        power = PowerRequest.model_validate(msg.payload)
        return await self.execute_local_power(msg.request_id, power, source=peer.node_id)

    async def execute_local_power(self, operation_id: str, request: PowerRequest, *, source=None) -> dict:
        """One durable operation at the target, regardless of which gateway forwards it."""
        digest = hashlib.sha256(json.dumps(request.model_dump(exclude={"confirmer_id", "decision"}), sort_keys=True).encode()).hexdigest()
        async with self.power_lock:
            row = self.db.execute("SELECT digest, result FROM power_operations WHERE id=?", (operation_id,)).fetchone()
            previous = json.loads(row[1]) if row else None
            if row and row[0] != digest:
                raise PeerError("Request ID reused with different contents")
            if previous and (request.decision == "execute" or previous["status"] != "executed"):
                return previous
            result = {"status": "unknown", "message": "Execution may have occurred; inspect target before issuing another command."}
            effective = request
            if request.decision == "cancel":
                if previous is None:
                    result = {"status": "canceled", "message": "Unconfirmed power action canceled; no OS action was run."}
                else:
                    effective = request.model_copy(update={"action": "cancel", "delay_seconds": 0, "force": False})
            # Commit before any OS side effect. Keep IDs across boots and gateways.
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO power_operations VALUES (?, ?, ?, ?)",
                                (operation_id, digest, json.dumps(result), int(time.time())))
            logging.warning("Power target=%s source=%s action=%s decision=%s requester=%s confirmer=%s request=%s",
                            self.config.node_id, source or self.config.node_id, request.action, request.decision,
                            request.requester_id, request.confirmer_id, operation_id)
            if result["status"] == "canceled":
                return result
            try:
                message = await self.power_executor(effective)
                result = {"status": "canceled" if request.decision == "cancel" else "executed", "message": message}
            except Exception:
                logging.exception("Power execution could not be confirmed")
            with self.db:
                self.db.execute("UPDATE power_operations SET result=? WHERE id=?", (json.dumps(result), operation_id))
            return result

    async def request(self, target: str, operation: str, payload: dict, *, timeout: float = TIMEOUT, request_id: str | None = None) -> dict:
        peer = self.peers.get(target)
        if peer is None:
            raise PeerError("Unknown remote server")
        msg = dict(version=1, network_id=self.config.network_id,
                   source=self.config.node_id, target=target, request_id=request_id or uuid.uuid4().hex,
                   application_key=self.application_key,
                   issued_at=int(time.time()), operation=operation, payload=payload)

        async def exchange():
            reader, writer = await asyncio.open_connection(peer.host, peer.port, ssl=self.client_ssl, server_hostname=peer.host)
            try:
                if certificate_fingerprint(writer) != peer.fingerprint:
                    raise PeerError("Remote certificate does not match configured server")
                certificate = writer.get_extra_info("ssl_object").getpeercert()
                if certificate.get("notAfter"):
                    self.peer_certificate_expiry[target] = ssl.cert_time_to_seconds(certificate["notAfter"])
                await write_frame(writer, msg)
                response = await read_frame(reader)
                if response.get("ok") is not True or not isinstance(response.get("result"), dict):
                    raise PeerError("Peer rejected the request; check membership and power permissions.")
                return response["result"]
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except Exception:
                    pass
        try:
            return await asyncio.wait_for(exchange(), timeout)
        except PeerError:
            raise
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:
            message = f"Server '{target}' is unreachable or returned an invalid response."
            if operation == "power":
                message += " Outcome unknown; inspect the target before sending another power command."
            raise PeerError(message) from exc

    async def power(self, target: str, request: PowerRequest, *, operation_id: str) -> str:
        self.resolve(target)
        if target == self.config.node_id:
            result = await self.execute_local_power(operation_id, request)
        else:
            result = await self.request(target, "power", request.model_dump(), request_id=operation_id)
        expected = "canceled" if request.decision == "cancel" else "executed"
        if result.get("status") != expected:
            raise PeerError(result.get("message", "Power outcome unknown"))
        return str(result["message"])

    async def notify(self, notification) -> bool:
        try:
            if self.notification_sink:
                await self.notification_sink(self.config.node_id, notification)
                return True
        except Exception:
            # Do not resend an ambiguous delivery through another node: that can ping twice.
            logging.warning("Could not deliver this node's notification", exc_info=True)
        return False

    def local_health(self):
        status = self.health_provider()
        self.health_sequence += 1
        if status["discord_connected"] and not self.was_discord_connected:
            self.last_discord_connected_at = int(time.time())
        self.was_discord_connected = status["discord_connected"]
        return Health(node_id=self.config.node_id, boot_id=self.boot_id, captured_at=int(time.time()),
                      process_uptime_seconds=int(time.monotonic() - self.started_at),
                      sample_sequence=self.health_sequence, last_discord_connected_at=self.last_discord_connected_at,
                      **status).model_dump()

    async def refresh_health(self, target):
        try:
            data = Health.model_validate(await self.request(target, "health", {}, timeout=3)).model_dump()
            if data["node_id"] != target or abs(time.time() - data["captured_at"]) > 60:
                raise PeerError("Invalid health identity or timestamp")
            self.health[target] = data
            self.online[target] = True
            return data
        except (PeerError, ValidationError):
            self.online[target] = False
            return None

    async def snapshot(self, target: str) -> tuple[dict, bool]:
        self.resolve(target)
        if target == self.config.node_id:
            return await self._local_snapshot(), False
        cache_key = target + ":" + self.peers[target].fingerprint
        try:
            data = Snapshot.model_validate(await self.request(target, "snapshot", {})).model_dump()
            if data["node_id"] != target or abs(time.time() - data["captured_at"]) > 60:
                raise PeerError("Peer returned a mismatched or stale snapshot")
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)", (cache_key, json.dumps(data)))
            return data, False
        except (PeerError, ValidationError):
            row = self.db.execute("SELECT value FROM snapshots WHERE node=?", (cache_key,)).fetchone()
            if row:
                return json.loads(row[0]), True
            raise PeerError(f"Server '{target}' is unavailable and has no cached UPS history.")

    async def _local_snapshot(self):
        # Slow USB reads must not block health probes or Discord interactions.
        data = await asyncio.to_thread(self.snapshot_provider)
        return Snapshot.model_validate(data).model_dump()

    async def _poll(self):
        while True:
            # Bound concurrent connections even for larger configured meshes.
            semaphore = asyncio.Semaphore(8)
            async def refresh(target):
                async with semaphore:
                    try:
                        await self.snapshot(target)
                    except Exception:
                        logging.debug("Could not refresh peer %s", target, exc_info=True)
            await asyncio.gather(*(refresh(target) for target in self.peers))
            await asyncio.sleep(self.config.poll_seconds)
