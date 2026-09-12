from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from mitra_bot.peer_config import PeerConfig
from mitra_bot.services.peer_monitor import PeerMonitor, MonitorStore, Setting, MonitorPolicy
from mitra_bot.services.peer_graph import metrics, render_history, _compact
from mitra_bot.services.peer_alerts import PeerAlertDelivery
from mitra_bot.discord_app.cogs.servers_cog import ServersCog
from mitra_bot.discord_app.peer_dashboard import build_dashboard, dashboard_view, handle_dashboard_component


def monitor(path=":memory:", node="b"):
    config = PeerConfig(node_id=node, network_id="test")
    db = sqlite3.connect(path)
    mesh = SimpleNamespace(config=config, db=db, peers={n:None for n in "abc" if n != node}, health={}, online={},
                           request=AsyncMock())
    mesh.resolve = lambda target: target if target in "abc" else (_ for _ in ()).throw(ValueError("Unknown server"))
    value = PeerMonitor(mesh)
    mesh.monitor = value
    value.started = 0
    return value


def health(node="a", now=1000, gateway=True, boot="boot1"):
    return dict(node_id=node, boot_id=boot, captured_at=int(now), process_uptime_seconds=100,
                discord_connected=gateway)


def observe(m, now, up=True, gateway=True, mono=None, boot="boot1"):
    m.record("a", health(now=now, gateway=gateway, boot=boot) if up else None,
             "connection failed" if not up else "", now=float(now), mono=float(now if mono is None else mono),
             rtt=12.0 if up else None)


def enable(m):
    with m.store.db:
        m.store.append("setting", Setting(revision="10", guild=123, subject="*", channel=456).model_dump())
        m.store.append("setting", Setting(revision="11", guild=123, subject="a", role=789).model_dump())


def test_transient_failure_then_one_outage_and_two_success_recovery():
    m = monitor()
    enable(m)
    observe(m, 1000)
    observe(m, 1010, False)
    assert m.store.latest("b", "a")["state"] == "suspect"
    observe(m, 1020)
    assert m.store.incidents("b") == []
    for now in (1030, 1040, 1050, 1060):
        observe(m, now, False)
    episodes = m.store.incidents("b")
    assert len(episodes) == 1
    assert episodes[0]["detected"] == 1050
    assert episodes[0]["last_success"] == 1020
    observe(m, 1070)
    assert m.store.latest("b", "a")["state"] == "recovering"
    observe(m, 1080)
    assert m.store.incidents("b")[0]["recovered"] == 1080
    assert m.store.db.execute("SELECT count(*) FROM health_outbox").fetchone()[0] == 2
    m.store.db.close()


def test_startup_grace_never_seen_and_no_notification_without_opt_in():
    m = monitor()
    m.started = 1000
    for now in range(1000, 1060, 10):
        observe(m, now, False)
    assert not m.store.incidents("b")
    observe(m, 1060, False)
    assert "never observed" in m.store.incidents("b")[0]["reason"]
    assert m.store.db.execute("SELECT count(*) FROM health_outbox").fetchone()[0] == 0
    m.store.db.close()


def test_observer_restart_preserves_episode_but_not_failure_counters_or_gap_coverage(tmp_path):
    path = tmp_path / "health.db"
    m = monitor(path)
    enable(m)
    observe(m, 1000)
    for now in (1010,1020,1030):
        observe(m, now, False)
    incident_id = m.store.incidents("b")[0]["id"]
    m.store.db.close()
    m = monitor(path)
    observe(m, 2000, False)
    assert m.store.incidents("b")[0]["id"] == incident_id
    assert m.store.latest("b", "a")["start"] == 2000
    observe(m, 2010)
    observe(m, 2020)
    assert m.store.incidents("b")[0]["recovered"] == 2020
    total = m.store.db.execute("SELECT SUM(up+down) FROM health_rollups").fetchone()[0]
    assert total == 50  # Observer downtime contributes no known duration.
    m.store.db.close()


def test_clock_jump_and_suspension_do_not_create_or_double_count_history():
    m = monitor()
    observe(m, 1000, mono=0)
    observe(m, 1010, mono=10)
    observe(m, 500, mono=20)
    observe(m, 510, mono=30)
    observe(m, 2000, mono=40)
    observe(m, 2010, mono=50)
    observe(m, 2500, mono=540, up=False)
    assert m.store.db.execute("SELECT SUM(up+down) FROM health_rollups").fetchone()[0] == 20
    assert not m.store.incidents("b")
    m.store.db.close()


