from pathlib import Path
from unittest.mock import Mock

import pytest

from mitra_bot import setup_wizard as wizard


def answers(monkeypatch, values):
    replies = iter(values)
    monkeypatch.setattr(wizard.ui, "ask", lambda _: next(replies))


def test_yes_and_menu_do_not_accept_mistyped_answers(monkeypatch):
    answers(monkeypatch, ["maybe", "yes", "9", "3"])
    assert wizard.yes("Continue?", False)
    assert wizard.menu("Mode", ("1", "2", "3"), "1") == "3"


@pytest.mark.parametrize("selection", ["relative", "absolute", "parent", "file"])
def test_bundle_path_correction_and_supported_path_styles(monkeypatch, tmp_path, selection):
    root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "peer-bundles" / "Mitra"
    bundle.mkdir(parents=True)
    config = (root / "peer-network.example.toml").read_text().replace("enabled = false", "enabled = true", 1)
    (bundle / "peer-network.toml").write_text(config)
    for name in ("ca.crt", "node.crt", "node.key"):
        (bundle / name).write_text(name)
    monkeypatch.chdir(tmp_path)
    chosen = {"relative": "peer-bundles/Mitra", "absolute": f'"{bundle}"',
              "parent": "peer-bundles", "file": str(bundle / "peer-network.toml")}[selection]
    answers(monkeypatch, ["missing-folder", chosen, "1"])
    assert wizard.prompt_bundle(tmp_path / "installed").enabled
    assert (tmp_path / "installed" / "node.key").read_text() == "node.key"


def test_conflicting_bundle_can_be_deferred_without_overwriting(monkeypatch, tmp_path):
    (tmp_path / "peer-network.toml").write_text("exists")
    install = Mock(side_effect=ValueError("Existing node.key differs"))
    monkeypatch.setattr(wizard, "install_bundle", install)
    answers(monkeypatch, [str(tmp_path), ""])
    assert wizard.prompt_bundle(tmp_path) is None
    assert (tmp_path / "peer-network.toml").read_text() == "exists"


def test_network_reenters_invalid_names_and_addresses(monkeypatch):
    answers(monkeypatch, ["bad/name", "Mitra", "Mitra/50.54.188.194", "999.999.0.1",
                          "mitra.example.com", "Mitra", "Anubis", "https://example.com",
                          "203.0.113.2", ""])
    assert wizard.prompt_nodes() == {"Mitra": "mitra.example.com", "Anubis": "203.0.113.2"}


def test_discord_connection_failure_can_accept_a_different_token(monkeypatch):
    monkeypatch.setattr(wizard, "read_bot_token", Mock(side_effect=["x" * 40, "y" * 40]))
    monkeypatch.setattr(wizard.DiscordSetup, "application", Mock(side_effect=[RuntimeError("Connection failed"), {"id": "123"}]))
    answers(monkeypatch, ["2"])
    token, _, _ = wizard.authenticate_discord(None, Mock())
    assert token == "y" * 40


def test_guided_setup_reselects_discord_and_corrects_output_folder(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MITRA_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setattr(wizard, "authenticate_discord", Mock(return_value=("x" * 40, Mock(), {"id": "123"})))
    monkeypatch.setattr(wizard, "discord_installation", Mock(return_value=[]))
    destination = Mock(side_effect=[RuntimeError("Fix channel permissions"), None])
    monkeypatch.setattr(wizard, "configure_discord_destination", destination)
    nodes = {"Mitra": "mitra.example.com", "Anubis": "anubis.example.com"}
    node_prompt = Mock(return_value=nodes)
    monkeypatch.setattr(wizard, "prompt_nodes", node_prompt)
    provision = Mock(side_effect=[FileExistsError("Folder already exists"), None])
    monkeypatch.setattr("mitra_bot.init_peers.provision", provision)
    finish = Mock()
    monkeypatch.setattr(wizard, "finish_setup", finish)
    answers(monkeypatch, ["1", "n", "2", "n", "peer-bundles", "y", "new-bundles", "0"])
    wizard.guided_setup(open_browser=False)
    assert destination.call_count == 2
    node_prompt.assert_called_once()
    assert [call.args[0] for call in provision.call_args_list] == [Path("peer-bundles"), Path("new-bundles")]
    finish.assert_called_once()
    assert "x" * 40 in (tmp_path / ".env").read_text()
