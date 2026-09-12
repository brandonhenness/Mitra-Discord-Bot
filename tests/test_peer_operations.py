import asyncio
import time
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from test_peer_monitor import monitor, observe, enable
from mitra_bot.services.peer_monitor import Setting
from mitra_bot.discord_app.cogs.servers_cog import ServersCog
from mitra_bot.discord_app.peer_operations import SharedDashboard, dashboard_marker, find_dashboard, verify_channel, doctor


def maintenance(m, until, revision="100"):
    with m.store.db:
        m.store.append("setting",Setting(revision=revision,guild=1,subject="a",maintenance_until=float(until),maintenance_reason="Updates").model_dump())


def test_maintenance_keeps_history_and_discards_recovered_alerts(tmp_path):
    async def run():
        m = monitor(tmp_path/"maintenance.db")
        enable(m)
        now = int(time.time())
        maintenance(m,now+300)
        observe(m,now-40)
        for ts in (now-30,now-20,now-10):
            observe(m,ts,False)
        m.alert_sink = AsyncMock()
        await m.flush_alerts()
        m.alert_sink.assert_not_awaited()
        assert m.store.incidents("b")[0]["recovered"] is None
        observe(m,now)
        observe(m,now+10)
        assert m.store.incidents("b")[0]["recovered"] == now+10
        assert m.store.db.execute("SELECT count(*) FROM health_outbox WHERE delivered IS NULL").fetchone()[0] == 0
        assert m.store.db.execute("SELECT SUM(up+down) FROM health_rollups").fetchone()[0] > 0
        m.store.db.close()
    asyncio.run(run())


def test_maintenance_expiry_and_early_end_release_open_outage_after_restart(tmp_path):
    async def run():
        path = tmp_path/"restart.db"
        m = monitor(path)
        enable(m)
        now = int(time.time())
        maintenance(m,now+300)
        observe(m,now-40)
        for ts in (now-30,now-20,now-10):
            observe(m,ts,False)
        m.store.db.close()
        m = monitor(path)
        assert m.maintenance("a")
        assert m.maintenance("a",now+301) is None
        m.alert_sink = AsyncMock(return_value="42")
        maintenance(m,0,revision="101")
        await m.flush_alerts()
        m.alert_sink.assert_awaited_once()
        assert m.alert_sink.call_args.args[1] == "outage"
        m.store.db.close()
    asyncio.run(run())


def channel_and_bot(m):
    guild = SimpleNamespace(id=123,me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),channels=[])
    channel = Mock(spec=discord.TextChannel)
    channel.id,channel.guild,channel.mention = 456,guild,"<#456>"
    channel.permissions_for.return_value = discord.Permissions.all()
    bot = SimpleNamespace(user=SimpleNamespace(id=99),peer_service=m.mesh,is_ready=lambda:True,gateway_connected=True,
                          get_channel=lambda _:channel,http=SimpleNamespace(request=AsyncMock(return_value={"id":"42"})),
                          state=SimpleNamespace(admin_role_name="Admin"))
    return channel,bot,guild


def dashboard_setting(m):
    value = Setting(revision="100",guild=123,subject="@dashboard",channel=456,message_id=999,dashboard_interval=60)
    with m.store.db:
        m.store.append("setting",value.model_dump())
    return value.model_dump()


def test_surviving_dashboard_updater_edits_same_message_and_other_peers_skip():
    async def run():
        b,c = monitor(node="b"),monitor(node="c")
        setting = dashboard_setting(b)
        dashboard_setting(c)
        channel,bot,guild = channel_and_bot(b)
        embed = discord.Embed()
        embed.set_footer(text=dashboard_marker(b.mesh,guild.id))
        message = SimpleNamespace(id=999,author=bot.user,embeds=[embed],created_at=datetime.fromtimestamp(time.time()-100,timezone.utc),edited_at=None)
        async def edit(**kwargs):
            assert kwargs["attachments"] == []
            assert kwargs["allowed_mentions"].to_dict()["parse"] == []
            message.edited_at = datetime.now(timezone.utc)
        message.edit = AsyncMock(side_effect=edit)
        channel.fetch_message = AsyncMock(return_value=message)
        payload = dict(embed=embed,file=discord.File(BytesIO(b"png"),filename="graph.png"),view=None,allowed_mentions=discord.AllowedMentions.none())
        with patch("mitra_bot.discord_app.peer_operations.public_dashboard",new=AsyncMock(return_value=payload)) as render:
            # The preferred node 'a' is absent. b refreshes; c sees that edit and skips.
            await SharedDashboard(bot,b.mesh).refresh(setting)
            await SharedDashboard(bot,c.mesh).refresh(setting)
            render.assert_awaited_once()
        message.edit.assert_awaited_once()
        assert channel.fetch_message.call_args.args == (999,)
        b.store.db.close()
        c.store.db.close()
    asyncio.run(run())