def test_backward_clock_watermark_survives_observer_restart(tmp_path):
    path = tmp_path / "clock.db"
    m = monitor(path)
    observe(m,1000,mono=0)
    observe(m,1010,mono=10)
    observe(m,500,mono=20)
    m.store.db.close()
    m = monitor(path)
    observe(m,510,mono=0)
    observe(m,520,mono=10)
    assert m.store.db.execute("SELECT SUM(up+down) FROM health_rollups").fetchone()[0] == 10
    m.store.db.close()


def test_gateway_incident_is_separate_and_unreachable_gateway_is_unknown():
    m = monitor()
    observe(m, 1000)
    for now in (1010,1020,1030):
        observe(m, now, gateway=False)
    assert m.store.incidents("b")[0]["kind"] == "discord"
    assert m.store.latest("b", "a")["up"]
    observe(m, 1040, False)
    assert m.store.latest("b", "a")["gateway"] is None
    observe(m, 1050)
    assert m.store.incidents("b")[0]["recovered"] is None
    observe(m, 1060)
    assert m.store.incidents("b")[0]["recovered"] == 1060
    m.store.db.close()


def test_replication_idempotence_ownership_settings_order_and_retention():
    a, b = monitor(node="a"), monitor(node="b")
    enable(a)
    observe(a, 1000)
    observe(a, 1010)
    page = a.store.export(0)
    b.store.import_page("a", page)
    b.store.import_page("a", page)
    assert b.store.db.execute("SELECT SUM(up+down) FROM health_rollups").fetchone()[0] == 10
    assert b.store.latest("a", "a")["ts"] == 1010
    assert b.store.setting(123, "a")["role"] == 789
    with b.store.db:
        b.store.append("setting", Setting(revision="20", guild=123, subject="a", role=999).model_dump())
    b.store.import_page("a", a.store.export(b.store.cursor("a")))
    assert b.store.setting(123, "a")["role"] == 999
    with pytest.raises(ValueError):
        b.store.import_page("intruder", page)
    with pytest.raises(ValueError):
        b.store.import_page("a", {**page, "cursor":0})
    a.store.prune(a.cfg, 1000+8*86400)
    assert a.store.export(0)["gap"]
    assert a.store.db.execute("SELECT count(*) FROM health_rollups").fetchone()[0] > 0
    assert a.store.setting(123, "a")["role"] == 789
    a.store.db.close()
    b.store.db.close()


def test_replication_rejects_bad_page_atomically():
    a, b = monitor(node="a"), monitor(node="b")
    observe(a, 1000)
    observe(a, 1010)
    page = a.store.export(0)
    page["records"][1]["value"]["start"] = 0.0
    with pytest.raises(ValueError):
        b.store.import_page("a", page)
    assert b.store.cursor("a") == 0
    assert b.store.latest("a", "a") is None
    a.store.db.close()
    b.store.db.close()


def test_shared_policy_survives_restart(tmp_path):
    path = tmp_path / "settings.db"
    m = monitor(path)
    with m.store.db:
        m.store.append("setting", Setting(revision="50", guild=1, subject="@monitoring",
                                         policy=MonitorPolicy(health_interval=20)).model_dump())
    m.store.db.close()
    m = monitor(path)
    assert m.cfg.health_interval == 20
    m.store.db.close()


def test_probe_timeout_and_remote_clock_skew_are_recorded_separately():
    from mitra_bot.services.peer_service import PeerError
    async def run():
        m = monitor()
        error = PeerError("unreachable")
        error.__cause__ = TimeoutError()
        m.mesh.request.side_effect = error
        await m.probe("a")
        assert m.store.latest("b","a")["reason"] == "probe timed out"
        m.mesh.request.side_effect = None
        m.mesh.request.return_value = health(now=time.time()-300)
        await m.probe("a")
        assert m.store.latest("b","a")["up"]
        assert m.store.latest("b","a")["skew"]
        m.store.db.close()
    asyncio.run(run())


def test_window_boundary_uses_exact_raw_duration_and_long_history_is_retained():
    m = monitor()
    observe(m,1200)
    observe(m,1210)
    observe(m,1220,False)
    stats = metrics(m.store.series("b","a",1205,1215),1205,1215)
    assert stats["coverage"] == 100
    assert stats["availability"] == 50
    m.store.prune(m.cfg,1200+8*86400)
    assert m.store.latest("b","a") is not None
    assert m.store.series("b","a",1200,1500)
    m.store.prune(m.cfg,1200+91*86400)
    assert m.store.series("b","a",1200,1500) == []
    m.store.db.close()


