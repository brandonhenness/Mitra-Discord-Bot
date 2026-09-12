import io
import json
import sqlite3
import zipfile
from unittest.mock import Mock, MagicMock
from types import SimpleNamespace

import pytest

from mitra_bot import cloudflare_setup, setup_health
from mitra_bot.services import update_service as updater


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    (root / "README.md").write_text("old")
    (root / ".env").write_text("private")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("release/README.md", "new")
        z.writestr("release/mitra_bot/new_module.py", "new module")
        z.writestr("release/.env", "do not install")
    monkeypatch.setattr(updater, "PROJECT_ROOT", root)
    monkeypatch.setattr(updater.requests, "get", Mock(return_value=Mock(content=archive.getvalue())))
    monkeypatch.setattr(updater, "_install_requirements", Mock())
    monkeypatch.setattr(updater, "_startup_preflight", Mock())
    monkeypatch.setattr(updater, "set_updater_config", Mock())
    return root, updater.ReleaseInfo("1.0.0", "url", "url", "")


@pytest.mark.parametrize("failure", ["copy", "dependencies", "preflight"])
def test_failed_update_restores_previous_files(installation, monkeypatch, failure):
    root, release = installation
    if failure == "copy":
        def partial(*_):
            (root / "README.md").write_text("partial")
            raise OSError("disk error")
        monkeypatch.setattr(updater, "_copy_release_tree", partial)
    elif failure == "dependencies":
        updater._install_requirements.side_effect = [RuntimeError("dependency failure"), None]
    else:
        updater._startup_preflight.side_effect = RuntimeError("import failure")
    result = updater.install_release(release)
    assert not result.ok
    assert (root / "README.md").read_text() == "old"
    assert (root / ".env").read_text() == "private"
    assert not (root / "mitra_bot/new_module.py").exists()
    manifest = next((root / ".recovery").glob("*/manifest.json"))
    assert json.loads(manifest.read_text())["stage"] == "rolled_back"
    updater.set_updater_config.assert_not_called()
    assert updater._install_requirements.call_count == (0 if failure == "copy" else 2)


def test_failed_dependency_recovery_keeps_backup(installation):
    root, release = installation
    updater._install_requirements.side_effect = RuntimeError("failure")
    result = updater.install_release(release)
    assert not result.ok and "dependencies need repair" in result.error
    assert (root / "README.md").read_text() == "old"
    backup = next((root / ".recovery").glob("*/files/README.md"))
    assert backup.read_text() == "old"
    updater.set_updater_config.assert_not_called()


def test_successful_update_requires_preflight_and_keeps_backup(installation):
    root, release = installation
    result = updater.install_release(release)
    assert result.ok
    updater._startup_preflight.assert_called_once_with("1.0.0")
    updater.set_updater_config.assert_called_once()
    assert (root / "README.md").read_text() == "new"
    assert (root / ".env").read_text() == "private"
    assert next((root / ".recovery").glob("*/files/README.md")).read_text() == "old"


def test_overlapping_update_is_rejected(installation):
    _, release = installation
    with updater._INSTALL_LOCK:
        assert "already running" in updater.install_release(release).error
    updater.requests.get.assert_not_called()


def test_restoration_failure_reports_retained_backup(installation, monkeypatch):
    from mitra_bot.services.update_recovery import Recovery
    root, release = installation
    updater._startup_preflight.side_effect = RuntimeError("broken import")
    monkeypatch.setattr(Recovery, "restore", Mock(side_effect=OSError("disk error")))
    result = updater.install_release(release)
    assert not result.ok and "file restoration failed" in result.error
    assert next((root / ".recovery").glob("*/files/README.md")).read_text() == "old"
    updater.set_updater_config.assert_not_called()


def test_doctor_rejects_wrong_peer_certificate(monkeypatch):
    context = MagicMock()
    context.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = b"wrong certificate"
    monkeypatch.setattr(setup_health.ssl, "SSLContext", Mock(return_value=context))
    monkeypatch.setattr(setup_health.socket, "create_connection", MagicMock())
    config = SimpleNamespace(ca_file="ca", cert_file="cert", key_file="key")
    peer = SimpleNamespace(host="peer", port=9843, fingerprint="0" * 64)
    with pytest.raises(ValueError, match="certificate"):
        setup_health.peer_connection(config, peer)
    context.load_verify_locations.assert_called_once_with(cafile="ca")
    context.load_cert_chain.assert_called_once_with("cert", "key")


def test_cloudflare_cancel_is_distinct_from_failure(monkeypatch):
    monkeypatch.setattr(cloudflare_setup, "configure_cloudflare", Mock(return_value=None))
    assert cloudflare_setup.run_cloudflare_setup() is None


def test_database_check_does_not_create_missing_file(tmp_path):
    missing = tmp_path / "absent.db"
    with pytest.raises(ValueError):
        setup_health.database_check(missing)
    assert not missing.exists()
    db = tmp_path / "ups.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE ups_samples (ts REAL)")
    setup_health.database_check(db)


def test_health_checks_redact_errors_and_do_not_write_dns(tmp_path, monkeypatch):
    from mitra_bot.storage import config_store
    from mitra_bot.peer_config import PeerConfig
    from mitra_bot.setup_wizard import DiscordSetup
    from mitra_bot.services import cloudflare_verify
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_APPLICATION_TOKEN", "secret-token")
    cfg = config_store.FileConfigModel().model_dump(mode="json")
    cfg["ups"]["enabled"] = False
    cfg["cloudflare"] = dict(enabled=True, targets=[])
    monkeypatch.setattr(config_store, "read_config_dict", lambda: cfg)
    monkeypatch.setattr("mitra_bot.peer_config.load_peer_config", lambda: PeerConfig())
    monkeypatch.setattr(DiscordSetup, "application", Mock(side_effect=RuntimeError("secret-token")))
    verify = Mock(return_value=0)
    monkeypatch.setattr(cloudflare_verify, "verify_targets", verify)
    rows = dict(setup_health.health_results())
    assert rows["Discord token"].startswith("CHECK")
    assert rows["Cloudflare"].startswith("CHECK")
    assert "secret-token" not in str(rows)
    assert verify.call_args.kwargs == {"write": False}
