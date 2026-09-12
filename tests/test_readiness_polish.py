import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from mitra_bot import setup_wizard
from mitra_bot.discord_app.node_commands import node_operation
from mitra_bot.services.peer_service import PeerError
from mitra_bot.services.update_recovery import prune_successful_backups
import pytest


def test_picker_keeps_saved_choice_even_when_not_first(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert setup_wizard.choose("Channel", ["general", "mitra"], str, default=2) == "mitra"
    assert setup_wizard.choose("Channel", ["general"], str, default=0) is None


def test_setup_retains_saved_guild_and_channel_when_api_order_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_APPLICATION_TOKEN", raising=False)
    monkeypatch.setenv("MITRA_CONFIG_PATH", str(tmp_path/"config.toml"))
    (tmp_path/".env").write_text("DISCORD_APPLICATION_TOKEN=" + "x"*40 + "\n")
    setup_wizard.write_config_dict({"bot": {"guild_id": 456, "channel_id": 999}})
    monkeypatch.setattr(setup_wizard.DiscordSetup, "application", Mock(return_value={"id": "123"}))
    def request(self, method, path, **kwargs):
        return {"/users/@me/guilds": [{"id":"111","name":"Other"}, {"id":"456","name":"Saved"}],
                "/guilds/456": {"owner_id":"789"},
                "/guilds/456/channels": [{"id":"222","name":"general","type":0},
                                         {"id":"999","name":"mitra","type":0}]}[path]
    monkeypatch.setattr(setup_wizard.DiscordSetup, "request", request)
    monkeypatch.setattr("mitra_bot.setup_health.show_health", Mock())
    monkeypatch.setattr("mitra_bot.storage.storage_store.get_notification_channel_id_for_guild", Mock(return_value=999))
    save = Mock()
    monkeypatch.setattr("mitra_bot.storage.storage_store.set_notification_channel_id_for_guild", save)
    answers = iter(["n", "n", "n", "", "", "n", "n", "1", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    setup_wizard.guided_setup(open_browser=False)
    save.assert_called_once_with(456, 999)
    assert setup_wizard.read_config_dict()["bot"]["guild_id"] == 456


def test_retention_keeps_recent_failed_and_manual_backups(tmp_path):
    root = tmp_path/".recovery"
    root.mkdir()
    now = time.time()
    for i in range(6):
        folder = root / ("update-" + f"{i:032x}")
        folder.mkdir()
        manifest = folder/"manifest.json"
        manifest.write_text(json.dumps({"stage": "validated" if i < 5 else "dependency_recovery_failed"}))
        os.utime(manifest, (now-(60-i)*86400,)*2)
    manual = root/"node-backup-01"
    manual.mkdir()
    removed = prune_successful_backups(tmp_path, now=now)
    assert set(removed) == {"update-"+f"{i:032x}" for i in (0,1)}
    assert manual.exists()
    assert (root/("update-"+f"{5:032x}")).exists()


def test_supported_peer_rejection_does_not_claim_upgrade_needed():
    async def run():
        error = PeerError("Permission rejected")
        mesh = SimpleNamespace(config=SimpleNamespace(node_id="a"), request=AsyncMock(side_effect=[
            error, {"version": "1.0.0", "operations": ["ups_settings"]}]))
        with pytest.raises(PeerError, match="Permission rejected"):
            await node_operation(SimpleNamespace(peer_service=mesh), "b", "ups_settings", {"enabled": True})
    asyncio.run(run())


def test_reachable_old_peer_explains_upgrade():
    async def run():
        mesh = SimpleNamespace(config=SimpleNamespace(node_id="a"), request=AsyncMock(side_effect=[
            PeerError("rejected"), PeerError("rejected"), {"node_id": "b"}]))
        with pytest.raises(PeerError, match="reachable.*Update this node"):
            await node_operation(SimpleNamespace(peer_service=mesh), "b", "public_ip")
    asyncio.run(run())


def test_log_rotation_is_bounded_and_reconfiguration_closes_old_handler(tmp_path):
    import logging
    from logging.handlers import RotatingFileHandler
    from mitra_bot.logging_setup import setup_logging
    root = logging.Logger("isolated")
    from unittest.mock import patch
    with patch("mitra_bot.logging_setup.logging.getLogger", return_value=root):
        setup_logging(logfile=str(tmp_path/"bot.log"))
        handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
        assert (handler.maxBytes, handler.backupCount) == (10*1024*1024, 5)
        setup_logging(add_file_handler=False)
        assert handler.stream is None
        for h in root.handlers:
            h.close()
