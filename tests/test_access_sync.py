import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from mitra_bot.discord_app.bot_factory import MitraBot
from mitra_bot.discord_app.interaction_routing import command_route
from mitra_bot.services.access_sync import apply_peer_access, sync_peer_access
from mitra_bot.services.peer_service import PeerError
from mitra_bot.storage import config_store


@pytest.fixture
def access_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("MITRA_CONFIG_PATH", str(path))
    path.write_text('[bot]\ninfrastructure_guild_ids = [99]\ncustom = "keep"\n'
                    '[ups]\nenabled = false\n[cloudflare]\nenabled = false\n'
                    '[extension]\nvalue = "preserved"\n', encoding="utf-8")
    return path


def local_bot():
    return SimpleNamespace(state=SimpleNamespace(infrastructure_guild_ids=(99,)),
                           pending_application_commands=[SimpleNamespace(name="power", guild_ids=[])],
                           application_commands=[SimpleNamespace(name="todo", guild_ids=None)])


def test_apply_persists_only_allowlist_and_updates_live_access(access_config):
    bot = local_bot()
    assert apply_peer_access(bot, {"infrastructure_guild_ids": [2, 1, 2]}) == {
        "saved": True, "infrastructure_guild_ids": [1, 2]}
    raw = config_store.tomllib.loads(access_config.read_text())
    assert raw["bot"] == {"infrastructure_guild_ids": [1, 2], "custom": "keep"}
    assert raw["extension"] == {"value": "preserved"}
    assert raw["ups"] == {"enabled": False}
    assert raw["cloudflare"] == {"enabled": False}
    assert bot.state.infrastructure_guild_ids == (1, 2)
    assert bot.pending_application_commands[0].guild_ids == [1, 2]
    assert bot.application_commands[0].guild_ids is None
    apply_peer_access(bot, {"infrastructure_guild_ids": []})
    assert bot.state.infrastructure_guild_ids == ()
    assert config_store.read_config_dict()["bot"]["infrastructure_guild_ids"] == []


@pytest.mark.parametrize("payload", [None, 1, "1", [True], [0], [-1], [2**64], ["1"]])
def test_bad_ids_do_not_change_access(access_config, payload):
    before = access_config.read_bytes()
    bot = local_bot()
    with pytest.raises(ValueError):
        apply_peer_access(bot, {"infrastructure_guild_ids": payload})
    assert access_config.read_bytes() == before
    assert bot.state.infrastructure_guild_ids == (99,)


def test_unrelated_patch_and_failed_write_cannot_grant_access(access_config, monkeypatch):
    bot = local_bot()
    with pytest.raises(ValueError):
        apply_peer_access(bot, {"infrastructure_guild_ids": [1], "admin_role_name": "everyone"})
    before = access_config.read_bytes()
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(config_store, "_atomic_write_text", fail)
    with pytest.raises(OSError):
        apply_peer_access(bot, {"infrastructure_guild_ids": [1]})
    assert access_config.read_bytes() == before
    assert bot.state.infrastructure_guild_ids == (99,)


def test_sync_uses_owner_allowlist_and_checks_target_support():
    async def run():
        mesh = SimpleNamespace(is_state_owner=True, config=SimpleNamespace(node_id="owner", resolved_state_owner="owner"),
            resolve=lambda target: target, request=AsyncMock(side_effect=[
                {"operations": ["infrastructure_access"]}, {"saved": True, "infrastructure_guild_ids": [1]}]))
        bot = SimpleNamespace(peer_service=mesh, state=SimpleNamespace(infrastructure_guild_ids=(1,)))
        assert await sync_peer_access(bot, "peer") == [1]
        mesh.request.assert_awaited_with("peer", "infrastructure_access", {"infrastructure_guild_ids": [1]})
        mesh.request.reset_mock(side_effect=True)
        mesh.is_state_owner = False
        with pytest.raises(PeerError):
            await sync_peer_access(bot, "peer")
        mesh.request.assert_not_awaited()
        mesh.is_state_owner = True
        mesh.request.return_value = {"operations": []}
        with pytest.raises(PeerError, match="Update the target"):
            await sync_peer_access(bot, "peer")
        mesh.request.assert_awaited_once_with("peer", "capabilities", {})
        assert command_route({"name": "servers", "options": [{"name": "sync-access", "type": 1}]}, mesh) == ("owner", False)
    asyncio.run(run())


def test_stale_peer_does_not_steal_owner_command_or_todo_components():
    async def run():
        bot = MitraBot(intents=discord.Intents.none())
        bot.state = SimpleNamespace(infrastructure_guild_ids=())
        bot.peer_service = SimpleNamespace(is_state_owner=False)
        event = SimpleNamespace(guild=SimpleNamespace(id=1), data={"name": "servers"},
            type=discord.InteractionType.application_command, id=1, created_at=datetime.now(timezone.utc),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        try:
            await bot.process_application_commands(event)
            event.response.defer.assert_not_awaited()
            event.type = discord.InteractionType.component
            event.data = {"custom_id": "todo"}
            bot.peer_service = None
            await bot.process_application_commands(event)
            event.response.defer.assert_not_awaited()
        finally:
            await bot.close()
    asyncio.run(run())
