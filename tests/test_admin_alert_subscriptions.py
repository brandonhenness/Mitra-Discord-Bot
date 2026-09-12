import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from test_unified_alerts import fixture
from mitra_bot.services.alert_roles import subscription
from mitra_bot.discord_app.cogs.servers_cog import ServersCog


def context(admin=False):
    bot, guild, actor, shared, ip, peer = fixture()
    actor.id = 99
    if admin:
        actor.roles.append(SimpleNamespace(name="Admin"))
    target = Mock(spec=discord.Member)
    target.id, target.guild, target.mention = 100, guild, "<@100>"
    target.add_roles, target.remove_roles = AsyncMock(), AsyncMock()
    ctx = SimpleNamespace(bot=bot, guild=guild, author=actor, defer=AsyncMock(), respond=AsyncMock())
    return ctx, target, shared, ip, peer


@pytest.mark.parametrize("subscribe", [True, False])
def test_admin_changes_only_selected_members_subscription(subscribe):
    ctx, target, shared, ip, peer = context(admin=True)
    asyncio.run(subscription(ctx, subscribe, target))
    if subscribe:
        target.add_roles.assert_awaited_once_with(shared, reason="Subscribed to all Mitra alerts by administrator 99")
    else:
        target.remove_roles.assert_awaited_once_with(shared, ip, peer, reason="Unsubscribed from all Mitra alerts by administrator 99")
    ctx.author.add_roles.assert_not_awaited()
    ctx.author.remove_roles.assert_not_awaited()
    assert ctx.respond.call_args.kwargs["ephemeral"] is True
    assert ctx.respond.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert target.mention in ctx.respond.call_args.args[0]


@pytest.mark.parametrize("subscribe", [True, False])
def test_non_admin_cannot_supply_user_option(subscribe):
    ctx, target, *_ = context()
    asyncio.run(subscription(ctx, subscribe, target))
    target.add_roles.assert_not_awaited()
    target.remove_roles.assert_not_awaited()
    ctx.defer.assert_not_awaited()
    assert "permission" in ctx.respond.call_args.args[0]


def test_explicit_user_option_is_admin_only_even_for_self():
    ctx, *_ = context()
    asyncio.run(subscription(ctx, True, ctx.author))
    ctx.author.add_roles.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["other_guild", "not_member"])
def test_admin_cannot_target_nonmember(invalid):
    ctx, target, *_ = context(admin=True)
    if invalid == "other_guild":
        target.guild = SimpleNamespace(id=777)
    else:
        target = Mock(spec=discord.User)
    asyncio.run(subscription(ctx, True, target))
    assert "Choose a member" in ctx.respond.call_args.args[0]
    ctx.defer.assert_not_awaited()


def test_discord_registers_optional_member_picker():
    async def run():
        bot = discord.Bot()
        bot.add_cog(ServersCog(bot))
        group = bot.get_cog("ServersCog").alerts_group
        for command in group.subcommands:
            if command.name not in {"subscribe", "unsubscribe"}:
                continue
            assert len(command.options) == 1
            option = command.options[0].to_dict()
            assert option["name"] == "user" and option["type"] == 6
            assert option["required"] is False
        await bot.close()
    asyncio.run(run())
