from __future__ import annotations

import asyncio
import ssl
import struct
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mitra_bot.init_peers import find_openssl, provision
from mitra_bot.peer_config import PeerConfig, load_peer_config
from mitra_bot.services.peer_service import MAX_FRAME, Notification, PeerError, PeerService, PowerRequest, read_frame, write_frame
from mitra_bot.storage.config_store import tomllib


@pytest.fixture(scope="session")
def bundles(tmp_path_factory):
    try:
        find_openssl()
    except RuntimeError:
        pytest.skip("OpenSSL required for real TLS tests")
    folder = tmp_path_factory.mktemp("certs") / "bundles"
    provision(folder, {"a": "127.0.0.1", "b": "127.0.0.1", "c": "127.0.0.1"}, allow_power=True)
    return folder


def snapshot(node):
    return dict(node_id=node, captured_at=int(time.time()), available=True, live={"battery_percent": 80},
                rows=[{"ts": "2026-09-08T00:00:00Z", "battery_percent": 80}],
                timezone="UTC", enabled=True, poll_seconds=30)


def config(bundles, tmp_path, node):
    folder = bundles / node
    cfg = PeerConfig.model_validate(tomllib.loads((folder / "peer-network.toml").read_text()))
    for name in ("ca_file", "cert_file", "key_file"):
        setattr(cfg, name, str(folder / getattr(cfg, name)))
    cfg.state_file = str(tmp_path / f"{node}.db")
    cfg.listen_host = "127.0.0.1"
    cfg.listen_port = 0  # OS-assigned ephemeral test port, not a deployable configuration.
    return cfg


@asynccontextmanager
async def mesh(bundles, tmp_path):
    services = {}
    async def no_poll():
        await asyncio.Event().wait()
    try:
        for node in ("a", "b", "c"):
            svc = PeerService(config(bundles, tmp_path, node), snapshot=lambda n=node: snapshot(n),
                              power=AsyncMock(return_value="scheduled"))
            svc._poll = no_poll
            await svc.start()
            services[node] = svc
        for svc in services.values():
            for peer in svc.peers.values():
                peer.port = services[peer.node_id].server.sockets[0].getsockname()[1]
        yield services
    finally:
        for svc in services.values():
            await svc.close()