def test_dashboard_stop_during_render_prevents_late_edit():
    async def run():
        m = monitor()
        setting = dashboard_setting(m)
        channel,bot,guild = channel_and_bot(m)
        embed = discord.Embed()
        embed.set_footer(text=dashboard_marker(m.mesh,guild.id))
        message = SimpleNamespace(id=999,author=bot.user,embeds=[embed],created_at=datetime.fromtimestamp(time.time()-200,timezone.utc),edited_at=None,edit=AsyncMock())
        channel.fetch_message = AsyncMock(return_value=message)
        async def render(*args):
            with m.store.db:
                m.store.append("setting",{**setting,"revision":"101","enabled":False})
            return dict(embed=embed,file=discord.File(BytesIO(b"png"),filename="graph.png"))
        with patch("mitra_bot.discord_app.peer_operations.public_dashboard",new=render):
            await SharedDashboard(bot,m.mesh).refresh(setting)
        message.edit.assert_not_awaited()
        m.store.db.close()
    asyncio.run(run())


def test_pin_permission_is_distinct_from_manage_messages():
    m = monitor()
    channel,bot,guild = channel_and_bot(m)
    permissions = discord.Permissions.all()
    permissions.pin_messages = False
    permissions.administrator = False
    channel.permissions_for.return_value = permissions
    with pytest.raises(ValueError,match="pin messages"):
        verify_channel(channel,guild,dashboard=True,pin=True)
    verify_channel(channel,guild,dashboard=True)  # Updating one's own message needs no pin permission.
    m.store.db.close()


def test_find_existing_dashboard_supports_new_pin_wrappers():
    async def run():
        m = monitor()
        channel,bot,guild = channel_and_bot(m)
        embed = discord.Embed()
        marker = dashboard_marker(m.mesh,guild.id)
        embed.set_footer(text=marker)
        message = SimpleNamespace(author=bot.user,embeds=[embed])
        async def pins():
            yield SimpleNamespace(message=SimpleNamespace(author=bot.user, embeds=[discord.Embed(title="No footer")]))
            yield SimpleNamespace(message=message)
        channel.pins = pins
        assert await find_dashboard(channel,bot,marker) is message
        m.store.db.close()
    asyncio.run(run())


def test_test_alert_is_explicit_and_does_not_write_incidents_or_ping_by_default():
    async def run():
        m = monitor()
        enable(m)
        channel,bot,guild = channel_and_bot(m)
        guild.get_role = lambda _:None
        ctx = SimpleNamespace(guild=guild,author=SimpleNamespace(id=77),interaction=SimpleNamespace(id=12345),defer=AsyncMock(),respond=AsyncMock())
        cog = ServersCog(bot)
        cog._mesh = AsyncMock(return_value=m.mesh)
        await ServersCog.alerts_test.callback(cog,ctx,"a",False)
        payload = bot.http.request.call_args.kwargs["json"]
        assert "TEST ONLY" in payload["embeds"][0]["title"]
        assert payload["allowed_mentions"] == {"parse":[],"users":[],"roles":[]}
        assert payload["enforce_nonce"] is True
        assert not m.store.incidents("b")
        await ServersCog.alerts_test.callback(cog,ctx,"a",True)
        assert bot.http.request.await_count == 1  # No valid role, no mention test sent.
        guild.get_role = lambda _:SimpleNamespace(id=789,mention="<@&789>",mentionable=True)
        cog._safe_role = lambda *_:True
        await ServersCog.alerts_test.callback(cog,ctx,"a",True)
        assert bot.http.request.call_args.kwargs["json"]["allowed_mentions"] == {"parse":[],"users":[],"roles":["789"]}
        m.store.db.close()
    asyncio.run(run())


def test_non_admin_cannot_send_test_alert_or_start_maintenance():
    async def run():
        m = monitor()
        channel,bot,guild = channel_and_bot(m)
        member = Mock(spec=discord.Member)
        member.roles = []
        ctx = SimpleNamespace(guild=guild,bot=bot,author=member,respond=AsyncMock())
        cog = ServersCog(bot)
        await ServersCog.alerts_test.callback(cog,ctx,"a",False)
        await ServersCog.maintenance_command.callback(cog,ctx,"a",60,"Test")
        bot.http.request.assert_not_awaited()
        assert m.maintenance("a") is None
        m.store.db.close()
    asyncio.run(run())