def test_dashboard_unauthorized_and_losing_claimant_do_not_render():
    async def run():
        m = monitor()
        bot = SimpleNamespace(peer_service=m.mesh, state=SimpleNamespace(admin_role_name="Admin"))
        interaction = SimpleNamespace(id=1,guild=None,user=None,followup=SimpleNamespace(send=AsyncMock()))
        with patch("mitra_bot.discord_app.peer_dashboard.claim_interaction", new=AsyncMock(return_value=False)), \
             patch("mitra_bot.discord_app.peer_dashboard.build_dashboard", new=AsyncMock()) as render:
            await handle_dashboard_component(bot,interaction)
            render.assert_not_awaited()
            interaction.followup.send.assert_not_awaited()
        with patch("mitra_bot.discord_app.peer_dashboard.claim_interaction", new=AsyncMock(return_value=True)), \
             patch("mitra_bot.discord_app.peer_dashboard.build_dashboard", new=AsyncMock()) as render:
            await handle_dashboard_component(bot,interaction)
            render.assert_not_awaited()
            assert "permission" in interaction.followup.send.call_args.args[0]
        m.store.db.close()
    asyncio.run(run())


def test_delayed_outage_collapses_to_summary_and_retry_survives_restart(tmp_path):
    async def run():
        path = tmp_path / "outbox.db"
        m = monitor(path)
        enable(m)
        now = int(time.time())-100
        observe(m, now)
        for ts in (now+10,now+20,now+30):
            observe(m, ts, False)
        m.alert_sink = AsyncMock(side_effect=OSError("ambiguous send"))
        await m.flush_alerts()
        assert m.store.db.execute("SELECT attempts FROM health_outbox").fetchone()[0] == 1
        m.store.db.close()
        m = monitor(path)
        observe(m, now+40)
        observe(m, now+50)
        m.alert_sink = AsyncMock(return_value="42")
        # A pending outage retry takes priority over the newer recovery.
        await m.flush_alerts()
        m.alert_sink.assert_not_awaited()
        with m.store.db:
            m.store.db.execute("UPDATE health_outbox SET due=0")
        await m.flush_alerts()
        m.alert_sink.assert_awaited_once()
        assert m.alert_sink.call_args.args[2]["recovered"] == now+50
        assert m.store.db.execute("SELECT count(*) FROM health_outbox WHERE delivered IS NULL").fetchone()[0] == 0
        m.store.db.close()
    asyncio.run(run())


def test_graph_unknown_coverage_and_rendered_png(tmp_path):
    m = monitor()
    observe(m, 1200)
    observe(m, 1210)
    observe(m, 1220, False)
    rows = m.store.series("b", "a", 1200, 1500)
    stats = metrics(rows, 1200, 1500)
    assert stats["availability"] == 50
    assert stats["coverage"] == pytest.approx(100*20/300)
    assert metrics([],1200,1500)["availability"] is None
    assert metrics(_compact(rows,900),1200,1500) == stats
    for detail in (True, False):
        png = render_history({"a":rows, "b":[]} if not detail else {"a":rows}, "b", 1200,1500,detail=detail)
        assert png.getvalue().startswith(b"\x89PNG")
        (tmp_path / f"health-{detail}.png").write_bytes(png.getvalue())
    m.store.db.close()


def test_dashboard_controls_and_schema_are_valid():
    async def run():
        m = monitor()
        view = dashboard_view(m.mesh, None, "b",24,0)
        assert view.is_finished()
        ids = [item.custom_id for item in view.children]
        assert len(ids) == len(set(ids))
        assert max(map(len, ids)) <= 100
        response = await build_dashboard(m.mesh)
        assert response["file"].filename == "peer-health.png"
        assert "coverage" in response["embed"].fields[0].value
        response["file"].close()
        m.store.db.close()
        bot = discord.Bot(intents=discord.Intents.none())
        cog = ServersCog(bot)
        bot.add_cog(cog)
        cog.servers.integration_types = {discord.IntegrationType.guild_install}
        cog.servers.contexts = {discord.InteractionContextType.guild}
        definition = cog.servers.to_dict()
        assert {v["name"] for v in definition["options"]} >= {"status","dashboard","incidents","alerts","subscribe","unsubscribe","monitoring"}
        await bot.close()
    asyncio.run(run())


