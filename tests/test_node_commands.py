import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from mitra_bot.discord_app.cogs.about_cog import AboutCog
from mitra_bot.discord_app.cogs.ip_cog import IPCog
from mitra_bot.discord_app.cogs.ups_cog import UPSCog
from mitra_bot.discord_app.interaction_routing import command_route
from mitra_bot.discord_app.node_commands import local_operation, selected_nodes
from mitra_bot.discord_app.peer_dashboard import dashboard_view, handle_dashboard_component
from mitra_bot.services.peer_service import PeerError


def bot_mesh():
    mesh = SimpleNamespace(config=SimpleNamespace(node_id="mitra", resolved_state_owner="mitra"),
                           peers={"test": object()}, request=AsyncMock())
    def resolve(node):
        if node not in (None, "mitra", "test"):
            raise PeerError("Unknown server")
        return node or "mitra"
    mesh.resolve = resolve
    return SimpleNamespace(peer_service=mesh)


def test_command_schema_preserves_boolean_and_optional_node_options():
    async def register():
        bot = discord.Bot()
        ups = UPSCog.__new__(UPSCog)
        ups.bot = bot
        for cog in (IPCog(bot), AboutCog(bot), ups):
            bot.add_cog(cog)
        return bot
    bot = asyncio.run(register())
    commands = [bot.get_cog("IPCog").status, bot.get_cog("AboutCog").about,
                bot.get_cog("UPSCog").monitoring, bot.get_cog("UPSCog").timezone]
    for command in commands:
        options = {o.name: o.to_dict() for o in command.options}
        assert options["server"]["type"] == 3
        assert not options["server"]["required"]
    assert next(o for o in commands[2].options if o.name == "enabled").to_dict()["type"] == 5


def test_selected_nodes_and_command_routing():
    bot = bot_mesh()
    assert selected_nodes(bot, None, default_all=True) == ["mitra", "test"]
    assert selected_nodes(bot, "test") == ["test"]
    assert selected_nodes(SimpleNamespace(), "all") == ["local"]
    with pytest.raises(PeerError):
        selected_nodes(bot, "unknown")
    for name in ("monitoring", "timezone"):
        data = {"name": "ups", "options": [{"type": 1, "name": name,
                "options": [{"name": "server", "value": "test"}]}]}
        assert command_route(data, bot.peer_service) == ("mitra", False)
    assert command_route({"name": "ip", "options": [{"type": 1, "name": "status"}]}, bot.peer_service) == ("mitra", True)


@pytest.mark.parametrize("payload", [{"enabled": "false"}, {"enabled": True, "timezone": "UTC"},
                                      {"timezone": "not/a/timezone"}, {"auto_shutdown_enabled": True}])
def test_ups_rejects_invalid_remote_settings_without_writing(payload):
    cog = object.__new__(UPSCog)
    with patch("mitra_bot.discord_app.cogs.ups_cog.set_ups_config") as save:
        with pytest.raises(PeerError):
            cog.apply_settings(payload)
        save.assert_not_called()


def test_ups_remote_settings_do_not_modify_coordinator():
    async def run():
        bot = bot_mesh()
        bot.peer_service.request.return_value = {"message": "UPS monitoring disabled."}
        cog = object.__new__(UPSCog)
        cog.bot = bot
        ctx = SimpleNamespace(defer=AsyncMock(), respond=AsyncMock())
        with patch("mitra_bot.discord_app.cogs.ups_cog.ensure_admin", return_value=None), \
             patch("mitra_bot.discord_app.cogs.ups_cog.set_ups_config") as save:
            await UPSCog.monitoring.callback(cog, ctx, False, "test")
            save.assert_not_called()
        bot.peer_service.request.assert_awaited_once_with("test", "ups_settings", {"enabled": False}, timeout=15)
        assert "test" in ctx.respond.call_args.args[0]
    asyncio.run(run())


def test_ups_admin_guard_blocks_remote_changes():
    async def run():
        cog = object.__new__(UPSCog)
        cog.bot = bot_mesh()
        ctx = SimpleNamespace(respond=AsyncMock())
        with patch("mitra_bot.discord_app.cogs.ups_cog.ensure_admin", side_effect=lambda ctx: ctx.respond("Denied")):
            await UPSCog.monitoring.callback(cog, ctx, False, "test")
        cog.bot.peer_service.request.assert_not_awaited()
    asyncio.run(run())


