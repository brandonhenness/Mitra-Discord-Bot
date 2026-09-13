import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from mitra_bot.discord_app.cogs import update_cog
from mitra_bot.services.update_service import InstallResult, ReleaseInfo


@pytest.mark.parametrize("ok", [True, False])
def test_install_button_edits_private_prompt_through_interaction(monkeypatch, ok):
    async def run():
        release = ReleaseInfo("v0.2.0b5", "https://example.com/update.zip", "https://example.com/release", "")
        installer = Mock(return_value=InstallResult(ok=ok, version=release.version, error=None if ok else "test failure"))
        monkeypatch.setattr(update_cog, "install_release", installer)
        bot = SimpleNamespace(state=SimpleNamespace(admin_role_name="Mitra Admin"))
        cog = update_cog.UpdateCog(bot)
        cog._restart_after_update = AsyncMock()
        view = update_cog.UpdatePromptView(cog, release, source="manual-check")
        user = Mock(spec=discord.Member)
        user.roles = [SimpleNamespace(name="Mitra Admin")]
        message = SimpleNamespace(edit=AsyncMock(side_effect=AssertionError("ephemeral messages cannot use channel edits")))
        interaction = SimpleNamespace(
            guild=object(), user=user, client=bot, message=message,
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )
        await view.children[0].callback(interaction)
        interaction.response.defer.assert_awaited_once()
        titles = [c.kwargs["embed"].title for c in interaction.edit_original_response.call_args_list]
        assert titles == ["Installing update", "Update installed" if ok else "Update failed"]
        assert all(c.kwargs["view"] is None for c in interaction.edit_original_response.call_args_list)
        message.edit.assert_not_awaited()
        installer.assert_called_once_with(release)
        assert cog._restart_after_update.await_count == int(ok)
        assert view.is_finished()
    asyncio.run(run())


def test_busy_install_uses_private_followup(monkeypatch):
    async def run():
        cog = update_cog.UpdateCog(SimpleNamespace())
        installer = Mock()
        monkeypatch.setattr(update_cog, "install_release", installer)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        message = SimpleNamespace(reply=AsyncMock())
        async with cog._install_lock:
            await cog.install_release_with_feedback(release=None, message=message, source="test", interaction=interaction)
        interaction.followup.send.assert_awaited_once()
        assert "An update install is already in progress." in interaction.followup.send.call_args.args[0]
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        message.reply.assert_not_awaited()
        installer.assert_not_called()
    asyncio.run(run())


def test_non_admin_button_never_installs(monkeypatch):
    async def run():
        installer = Mock()
        monkeypatch.setattr(update_cog, "install_release", installer)
        cog = update_cog.UpdateCog(SimpleNamespace())
        view = update_cog.UpdatePromptView(cog, None, source="test")
        interaction = SimpleNamespace(guild=None, user=None, response=SimpleNamespace(send_message=AsyncMock()))
        await view.children[0].callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        installer.assert_not_called()
        view.stop()
    asyncio.run(run())


def test_button_errors_reach_visible_error_handler(monkeypatch):
    async def run():
        handler = AsyncMock()
        monkeypatch.setattr(update_cog, "report_command_error", handler)
        ctx = object()
        bot = SimpleNamespace(get_application_context=AsyncMock(return_value=ctx))
        view = update_cog.UpdatePromptView(update_cog.UpdateCog(bot), None, source="test")
        error = RuntimeError("failure")
        await view.on_error(error, view.children[0], SimpleNamespace(client=bot))
        handler.assert_awaited_once_with(ctx, error)
        view.stop()
    asyncio.run(run())
