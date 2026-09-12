import json
import sqlite3
from pathlib import Path

import pytest

from mitra_bot.recovery import backup, restore


def installation(tmp_path, monkeypatch):
    for key in ("MITRA_CONFIG_PATH", "MITRA_STATE_PATH", "MITRA_PEER_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "node"
    root.mkdir()
    (root/"config.toml").write_text('[ups]\ndatabase_file = "ups.db"\n')
    (root/".env").write_text("DISCORD_APPLICATION_TOKEN=test-secret\n")
    with sqlite3.connect(root/"state.db") as db:
        db.execute("CREATE TABLE saved(value TEXT)")
        db.execute("INSERT INTO saved VALUES ('preserved')")
    return root


def test_backup_restore_preserves_config_credentials_and_database(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    saved, recovered = tmp_path/"backup", tmp_path/"restored"
    manifest = backup(root, saved)
    assert {entry["path"] for entry in manifest["files"]} == {"config.toml", ".env", "state.db"}
    restore(saved, recovered)
    assert (recovered/".env").read_bytes() == (root/".env").read_bytes()
    with sqlite3.connect(recovered/"state.db") as db:
        assert db.execute("SELECT value FROM saved").fetchone() == ("preserved",)
    with pytest.raises(FileExistsError):
        restore(saved, recovered)


def test_tampered_backup_restores_nothing(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    saved, recovered = tmp_path/"backup", tmp_path/"restored"
    backup(root, saved)
    (saved/"config.toml").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore(saved, recovered)
    assert not recovered.exists()


@pytest.mark.parametrize("bad", ["../escape", "C:/escape", "state.db/../../escape"])
def test_restore_rejects_traversal(tmp_path, monkeypatch, bad):
    root = installation(tmp_path, monkeypatch)
    saved = tmp_path/"backup"
    backup(root, saved)
    manifest = json.loads((saved/"manifest.json").read_text())
    manifest["files"][0]["path"] = bad
    (saved/"manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore(saved, tmp_path/"restored")
    assert not (tmp_path/"restored").exists()


def test_custom_paths_outside_installation_require_manual_backup(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    monkeypatch.setenv("MITRA_STATE_PATH", str(tmp_path/"outside.db"))
    with pytest.raises(ValueError, match="inside"):
        backup(root, tmp_path/"backup")