def test_target_settings_reload_and_persist():
    async def run():
        cog = object.__new__(UPSCog)
        cog._reload_from_cache = Mock()
        bot = SimpleNamespace(get_cog=lambda name: cog)
        with patch("mitra_bot.discord_app.cogs.ups_cog.set_ups_config") as save:
            await local_operation(bot, "ups_settings", {"timezone": "America/Los_Angeles"})
            save.assert_called_once_with({"timezone": "America/Los_Angeles"})
        cog._reload_from_cache.assert_called_once()
    asyncio.run(run())


def test_ip_all_retains_success_when_another_node_is_unavailable():
    async def run():
        bot = bot_mesh()
        bot.peer_service.request.side_effect = PeerError("offline")
        ctx = SimpleNamespace(defer=AsyncMock(), respond=AsyncMock())
        with patch("mitra_bot.discord_app.node_commands.get_public_ip", return_value="203.0.113.1"):
            await IPCog.status.callback(IPCog(bot), ctx, "all")
        message = ctx.respond.call_args.args[0]
        assert "mitra" in message and "203.0.113.1" in message
        assert "```\n203.0.113.1\n```" in message
        assert "test" in message and "Unavailable" in message
    asyncio.run(run())


def test_about_displays_each_nodes_own_version():
    async def run():
        bot = bot_mesh()
        bot.peer_service.request.return_value = dict(version="remote-version", python="3.10", pycord="2.7",
            discord_servers=1, health=dict(process_uptime_seconds=120, discord_connected=True))
        ctx = SimpleNamespace(defer=AsyncMock(), respond=AsyncMock())
        await AboutCog.about.callback(AboutCog(bot), ctx, "test")
        field = ctx.respond.call_args.kwargs["embed"].fields[0]
        assert field.name == "test" and "remote-version" in field.value and "2m" in field.value
    asyncio.run(run())


def test_dashboard_perspective_is_optional_and_controls_explain_selection():
    async def run():
        mesh = bot_mesh().peer_service
        simple = dashboard_view(mesh, "test", "mitra", 24, 0)
        assert len([c for c in simple.children if isinstance(c, discord.ui.Select)]) == 1
        advanced = dashboard_view(mesh, "test", "mitra", 24, 0, advanced=True)
        selects = [c for c in advanced.children if isinstance(c, discord.ui.Select)]
        assert [next(o.label for o in s.options if o.default) for s in selects] == ["Show: test", "Measured from: mitra"]
        assert all(len(c.custom_id) <= 100 for c in advanced.children)
        assert all(":advanced" in c.custom_id for c in advanced.children)
    asyncio.run(run())


def test_dashboard_toggle_and_refresh_retain_perspective():
    async def run():
        bot = bot_mesh()
        bot.user = SimpleNamespace(id=123)
        bot.state = SimpleNamespace(admin_role_name="Mitra Admin")
        view = dashboard_view(bot.peer_service, "test", "mitra", 24, 0)
        toggle = next(c for c in view.children if c.custom_id.endswith(":perspective"))
        event = SimpleNamespace(id=123, guild=object(), user=Mock(spec=discord.Member),
            message=SimpleNamespace(author=bot.user), data={"custom_id": toggle.custom_id},
            followup=SimpleNamespace(send=AsyncMock()))
        with patch("mitra_bot.discord_app.peer_dashboard.claim_interaction", new=AsyncMock(return_value=True)), \
             patch("mitra_bot.discord_app.peer_dashboard.member_has_role", return_value=True), \
             patch("mitra_bot.discord_app.peer_dashboard.build_dashboard", new=AsyncMock(return_value={})) as render:
            await handle_dashboard_component(bot, event)
            render.assert_awaited_with(bot.peer_service, "test", "mitra", 24, 0, advanced=True)
            advanced = dashboard_view(bot.peer_service, "test", "mitra", 24, 0, advanced=True)
            event.data["custom_id"] = next(c.custom_id for c in advanced.children if c.custom_id.endswith(":refresh"))
            await handle_dashboard_component(bot, event)
            render.assert_awaited_with(bot.peer_service, "test", "mitra", 24, 0, advanced=True)
            event.data["custom_id"] = next(c.custom_id for c in advanced.children if c.custom_id.endswith(":perspective"))
            await handle_dashboard_component(bot, event)
            render.assert_awaited_with(bot.peer_service, "test", "mitra", 24, 0, advanced=False)
    asyncio.run(run())
