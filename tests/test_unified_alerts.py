import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from mitra_bot.services.alert_roles import configure_shared_role, subscription, shared_role
from mitra_bot.services.peer_alerts import PeerAlertDelivery
from mitra_bot.discord_app.interaction_routing import command_route


def fixture():
    def role(id, name):
        r = Mock(spec=discord.Role)
        r.id, r.name, r.managed, r.mentionable = id, name, False, True
        r.permissions = discord.Permissions.none()
        r.__lt__ = Mock(return_value=True)
        return r
    shared, ip, peer = role(10, "Mitra Alerts"), role(11, "Mitra IP Subscriber"), role(12, "Mitra test alerts")
    guild = SimpleNamespace(id=1, roles=[shared, ip, peer], channels=[], me=SimpleNamespace(top_role=object()))
    guild.fetch_roles = AsyncMock(return_value=guild.roles)
    guild.create_role = AsyncMock()
    member = Mock(spec=discord.Member)
    member.roles = [ip, peer]
    member.add_roles, member.remove_roles = AsyncMock(), AsyncMock()
    async def members(**kwargs):
        assert kwargs == {"limit": None}
        yield member
    guild.fetch_members = members
    store = SimpleNamespace(settings=lambda: [{"guild": 1, "subject": "test", "role": 12}])
    bot = SimpleNamespace(state=SimpleNamespace(infrastructure_guild_ids=(1,), admin_role_name="Admin", ip_subscriber_role_name="Mitra IP Subscriber"),
                          peer_service=SimpleNamespace(monitor=SimpleNamespace(store=store)))
    return bot, guild, member, shared, ip, peer


def test_migration_preserves_union_of_old_subscribers():
    bot, guild, member, shared, ip, peer = fixture()
    assert asyncio.run(configure_shared_role(bot, guild)) is shared
    member.add_roles.assert_awaited_once_with(shared, reason="Preserve existing Mitra alert subscription")
    member.remove_roles.assert_awaited_once_with(ip, peer, reason="Consolidate Mitra alert subscriptions")
    guild.create_role.assert_not_awaited()


def test_failed_assignment_retains_old_subscriptions():
    bot, guild, member, *_ = fixture()
    member.add_roles.side_effect = RuntimeError("failed")
    with pytest.raises(RuntimeError):
        asyncio.run(configure_shared_role(bot, guild))
    member.remove_roles.assert_not_awaited()


def test_unsafe_legacy_role_stops_membership_migration():
    bot, guild, member, shared, ip, peer = fixture()
    peer.permissions = discord.Permissions(administrator=True)
    with pytest.raises(ValueError, match="old subscriber role"):
        asyncio.run(configure_shared_role(bot, guild))
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


def test_duplicate_shared_roles_are_not_chosen_arbitrarily():
    bot, guild, member, shared, *_ = fixture()
    guild.roles.append(shared)
    with pytest.raises(ValueError, match="Multiple"):
        shared_role(guild)
    with pytest.raises(ValueError, match="Multiple"):
        asyncio.run(configure_shared_role(bot, guild))


@pytest.mark.parametrize("subscribe", [True, False])
def test_shared_subscription_and_unsubscribe_clear_legacy_memberships(subscribe):
    bot, guild, member, shared, ip, peer = fixture()
    ctx = SimpleNamespace(bot=bot, guild=guild, author=member, defer=AsyncMock(), respond=AsyncMock())
    asyncio.run(subscription(ctx, subscribe))
    if subscribe:
        member.add_roles.assert_awaited_once_with(shared, reason="Subscribed to all Mitra alerts")
    else:
        member.remove_roles.assert_awaited_once_with(shared, ip, peer, reason="Unsubscribed from all Mitra alerts")
    assert "all Mitra alerts" in ctx.respond.call_args.args[0]


def test_shared_commands_can_run_when_state_owner_is_down():
    mesh = SimpleNamespace(config=SimpleNamespace(resolved_state_owner="mitra"))
    for name, sub in (("alerts", "subscribe"), ("alerts", "unsubscribe")):
        assert command_route({"name": name, "options": [{"type": 1, "name": sub}]}, mesh) == ("mitra", True)


