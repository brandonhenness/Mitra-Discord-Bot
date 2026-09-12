from unittest.mock import Mock

import pytest

from mitra_bot import setup_wizard as wizard


def test_empty_paste_retries_and_confirms_input_without_revealing_it(monkeypatch, capsys):
    secret = "synthetic-bot-token-" + "x" * 30
    read = Mock(side_effect=["", "  ", secret])
    monkeypatch.setattr(wizard.getpass, "getpass", read)
    assert wizard.read_bot_token(Mock()) == secret
    output = capsys.readouterr().out
    assert output.count("No token was received") == 2
    assert f"Received {len(secret)} characters" in output
    assert "OAuth2 Client Secret" in output
    assert secret not in output


def test_401_retries_without_saving_rejected_token(monkeypatch, capsys):
    tokens = ["rejected-"+"x"*30, "accepted-"+"y"*30]
    monkeypatch.setattr(wizard.getpass, "getpass", Mock(side_effect=tokens))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    application = Mock(side_effect=[wizard.DiscordAuthenticationError("HTTP 401"), {"id": "123"}])
    monkeypatch.setattr(wizard.DiscordSetup, "application", application)
    save = Mock()
    monkeypatch.setattr(wizard, "save_token", save)
    token, api, result = wizard.authenticate_discord(None, Mock())
    assert token == tokens[1] and api.token == tokens[1] and result["id"] == "123"
    assert application.call_count == 2
    save.assert_not_called()
    output = capsys.readouterr().out
    assert all(secret not in output for secret in tokens)


def test_cancel_after_rejection_preserves_existing_secret(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_APPLICATION_TOKEN", raising=False)
    original = "DISCORD_APPLICATION_TOKEN=" + "x"*40 + "\n"
    (tmp_path/".env").write_text(original)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda _: "wrong-secret-"+"y"*30)
    monkeypatch.setattr(wizard.DiscordSetup, "application", Mock(side_effect=wizard.DiscordAuthenticationError("HTTP 401")))
    with pytest.raises(RuntimeError, match="retained"):
        wizard.guided_setup(open_browser=False)
    assert (tmp_path/".env").read_text() == original


@pytest.mark.parametrize("status,expected", [(401, "OAuth2 Client Secret"), (403, "Manage Roles")])
def test_auth_and_permission_errors_have_different_remedies(monkeypatch, status, expected):
    monkeypatch.setattr(wizard.requests, "request", Mock(return_value=Mock(status_code=status)))
    with pytest.raises(RuntimeError) as error:
        wizard.DiscordSetup("private-value").application()
    assert expected in str(error.value)
    assert "private-value" not in str(error.value)
    assert isinstance(error.value, wizard.DiscordAuthenticationError) == (status == 401)
