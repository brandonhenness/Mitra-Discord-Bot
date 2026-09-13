import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from mitra_bot.discord_app.access import command_allowed, infrastructure_guild
from mitra_bot.discord_app.bot_factory import AppState, MitraBot, create_bot
from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.discord_app.cogs.about_cog import AboutCog
from mitra_bot.discord_app.cogs.power_cog import PowerActionView
from mitra_bot.discord_app.cogs.update_cog import UpdatePromptView
from mitra_bot.discord_app.cogs.todo_cog import (
    TodoCog, AddTaskModal, ThreadEditTaskModal, ListCreateModal, BoardView, can_manage_lists,
)
from mitra_bot.services.notifier import Notifier
from mitra_bot.services.alert_roles import subscription, configure_shared_role
from mitra_bot.services.peer_alerts import PeerAlertDelivery
from mitra_bot.discord_app.peer_operations import SharedDashboard
from mitra_bot.storage import storage_store as storage
from mitra_bot.storage.config_store import write_config_dict, read_config_dict, BotFileConfigModel
from mitra_bot.settings import load_settings


def bot_state(*guild_ids):
    return AppState(None, "Mitra Admin", "Mitra Alerts", guild_ids)


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("MITRA_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MITRA_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DISCORD_APPLICATION_TOKEN", "test-token")


@pytest.mark.parametrize("name", ["ip", "power", "ups", "update", "alerts", "notifications", "servers", "future-command"])
def test_infrastructure_requires_operator_guild_even_with_admin_role(name):
    bot = SimpleNamespace(state=bot_state(1))
    assert command_allowed(bot, SimpleNamespace(id=1), name)
    assert not command_allowed(bot, SimpleNamespace(id=2), name)
    assert not command_allowed(bot, None, name)
    assert not infrastructure_guild(SimpleNamespace(), 1)


@pytest.mark.parametrize("guild_id, admin, allowed", [(1, True, True), (1, False, False), (2, True, False)])
def test_admin_guard_requires_both_guild_and_role(guild_id, admin, allowed):
    member = Mock(spec=discord.Member)
    member.roles = [SimpleNamespace(name="Mitra Admin")] if admin else []
    ctx = SimpleNamespace(bot=SimpleNamespace(state=bot_state(1)), guild=SimpleNamespace(id=guild_id),
                          author=member, respond=AsyncMock())
    guard = ensure_admin(ctx)
    assert (guard is None) == allowed
    if guard is not None:
        asyncio.run(guard)
        assert ctx.respond.call_args.kwargs["ephemeral"]


@pytest.mark.parametrize("ids", [(), (1, 2)])
def test_only_public_commands_are_global(ids, isolated_storage):
    async def run():
        bot = create_bot(state=bot_state(*ids))
        try:
            commands = bot.pending_application_commands
            assert {c.name for c in commands if c.guild_ids is None} == {"todo", "about"}
            assert {c.name for c in commands} >= {"ip", "power", "ups", "update", "servers", "alerts", "notifications"}
            for command in commands:
                if command.name not in {"todo", "about"}:
                    assert command.guild_ids == list(ids)
        finally:
            await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("peer", [False, True])