@pytest.mark.parametrize("unsafe", ["permissions", "admin", "channel", "managed"])
def test_subscription_rejects_roles_that_grant_access(unsafe):
    bot, guild, member, role, *_ = fixture()
    if unsafe == "permissions":
        role.permissions = discord.Permissions(administrator=True)
    elif unsafe == "admin":
        bot.state.admin_role_name = "Mitra Alerts"
    elif unsafe == "channel":
        guild.channels = [SimpleNamespace(overwrites_for=lambda _: discord.PermissionOverwrite(view_channel=True))]
    else:
        role.managed = True
    ctx = SimpleNamespace(bot=bot, guild=guild, author=member, defer=AsyncMock(), respond=AsyncMock())
    asyncio.run(subscription(ctx, True))
    member.add_roles.assert_not_awaited()


def test_ip_notifications_use_shared_destination_and_honor_disable(monkeypatch):
    from mitra_bot.discord_app.cogs import ip_cog
    settings = [{"subject": "*", "guild": 1, "role": 10, "channel": 100, "enabled": True}]
    mesh = SimpleNamespace(config=SimpleNamespace(node_id="Anubis"), monitor=SimpleNamespace(store=SimpleNamespace(settings=lambda: settings)), notify=AsyncMock(return_value=True))
    bot = SimpleNamespace(peer_service=mesh, state=SimpleNamespace(channel_id=300, infrastructure_guild_ids=(1,)))
    monkeypatch.setattr(ip_cog, "get_notification_channel_map", lambda: {1: 200})
    cog = ip_cog.IPCog(bot)
    assert asyncio.run(cog.notify_ip_change("203.0.113.1"))
    assert mesh.notify.call_args.args[0].channel_id == 100
    assert mesh.notify.call_args.args[0].mention_ip_subscribers is True
    assert mesh.notify.call_args.args[0].message.startswith("### 🌐 Anubis's public IP address changed")
    assert "```\n203.0.113.1\n```" in mesh.notify.call_args.args[0].message
    settings[0]["enabled"] = False
    mesh.notify.reset_mock()
    assert asyncio.run(cog.notify_ip_change("203.0.113.2"))
    mesh.notify.assert_not_awaited()


def test_health_alerts_prefer_shared_role_over_legacy_per_node_role():
    async def run():
        bot, guild, member, shared, *_ = fixture()
        shared.mention = "<@&10>"
        guild.get_role = lambda id: shared if id == 10 else None
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.guild = 100, guild
        async def history(**kwargs):
            if False:
                yield None
        channel.history = history
        bot.is_ready = lambda: True
        bot.gateway_connected = True
        bot.user = SimpleNamespace(id=50)
        bot.get_channel = lambda _: channel
        bot.http = SimpleNamespace(request=AsyncMock(return_value={"id": "5"}))
        mesh = SimpleNamespace(config=SimpleNamespace(network_id="n", node_id="mitra"))
        incident = dict(subject="test", kind="peer", start=1000, detected=1030, last_success=990, recovered=None)
        await PeerAlertDelivery(bot, mesh)("key", "outage", incident,
            dict(channel=100, guild=1, role=10, enabled=True), dict(role=999, enabled=True))
        assert bot.http.request.call_args.kwargs["json"]["allowed_mentions"]["roles"] == ["10"]
    asyncio.run(run())


def test_alert_setup_saves_one_role_without_resetting_maintenance(monkeypatch):
    from test_peer_monitor import monitor
    from mitra_bot.services.peer_monitor import Setting
    from mitra_bot.discord_app.cogs import servers_cog
    from mitra_bot.discord_app import peer_operations
    async def run():
        m = monitor()
        with m.store.db:
            m.store.append("setting", Setting(revision="1", guild=1, subject="a", maintenance_until=9999999999.0, maintenance_reason="planned").model_dump())
        bot, guild, member, role, *_ = fixture()
        bot.peer_service = m.mesh
        cog = servers_cog.ServersCog(bot)
        cog._mesh = AsyncMock(return_value=m.mesh)
        channel = SimpleNamespace(id=123, mention="#mitra")
        ctx = SimpleNamespace(guild=guild, interaction=SimpleNamespace(id=100), defer=AsyncMock(), respond=AsyncMock())
        monkeypatch.setattr(servers_cog, "configure_shared_role", AsyncMock(return_value=role))
        monkeypatch.setattr(peer_operations, "verify_channel", lambda *_: None)
        await servers_cog.ServersCog.alerts.callback(cog, ctx, channel)
        assert m.store.setting(1, "*")["role"] == role.id
        assert all(m.store.setting(1, node)["role"] == role.id for node in m.store.members)
        assert m.store.setting(1, "a")["maintenance_reason"] == "planned"
        assert m.store.setting(1, "a")["maintenance_until"] == 9999999999.0
        m.store.db.close()
    asyncio.run(run())