def test_optional_and_invalid_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MITRA_PEER_CONFIG_PATH", raising=False)
    assert not load_peer_config().enabled
    monkeypatch.setenv("MITRA_PEER_CONFIG_PATH", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        load_peer_config()
    with pytest.raises(ValidationError):
        PeerConfig(enabled=True)


def test_uptime_replication_and_subscription_settings_over_real_tls(bundles, tmp_path):
    from mitra_bot.services.peer_monitor import Setting
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b, c = (nodes[n] for n in "abc")
            for service in nodes.values():
                await service.monitor.close()
            a.peers["b"].allow_power = False
            now = time.time()
            sample = a.local_health()
            a.monitor.record("a", sample, now=now, mono=100.0, rtt=0.0)
            a.monitor.record("a", sample, now=now+10, mono=110.0, rtt=0.0)
            setting = Setting(revision="1234", guild=123, subject="a", role=789)
            with a.db:
                a.monitor.store.append("setting", setting.model_dump())
            await b.monitor.replicate_once("a")
            await c.monitor.replicate_once("a")
            assert b.peer_certificate_expiry["a"] > time.time()
            assert b.monitor.store.last_success("a", "a")
            assert b.monitor.store.setting(123,"a")["role"] == 789
            assert (await b.request("a", "monitor_settings", Setting(revision="1235",guild=123,subject="*",channel=456).model_dump()))["saved"]
            with pytest.raises(PeerError):
                await b.request("a", "history", {"after":-1})
            with pytest.raises(PeerError):
                await b.request("a", "monitor_settings", {**setting.model_dump(), "subject":"intruder"})
            await a.close()
            rows = b.monitor.store.series("a","a",now-300,now+300)
            assert sum(r["up"] for r in rows) > 0
            assert c.monitor.store.last_success("a","a") is not None
            assert b.power_executor.await_count == c.power_executor.await_count == 0
    asyncio.run(run())


def test_health_collection_is_independent_of_blocked_ups_snapshot(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a,b = nodes["a"],nodes["b"]
            for service in nodes.values():
                await service.monitor.close()
            started = asyncio.Event()
            release = asyncio.Event()
            async def slow_snapshot():
                started.set()
                await release.wait()
                return snapshot("a")
            a._local_snapshot = slow_snapshot
            pending = asyncio.create_task(b.snapshot("a"))
            try:
                await started.wait()
                await asyncio.wait_for(b.monitor.probe("a"), timeout=2)
                assert b.monitor.store.latest("b","a")["up"]
                assert not pending.done()
            finally:
                release.set()
                await pending
    asyncio.run(run())


def test_roundtrip_isolation_and_offline_cache(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b, c = (nodes[n] for n in "abc")
            assert (await a.snapshot("b"))[0]["node_id"] == "b"
            await b.close()
            data, stale = await a.snapshot("b")
            assert stale and data["node_id"] == "b" and data["live"]["battery_percent"] == 80
            assert (await a.snapshot("c"))[0]["node_id"] == "c"
            assert not (await c.snapshot("a"))[1]
            await a.close()
            await a.start()
            assert (await a.snapshot("b"))[1]  # Cache survived process/service restart.
    asyncio.run(run())


def test_targeted_power_and_read_only_permission(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b, c = (nodes[n] for n in "abc")
            req = PowerRequest(action="restart", delay_seconds=60, requester_id="1", confirmer_id="2")
            assert await a.power("b", req, operation_id=uuid.uuid4().hex) == "scheduled"
            b.power_executor.assert_awaited_once_with(req)
            a.power_executor.assert_not_awaited()
            c.power_executor.assert_not_awaited()
            b.peers["a"].allow_power = False
            with pytest.raises(PeerError, match="rejected"):
                await a.power("b", req, operation_id=uuid.uuid4().hex)
            assert not (await a.snapshot("b"))[1]
            assert b.power_executor.await_count == 1
    asyncio.run(run())


def test_wrong_certificate_network_and_identity_rejected(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b = nodes["a"], nodes["b"]
            a.peers["b"].fingerprint = "0" * 64
            with pytest.raises(PeerError, match="certificate"):
                await a.request("b", "snapshot", {})
            a.peers["b"].fingerprint = nodes["c"].peers["b"].fingerprint
            a.config.network_id = "other-network"
            with pytest.raises(PeerError, match="rejected"):
                await a.request("b", "snapshot", {})
            a.config.network_id = b.config.network_id
            a.config.node_id = "c"
            with pytest.raises(PeerError, match="rejected"):
                await a.request("b", "snapshot", {})
            a.config.node_id = "a"
            del b.peers["a"]
            with pytest.raises(PeerError):
                await a.request("b", "snapshot", {})
            b.power_executor.assert_not_awaited()
    asyncio.run(run())


def envelope(source="a", target="b", network="test"):
    return dict(version=1, network_id=network, source=source, target=target,
                request_id=uuid.uuid4().hex, issued_at=int(time.time()), operation="power",
                payload=dict(action="restart", delay_seconds=60, force=False, requester_id="1", confirmer_id="2"))


def test_power_deduplicated_across_restart_and_replay_rejected(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            b = nodes["b"]
            msg = envelope(network=b.config.network_id)
            first = await b._dispatch(b.peers["a"], msg)
            assert await b._dispatch(b.peers["a"], msg) == first
            await b.close()
            await b.start()
            assert await b._dispatch(b.peers["a"], msg) == first
            b.power_executor.assert_awaited_once()
            msg["payload"]["action"] = "shutdown"
            with pytest.raises(PeerError, match="reused"):
                await b._dispatch(b.peers["a"], msg)
            msg["issued_at"] -= 120
            with pytest.raises(PeerError, match="expired"):
                await b._dispatch(b.peers["a"], msg)
    asyncio.run(run())


def test_uncertain_journal_does_not_execute_again(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            b = nodes["b"]
            msg = envelope(network=b.config.network_id)
            b.power_executor.side_effect = asyncio.CancelledError
            with pytest.raises(asyncio.CancelledError):
                await b._dispatch(b.peers["a"], msg)
            await b.close()
            await b.start()
            result = await b._dispatch(b.peers["a"], msg)
            assert result["status"] == "unknown"
            b.power_executor.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("change", [dict(target="c"), dict(version=2), dict(operation="shell"),
                                    dict(payload={"action": "restart", "delay_seconds": -1}),
                                    dict(payload={"action": "restart", "force": "false"})])
def test_invalid_power_never_executes(bundles, tmp_path, change):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            b = nodes["b"]
            msg = envelope(network=b.config.network_id)
            msg.update(change)
            with pytest.raises((PeerError, ValidationError)):
                await b._dispatch(b.peers["a"], msg)
            b.power_executor.assert_not_awaited()
    asyncio.run(run())


def test_missing_client_certificate_cannot_read(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            b = nodes["b"]
            ctx = ssl.create_default_context(cafile=b.config.ca_file)
            ctx.check_hostname = False
            writer = None
            try:
                with pytest.raises((OSError, asyncio.IncompleteReadError, ConnectionError)):
                    reader, writer = await asyncio.open_connection("127.0.0.1", b.server.sockets[0].getsockname()[1], ssl=ctx)
                    await write_frame(writer, {})
                    await asyncio.wait_for(read_frame(reader), 3)
            finally:
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
    asyncio.run(run())


def test_oversize_frame_rejected_before_body():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(PeerError, match="size"):
            await read_frame(reader)
    asyncio.run(run())


def test_state_cannot_cross_networks(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a = nodes["a"]
            await a.close()
            a.config.network_id = "other-network"
            with pytest.raises(PeerError, match="database"):
                await a.start()
    asyncio.run(run())


def test_same_power_id_from_different_gateways_and_local_target_executes_once(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b, c = (nodes[n] for n in "abc")
            req = PowerRequest(action="restart", delay_seconds=60, requester_id="1", confirmer_id="2")
            operation = uuid.uuid4().hex
            results = await asyncio.gather(*(s.power("b", req, operation_id=operation) for s in (a, b, c)))
            assert results == ["scheduled"] * 3
            b.power_executor.assert_awaited_once()
            a.power_executor.assert_not_awaited()
            c.power_executor.assert_not_awaited()
            await b.close()
            await b.start()
            c.peers["b"].port = b.server.sockets[0].getsockname()[1]
            await c.power("b", req, operation_id=operation)
            b.power_executor.assert_awaited_once()
    asyncio.run(run())


def test_cancel_before_confirmation_prevents_later_execution(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            req = PowerRequest(action="restart", requester_id="1", confirmer_id="2")
            operation = uuid.uuid4().hex
            await nodes["a"].power("b", req.model_copy(update={"decision": "cancel"}), operation_id=operation)
            with pytest.raises(PeerError, match="canceled"):
                await nodes["c"].power("b", req, operation_id=operation)
            nodes["b"].power_executor.assert_not_awaited()
    asyncio.run(run())


def test_confirm_and_cancel_are_each_executed_at_most_once(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            req = PowerRequest(action="shutdown", delay_seconds=60, requester_id="1", confirmer_id="2")
            operation = uuid.uuid4().hex
            await nodes["a"].power("b", req, operation_id=operation)
            canceled = req.model_copy(update={"decision": "cancel", "confirmer_id": "3"})
            await nodes["c"].power("b", canceled, operation_id=operation)
            await nodes["a"].power("b", canceled, operation_id=operation)
            assert [call.args[0].action for call in nodes["b"].power_executor.await_args_list] == ["shutdown", "cancel"]
    asyncio.run(run())


def test_health_and_notifications_work_without_other_nodes_or_a_quorum(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b, c = (nodes[n] for n in "abc")
            health = await b.refresh_health("a")
            assert health["node_id"] == "a" and health["process_uptime_seconds"] >= 0
            await a.close()
            await c.close()
            assert await b.refresh_health("a") is None
            assert b.online["a"] is False
            b.notification_sink = AsyncMock()
            notification = Notification(channel_id=123, message="Local UPS event")
            assert await b.notify(notification)
            b.notification_sink.assert_awaited_once_with("b", notification)
    asyncio.run(run())


def test_different_discord_identity_cannot_issue_peer_commands(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            nodes["a"].configure_discord_identity("token-a")
            nodes["b"].configure_discord_identity("token-b")
            with pytest.raises(PeerError, match="rejected"):
                await nodes["a"].request("b", "health", {})
    asyncio.run(run())



def test_only_state_owner_can_initiate_updates_over_real_tls(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            target = nodes["b"]
            target.config.state_owner = "a"
            target.update_rpc = AsyncMock(return_value={"accepted": True})
            assert await nodes["a"].request("b", "update_install", {"job": "a"*32, "version": "1.0.0"}) == {"accepted": True}
            target.update_rpc.reset_mock()
            with pytest.raises(PeerError, match="rejected"):
                await nodes["c"].request("b", "update_install", {"job": "c"*32, "version": "1.0.0"})
            target.update_rpc.assert_not_awaited()
            await nodes["c"].request("b", "update_status", {})
            target.update_rpc.assert_awaited_once_with("update_status", {})
    asyncio.run(run())


def test_node_commands_and_settings_owner_permission_over_real_tls(bundles, tmp_path):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            target = nodes["b"]
            target.config.state_owner = "a"
            target.node_rpc = AsyncMock(return_value={"message": "saved"})
            await nodes["a"].request("b", "ups_settings", {"enabled": False})
            target.node_rpc.assert_awaited_once_with("ups_settings", {"enabled": False})
            target.node_rpc.reset_mock()
            with pytest.raises(PeerError, match="rejected"):
                await nodes["c"].request("b", "ups_settings", {"enabled": True})
            target.node_rpc.assert_not_awaited()
            await nodes["c"].request("b", "node_info", {})
            target.node_rpc.assert_awaited_once_with("node_info", {})
            del target.node_rpc
            with pytest.raises(PeerError, match="rejected"):
                await nodes["a"].request("b", "public_ip", {})
    asyncio.run(run())


def test_membership_add_remove_and_apply_preserve_existing_identity(bundles, tmp_path):
    from mitra_bot.peer_membership import prepare, apply_membership
    import shutil
    added = tmp_path / "added"
    assert prepare(bundles, added, add="d=127.0.0.1") == ["a", "b", "c", "d"]
    assert not (added/"OFFLINE-CA.key").exists()
    for node in "abc":
        assert (added/node/"node.key").read_bytes() == (bundles/node/"node.key").read_bytes()
        cfg = PeerConfig.model_validate(tomllib.loads((added/node/"peer-network.toml").read_text()))
        assert any(p.node_id == "d" and not p.allow_power for p in cfg.peers)
    local = tmp_path/"live"
    shutil.copytree(bundles/"b", local)
    before = (local/"peer-network.toml").read_text()
    backup = apply_membership(added/"b"/"peer-network.toml", local/"peer-network.toml")
    assert backup.read_text() == before
    assert 'node_id = "d"' in (local/"peer-network.toml").read_text()
    with pytest.raises(ValueError, match="another"):
        apply_membership(added/"a"/"peer-network.toml", local/"peer-network.toml")
    removed = tmp_path/"removed"
    assert prepare(added, removed, remove="d") == ["a", "b", "c"]
    assert not (removed/"d").exists()
    with pytest.raises(ValueError, match="state owner"):
        prepare(added, tmp_path/"bad", remove="a")


def test_hostname_reconnects_after_dns_address_changes_without_process_restart(bundles, tmp_path, monkeypatch):
    async def run():
        async with mesh(bundles, tmp_path) as nodes:
            a, b = nodes["a"], nodes["b"]
            loop = asyncio.get_running_loop()
            original = loop.getaddrinfo
            address, queries = ["127.0.0.1"], []
            async def resolve(host, port, *args, **kwargs):
                if host == "dynamic-peer.example":
                    queries.append(address[0])
                    host = address[0]
                return await original(host, port, *args, **kwargs)
            monkeypatch.setattr(loop, "getaddrinfo", resolve)
            a.peers["b"].host = "dynamic-peer.example"
            assert (await a.request("b", "health", {}))["node_id"] == "b"
            old_boot = b.boot_id
            port = b.server.sockets[0].getsockname()[1]
            b.server.close()
            await b.server.wait_closed()
            server_ssl, _ = b._tls_contexts()
            b.server = await asyncio.start_server(b._accept, "127.0.0.2", port, ssl=server_ssl)
            address[0] = "127.0.0.2"
            health = await a.request("b", "health", {})
            assert health["boot_id"] == old_boot
            assert queries == ["127.0.0.1", "127.0.0.2"]
    asyncio.run(run())