def test_doctor_reports_missing_permissions_and_pending_alerts_without_mutations():
    async def run():
        m = monitor()
        enable(m)
        channel,bot,guild = channel_and_bot(m)
        channel.permissions_for.return_value = discord.Permissions.none()
        from mitra_bot.services.peer_service import PeerError
        m.mesh.request.side_effect = PeerError("cannot connect")
        before = m.store.db.total_changes
        lines = await doctor(m.mesh,bot,guild)
        assert any("check failed" in line for line in lines)
        assert any("send messages" in line for line in lines)
        assert any("Queued alerts: 0" in line for line in lines)
        assert m.store.db.total_changes == before
        bot.http.request.assert_not_awaited()
        m.store.db.close()
    asyncio.run(run())


def test_new_settings_replicate_and_old_schema_same_revision_is_accepted():
    a,b = monitor(node="a"),monitor(node="b")
    maintenance(a,time.time()+300)
    dashboard_setting(a)
    b.store.import_page("a",a.store.export(0))
    assert b.maintenance("a")
    assert b.store.setting(123,"@dashboard")["message_id"] == 999
    value = Setting(revision="200",guild=123,subject="*",channel=456).model_dump()
    import json
    old = {k:v for k,v in value.items() if k not in {"maintenance_until","maintenance_reason","message_id","dashboard_interval","dashboard_hours","dashboard_page"}}
    with b.store.db:
        b.store.db.execute("INSERT OR REPLACE INTO health_settings VALUES (?,?,?,?)",(123,"*","200",json.dumps(old)))
        b.store._setting(value)
    a.store.db.close()
    b.store.db.close()


def test_blocked_recoveries_do_not_starve_other_outage_alerts():
    async def run():
        m = monitor()
        enable(m)
        now = time.time()
        with m.store.db:
            for i in range(21):
                incident = dict(id=f"{i:032x}",subject="a",kind="peer",start=now-100,detected=now-60,
                                recovered=now-30,last_success=now-110,reason="connection failed")
                m.store.append("incident",incident)
                m.store.db.execute("INSERT INTO health_outbox(id,incident,guild,kind,due) VALUES (?,?,?,?,?)",
                                   (f"outage-{i}",incident["id"],123,"outage",now+3600))
                m.store.db.execute("INSERT INTO health_outbox(id,incident,guild,kind,due) VALUES (?,?,?,?,?)",
                                   (f"recovery-{i}",incident["id"],123,"recovery",now-100))
            incident["id"] = "f"*32
            incident["recovered"] = None
            m.store.append("incident",incident)
            m.store.db.execute("INSERT INTO health_outbox(id,incident,guild,kind,due) VALUES (?,?,?,?,?)",
                               ("new-outage",incident["id"],123,"outage",now-1))
        m.alert_sink = AsyncMock(return_value="42")
        await m.flush_alerts()
        await m.flush_alerts()
        m.alert_sink.assert_awaited_once()
        assert m.alert_sink.call_args.args[0] == "new-outage"
        m.store.db.close()
    asyncio.run(run())


def test_pin_command_saves_shared_message_and_stop_persists_without_deleting():
    async def run():
        m = monitor()
        channel,bot,guild = channel_and_bot(m)
        message = SimpleNamespace(id=999,jump_url="https://discord.com/channels/123/456/999",pin=AsyncMock())
        # Python 3.10 cannot inspect an unspecced AsyncMock's synthetic code
        # object. Use a real async function signature, as the Discord API has.
        from unittest.mock import create_autospec
        async def send(*, nonce, **kwargs):
            return message
        channel.send = create_autospec(send, side_effect=send)
        ctx = SimpleNamespace(guild=guild,interaction=SimpleNamespace(id=200),defer=AsyncMock(),respond=AsyncMock())
        cog = ServersCog(bot)
        cog._mesh = AsyncMock(return_value=m.mesh)
        payload = dict(embed=discord.Embed(),file=discord.File(BytesIO(b"png"),filename="graph.png"),view=None,
                       allowed_mentions=discord.AllowedMentions.none())
        with patch("mitra_bot.discord_app.peer_operations.find_dashboard",new=AsyncMock(return_value=None)), \
             patch("mitra_bot.discord_app.peer_operations.public_dashboard",new=AsyncMock(return_value=payload)):
            await ServersCog.dashboard_pin.callback(cog,ctx,channel,300,24,1)
        saved = m.store.setting(123,"@dashboard")
        assert saved["message_id"] == 999 and saved["enabled"]
        assert saved["dashboard_interval"] == 300
        message.pin.assert_awaited_once()
        assert m.mesh.request.await_count == 2
        ctx.interaction.id = 201
        await ServersCog.dashboard_stop.callback(cog,ctx)
        assert not m.store.setting(123,"@dashboard")["enabled"]
        channel.send.assert_awaited_once()
        m.store.db.close()
    asyncio.run(run())
