import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from mitra_bot.discord_app.cogs.servers_cog import ServersCog
from mitra_bot.discord_app.command_errors import report_command_error


def test_registered_server_commands_use_discord_option_types():
    async def registered():
        bot = discord.Bot()
        bot.add_cog(ServersCog(bot))
        return bot
    bot = asyncio.run(registered())
    commands = {c.name: c for c in bot.get_cog("ServersCog").servers.subcommands}
    # Inspect the payload sent to Discord, not callbacks invoked directly by tests.
    alerts = {"options": [o.to_dict() for o in commands["alerts"].options]}
    assert {o["name"]: o["type"] for o in alerts["options"]} == {
        "channel": 7, "enabled": 5,
    }
    assert {o["name"] for o in alerts["options"] if o["required"]} == {"channel"}
    assert commands["doctor"].options == []
    assert commands["list"].options == []
    for command in commands.values():
        for option in command.options:
            types = option._raw_type if isinstance(option._raw_type, tuple) else (option._raw_type,)
            assert all(isinstance(t, type) for t in types), (command.name, option.name)
            assert option.name not in {"ctx", "self"}
    pin = {o.name: o for o in commands["dashboard-pin"].options}
    assert pin["channel"].input_type == discord.SlashCommandOptionType.channel
    assert pin["interval"].input_type == discord.SlashCommandOptionType.integer
    assert (pin["interval"].min_value, pin["interval"].max_value) == (60, 3600)
    mention = {o.name: o for o in commands["alerts-test"].options}["mention"]
    assert mention.input_type == discord.SlashCommandOptionType.boolean
    assert mention.default is False


@pytest.mark.parametrize("deferred", [False, True])
def test_command_error_finishes_response_and_logs_original_traceback(deferred, caplog):
    interaction = SimpleNamespace(
        id=12345, response=SimpleNamespace(is_done=Mock(return_value=deferred)),
        edit_original_response=AsyncMock(),
    )
    ctx = SimpleNamespace(interaction=interaction, respond=AsyncMock(),
                          command=SimpleNamespace(qualified_name="servers alerts"))
    try:
        raise TypeError("private diagnostic detail")
    except TypeError as original:
        wrapped = discord.ApplicationCommandInvokeError(original)
    asyncio.run(report_command_error(ctx, wrapped))
    if deferred:
        interaction.edit_original_response.assert_awaited_once()
        ctx.respond.assert_not_awaited()
        message = interaction.edit_original_response.call_args.kwargs["content"]
    else:
        ctx.respond.assert_awaited_once()
        assert ctx.respond.call_args.kwargs["ephemeral"] is True
        message = ctx.respond.call_args.args[0]
    assert "12345" in message and "private diagnostic detail" not in message
    assert any(r.exc_info and r.exc_info[2] for r in caplog.records)


def test_error_delivery_failure_is_logged_without_retry(caplog):
    interaction = SimpleNamespace(
        id=12345, response=SimpleNamespace(is_done=lambda: True),
        edit_original_response=AsyncMock(side_effect=OSError("disconnected")),
    )
    ctx = SimpleNamespace(interaction=interaction, command=None)
    asyncio.run(report_command_error(ctx, ValueError("failure")))
    interaction.edit_original_response.assert_awaited_once()
    assert "Could not deliver command error" in caplog.text