def test_private_command_is_denied_before_peer_routing_or_handler(monkeypatch, peer):
    async def run():
        bot = MitraBot(intents=discord.Intents.none())
        bot.state = bot_state(1)
        bot.peer_service = SimpleNamespace(is_state_owner=True) if peer else None
        parent = AsyncMock()
        monkeypatch.setattr(discord.Bot, "process_application_commands", parent)
        event = SimpleNamespace(guild=SimpleNamespace(id=2), data={"name": "power"}, id=1,
            type=discord.InteractionType.application_command, created_at=datetime.now(timezone.utc),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await bot.process_application_commands(event)
        parent.assert_not_awaited()
        event.followup.send.assert_awaited_once()
        await bot.close()
    asyncio.run(run())


def test_public_about_never_queries_nodes():
    bot = SimpleNamespace(state=bot_state(1), peer_service=SimpleNamespace())
    ctx = SimpleNamespace(guild=SimpleNamespace(id=2), respond=AsyncMock())
    asyncio.run(AboutCog.about.callback(AboutCog(bot), ctx, "private-node"))
    text = str(ctx.respond.call_args.kwargs["embed"].to_dict())
    assert "private-node" not in text and "Python" not in text
    assert "/todo list_create" in text


def test_unauthorized_autocomplete_returns_no_private_choices():
    async def run():
        bot = MitraBot(intents=discord.Intents.none())
        bot.state = bot_state(1)
        event = SimpleNamespace(guild=SimpleNamespace(id=2), data={"name": "ip"},
            type=discord.InteractionType.auto_complete,
            response=SimpleNamespace(send_autocomplete_result=AsyncMock()))
        await bot.process_application_commands(event)
        event.response.send_autocomplete_result.assert_awaited_once_with([])
        await bot.close()
    asyncio.run(run())


def test_public_owner_outage_does_not_disclose_private_node_names(monkeypatch):
    async def run():
        bot = MitraBot(intents=discord.Intents.none())
        bot.state = bot_state(1)
        bot.peer_service = SimpleNamespace(is_state_owner=False,
            config=SimpleNamespace(resolved_state_owner="secret-host"))
        monkeypatch.setattr("mitra_bot.discord_app.bot_factory.response_delay", lambda *args: 0)
        event = SimpleNamespace(guild=SimpleNamespace(id=2), data={"name": "todo"}, id=1,
            type=discord.InteractionType.application_command, created_at=datetime.now(timezone.utc),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await bot.process_application_commands(event)
        text = event.followup.send.call_args.args[0]
        assert "secret-host" not in text and "no change" in text
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("guild_id, allowed", [(1, True), (2, False)])
def test_power_and_update_buttons_recheck_server(guild_id, allowed):
    async def run():
        bot = SimpleNamespace(state=bot_state(1))
        user = Mock(spec=discord.Member)
        user.roles = [SimpleNamespace(name="Mitra Admin")]
        event = SimpleNamespace(client=bot, guild=SimpleNamespace(id=guild_id), user=user)
        power = PowerActionView(action="shutdown", delay_seconds=0, force=False, requester_id=3, channel_id=4)
        update = UpdatePromptView(SimpleNamespace(bot=bot), SimpleNamespace(), source="test")
        assert power._is_admin_user(event) == allowed
        assert update._is_admin_user(event) == allowed
    asyncio.run(run())


def test_public_alert_subscriptions_and_background_sinks_are_blocked():
    async def run():
        bot = SimpleNamespace(state=bot_state(1))
        ctx = SimpleNamespace(bot=bot, guild=SimpleNamespace(id=2), respond=AsyncMock())
        await subscription(ctx, True)
        assert "not available" in ctx.respond.call_args.args[0]
        with pytest.raises(ValueError):
            await configure_shared_role(bot, ctx.guild)
        with pytest.raises(ValueError):
            await PeerAlertDelivery(bot, None)("key", "outage", {}, {"guild": 2}, {})
        with pytest.raises(ValueError):
            await SharedDashboard(bot, None).refresh({"guild": 2})
    asyncio.run(run())


@pytest.mark.parametrize("guild_id, sent", [(1, True), (2, False)])
def test_notification_sink_validates_resolved_channel(guild_id, sent):
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=guild_id)
    channel.send = AsyncMock()
    bot = SimpleNamespace(state=bot_state(1), get_channel=lambda _: channel, fetch_user=AsyncMock())
    assert asyncio.run(Notifier(bot).send_to_channel(100, "private IP")) == sent
    assert channel.send.await_count == int(sent)
    asyncio.run(Notifier(bot).dm_subscribers([7], "private IP"))
    bot.fetch_user.assert_not_awaited()


@pytest.mark.parametrize("config, expected", [({}, ()), ({"guild_id": 1}, (1,)),
    ({"guild_id": 1, "infrastructure_guild_ids": []}, ()),
    ({"guild_id": 1, "infrastructure_guild_ids": [2, 3]}, (2, 3))])
def test_settings_authorization_is_explicit_and_survives_state_saves(isolated_storage, config, expected):
    write_config_dict({"bot": config})
    assert load_settings(interactive_token=False).infrastructure_guild_ids == expected
    storage.set_todo_tasks_for_list_channel(10, [], guild_id=9)
    assert load_settings(interactive_token=False).infrastructure_guild_ids == expected
    assert read_config_dict()["bot"].get("guild_id") == config.get("guild_id")


@pytest.mark.parametrize("value", [[True], [0], [-1], ["1"], "1", [2**64]])
def test_malformed_allowlist_fails_closed(value):
    with pytest.raises(ValueError):
        BotFileConfigModel(infrastructure_guild_ids=value)


def test_todo_ownership_and_legacy_isolation(isolated_storage):
    storage.set_todo_tasks_for_list_channel(10, [{"id": 1, "title": "Private"}], guild_id=1)
    storage.set_todo_tasks_for_list_channel(20, [{"id": 1, "title": "Public"}], guild_id=2)
    storage.set_todo_tasks_for_list_channel(30, [{"id": 1, "title": "Legacy"}])
    assert storage.get_todo_list_channel_ids_for_guild(1) == [10]
    assert storage.get_todo_list_channel_ids_for_guild(2) == [20]
    with pytest.raises(ValueError):
        storage.get_todo_tasks_for_list_channel(10, guild_id=2)
    with pytest.raises(ValueError):
        storage.set_todo_tasks_for_list_channel(10, [], guild_id=2)
    with pytest.raises(ValueError):
        storage.set_todo_list_board_message_id(10, 40, guild_id=2)
    assert storage.get_todo_tasks_for_list_channel(10, guild_id=1)[0]["title"] == "Private"


def todo_fixture():
    guild = SimpleNamespace(id=2)
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild = 20, guild
    channel.permissions_for = Mock(return_value=discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True))
    guild.get_channel = lambda channel_id: channel if channel_id == 20 else None
    bot = SimpleNamespace(state=bot_state(1), get_channel=guild.get_channel)
    cog = TodoCog(bot)
    cog._list_channels_in_category = lambda _: [channel]
    member = Mock(spec=discord.Member)
    member.guild, member.id = guild, 5
    member.guild_permissions = discord.Permissions.none()
    return cog, guild, channel, member