def test_subscription_rejects_privileged_roles_and_only_changes_invoking_member():
    async def run():
        m = monitor()
        enable(m)
        role = SimpleNamespace(id=789, permissions=discord.Permissions(administrator=True), managed=False)
        guild = SimpleNamespace(id=123, get_role=lambda _:role)
        author = Mock(spec=discord.Member)
        author.add_roles = AsyncMock()
        author.remove_roles = AsyncMock()
        ctx = SimpleNamespace(guild=guild, author=author, respond=AsyncMock())
        cog = ServersCog(SimpleNamespace(peer_service=m.mesh))
        await cog._subscription(ctx,"a",True)
        author.add_roles.assert_not_awaited()
        assert "permissions changed" in ctx.respond.call_args.args[0]
        await cog._subscription(ctx,"a",False)
        author.remove_roles.assert_awaited_once_with(role, reason="Self-service Mitra peer alert unsubscription")
        m.store.db.close()
    asyncio.run(run())


def test_subscription_does_not_grant_mitra_admin_or_channel_overwrite_access():
    class Role:
        id = 789
        name = "Admin"
        managed = False
        permissions = discord.Permissions.none()
        def __lt__(self, other):
            return True
    async def run():
        m = monitor()
        enable(m)
        role = Role()
        channel = SimpleNamespace(overwrites_for=lambda _:discord.PermissionOverwrite(view_channel=True))
        guild = SimpleNamespace(id=123, get_role=lambda _:role, me=SimpleNamespace(top_role=object()), channels=[])
        author = Mock(spec=discord.Member)
        author.add_roles = AsyncMock()
        ctx = SimpleNamespace(guild=guild, author=author, respond=AsyncMock())
        cog = ServersCog(SimpleNamespace(peer_service=m.mesh, state=SimpleNamespace(admin_role_name="Admin")))
        await cog._subscription(ctx,"a",True)
        author.add_roles.assert_not_awaited()
        role.name = "Mitra a alerts"
        guild.channels = [channel]
        await cog._subscription(ctx,"a",True)
        author.add_roles.assert_not_awaited()
        guild.channels = []
        await cog._subscription(ctx,"a",True)
        author.add_roles.assert_awaited_once_with(role, reason="Self-service Mitra peer alert subscription")
        m.store.db.close()
    asyncio.run(run())


def test_removed_members_do_not_block_retained_history_replication():
    a,b = monitor(node="a"),monitor(node="b")
    a.record("c",health(node="c"),now=1000.0,mono=1000.0)
    observe(a,1010)
    a.store.members.remove("c")
    b.store.members.remove("c")
    page = a.store.export(0)
    assert page["gap"]
    b.store.import_page("a",page)
    assert b.store.latest("a","a")
    assert b.store.latest("a","c") is None
    a.store.db.close()
    b.store.db.close()


def test_discord_alert_nonce_mentions_dedup_and_delayed_summary():
    async def run():
        m = monitor()
        bot = SimpleNamespace(is_ready=lambda:True, gateway_connected=True, user=SimpleNamespace(id=99),
                              http=SimpleNamespace(request=AsyncMock(return_value={"id":"42"})))
        role = SimpleNamespace(id=789, mention="<@&789>")
        channel = Mock(spec=discord.TextChannel)
        channel.id = 456
        channel.guild = SimpleNamespace(id=123, get_role=lambda _:role)
        messages = []
        async def history(**kwargs):
            for message in messages:
                yield message
        channel.history = history
        bot.get_channel = lambda _:channel
        delivery = PeerAlertDelivery(bot,m.mesh)
        incident = dict(subject="a",kind="peer",start=1000.0,detected=1030.0,recovered=None,last_success=990.0)
        setting = dict(channel=456,guild=123)
        role_setting = dict(role=789,enabled=True)
        assert await delivery("test-key","outage",incident,setting,role_setting) == "42"
        payload = bot.http.request.call_args.kwargs["json"]
        assert payload["enforce_nonce"] is True
        assert payload["allowed_mentions"] == {"parse":[],"roles":["789"],"users":[]}
        assert len(payload["nonce"]) <= 25
        from datetime import datetime, timezone
        messages.append(SimpleNamespace(id=42, author=bot.user, embeds=[discord.Embed.from_dict(payload["embeds"][0])],
                                        created_at=datetime.now(timezone.utc)))
        assert await delivery("test-key","outage",incident,setting,role_setting) == 42
        assert await delivery("different-observer","outage",incident,setting,role_setting) == 42
        assert bot.http.request.await_count == 1
        messages.clear()
        incident["recovered"] = 1060.0
        await delivery("summary-key","outage",incident,setting,role_setting)
        assert "delayed report" in bot.http.request.call_args.kwargs["json"]["embeds"][0]["title"]
        m.store.db.close()
    asyncio.run(run())
