import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mitra_bot.discord_app.cogs.power_cog import PowerActionView, PowerCog
from mitra_bot.discord_app.cogs.servers_cog import ServersCog
from mitra_bot.discord_app.cogs.ups_cog import UPSCog
from mitra_bot.discord_app.server_target import resolve_server
from mitra_bot.services.peer_service import PeerError


def test_disabled_network_never_falls_back_for_unknown_target():
    bot = SimpleNamespace()
    assert resolve_server(bot, None) == "local"
    with pytest.raises(PeerError):
        resolve_server(bot, "other")


def test_remote_power_never_calls_local_os():
    async def run():
        mesh = SimpleNamespace(config=SimpleNamespace(node_id="a"), power=AsyncMock())
        view = PowerActionView(action="restart", delay_seconds=60, force=True,
                               requester_id=1, channel_id=2, server="b", mesh=mesh)
        assert any(f.name == "Server" and f.value == "`b`" for f in view._build_embed(state="pending").fields)
        with patch("mitra_bot.discord_app.cogs.power_cog.execute_power_action") as local:
            with pytest.raises(PeerError, match="signed"):
                await view._execute("restart", confirmer_id=3)
            with pytest.raises(PeerError, match="signed"):
                await view._execute("cancel", confirmer_id=3)
            local.assert_not_called()
    asyncio.run(run())


def test_cancel_unconfirmed_view_does_not_cancel_os_action():
    async def run():
        view = PowerActionView(action="shutdown", delay_seconds=0, force=False, requester_id=1, channel_id=2)
        view._is_admin_user = lambda interaction: True
        view._execute = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(defer=AsyncMock()),
                                      edit_original_response=AsyncMock())
        await view.cancel_button.callback(interaction)
        assert view.canceled
        view._execute.assert_not_awaited()
    asyncio.run(run())


def test_power_confirmation_requires_admin():
    async def run():
        view = PowerActionView(action="shutdown", delay_seconds=0, force=False, requester_id=1, channel_id=2)
        view._is_admin_user = lambda interaction: False
        view._execute = AsyncMock()
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await view.confirm_button.callback(interaction)
        await view.cancel_button.callback(interaction)
        assert interaction.response.send_message.await_count == 2
        view._execute.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("cls,command,args,module", [
    (PowerCog, "restart", (60, False, "b"), "power_cog"),
    (PowerCog, "shutdown", (60, False, "b"), "power_cog"),
    (PowerCog, "cancel", ("b",), "power_cog"),
    (UPSCog, "status", (6, "b"), "ups_cog"),
    (ServersCog, "list_servers", (), "servers_cog"),
])
def test_remote_command_admin_denial(cls, command, args, module):
    async def run():
        cog = object.__new__(cls)
        denial = AsyncMock()
        ctx = Mock()
        with patch(f"mitra_bot.discord_app.cogs.{module}.ensure_admin", return_value=denial()):
            await getattr(cls, command).callback(cog, ctx, *args)
        denial.assert_awaited_once()
    asyncio.run(run())


def test_offline_ups_uses_only_named_peer_history():
    async def run():
        cog = object.__new__(UPSCog)
        data = dict(captured_at=1788868800, rows=[{"ts": "2026-09-08T12:00:00Z"}],
                    live={"battery_percent": 45}, timezone="UTC", available=True)
        mesh = SimpleNamespace(snapshot=AsyncMock(return_value=(data, True)))
        ctx = SimpleNamespace(respond=AsyncMock())
        with patch("mitra_bot.discord_app.cogs.ups_cog.build_ups_status_graph", return_value=None) as graph:
            await cog._remote_status(ctx, mesh, "server-b", 24)
        mesh.snapshot.assert_awaited_once_with("server-b")
        embed = ctx.respond.call_args.kwargs["embed"]
        assert "server-b" in embed.title and "cached" in embed.description
        assert str(data["captured_at"]) in embed.description
        assert graph.call_args.args[0] == data["rows"]
    asyncio.run(run())