def test_public_todo_rejects_foreign_and_nonlist_channels(isolated_storage):
    cog, guild, channel, member = todo_fixture()
    assert cog.can_use_list(guild, member, 20)
    foreign = Mock(spec=discord.TextChannel)
    foreign.id, foreign.guild = 10, SimpleNamespace(id=1)
    assert cog._resolve_list_channel_id_from_context(guild, channel, foreign) is None
    assert not cog.can_use_list(guild, member, 10)
    channel.permissions_for.return_value = discord.Permissions.none()
    assert not cog.can_use_list(guild, member, 20)
    assert not can_manage_lists(guild, member)
    member.guild_permissions = discord.Permissions(manage_channels=True)
    assert can_manage_lists(guild, member)


def test_foreign_modals_and_persistent_board_cannot_read_private_tasks(isolated_storage):
    async def run():
        cog, guild, channel, member = todo_fixture()
        cog._load_items = Mock(side_effect=AssertionError("must not read private tasks"))
        event = SimpleNamespace(guild=guild, channel=channel, user=member,
                                response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
        await AddTaskModal(cog, 1, 10).callback(event)
        await ThreadEditTaskModal(cog, 10, 1, "title", "notes").callback(event)
        await ListCreateModal(cog, 1).callback(event)
        await BoardView(cog, 1, 10).children[0].callback(event)
        cog._load_items.assert_not_called()
        event.response.send_modal.assert_not_awaited()
        assert event.response.send_message.await_count == 4
    asyncio.run(run())


def test_public_guilds_are_not_provisioned_automatically(isolated_storage):
    cog, guild, _, _ = todo_fixture()
    cog.bot.guilds = [guild]
    cog.ensure_lists_for_guild = AsyncMock()
    asyncio.run(cog.ensure_board_for_all_guilds())
    cog.ensure_lists_for_guild.assert_not_awaited()
    storage.set_todo_category_id_for_guild(guild.id, 99)
    asyncio.run(cog.ensure_board_for_all_guilds())
    cog.ensure_lists_for_guild.assert_awaited_once_with(guild)


def test_two_servers_create_independent_task_histories(isolated_storage):
    async def run():
        cog, guild, channel, member = todo_fixture()
        other = SimpleNamespace(id=1)
        other_channel = Mock(spec=discord.TextChannel)
        other_channel.id, other_channel.guild = 10, other
        other_channel.permissions_for = channel.permissions_for
        other.get_channel = lambda cid: other_channel if cid == 10 else None
        other_member = Mock(spec=discord.Member)
        other_member.id, other_member.guild = 6, other
        channels = {10: other_channel, 20: channel}
        cog.bot.get_channel = channels.get
        cog._list_channels_in_category = lambda g: [other_channel if g.id == 1 else channel]
        cog.refresh_board, cog.refresh_hub = AsyncMock(), AsyncMock()
        for ch in channels.values():
            thread = Mock(spec=discord.Thread)
            thread.id, thread.mention = ch.id + 100, f"<#{ch.id + 100}>"
            thread.send, thread.add_user = AsyncMock(), AsyncMock()
            ch.create_thread = AsyncMock(return_value=thread)
        private, _ = await cog._create_task(other, 10, "Private", "Private notes", 6, other_member)
        public, _ = await cog._create_task(guild, 20, "Public", "Public notes", 5, member)
        assert private.id == public.id == 1
        assert cog.find_task_by_thread(guild, private.thread_id) == (None, [], None)
        assert cog.find_task_by_thread(other, public.thread_id) == (None, [], None)
        assert storage.get_todo_tasks_for_list_channel(10, guild_id=1)[0]["title"] == "Private"
        assert storage.get_todo_tasks_for_list_channel(20, guild_id=2)[0]["title"] == "Public"
        storage.clear_todo_tasks_for_list_channel(20)
        assert cog._load_items(10)[0].title == "Private"
    asyncio.run(run())
