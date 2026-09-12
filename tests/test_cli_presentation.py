from io import StringIO
from unittest.mock import Mock

from rich.console import Console

from mitra_bot import cli_ui as ui


def test_interactive_questions_and_hidden_input_are_distinct(monkeypatch):
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=80)
    monkeypatch.setattr(ui, "console", lambda: console)
    monkeypatch.setattr(ui, "interactive", lambda display: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    hidden = Mock(return_value="never-print-this")
    monkeypatch.setattr(ui.getpass, "getpass", hidden)
    ui.step("Discord connection", 1, 5, purpose="Connect your bot identity.")
    assert ui.ask("Keep the existing token? [Y/n]: ") == "yes"
    assert ui.secret("Bot token") == "never-print-this"
    text = output.getvalue()
    assert "Your choice" in text and "Answer >" in text
    assert "Connect your bot identity." in text
    assert "Input is hidden" in text and "never-print-this" not in text
    hidden.assert_called_once()


def test_plain_mode_keeps_direct_input_and_explanations(monkeypatch, capsys):
    monkeypatch.setattr(ui, "interactive", lambda display: False)
    ask = Mock(return_value="n")
    monkeypatch.setattr("builtins.input", ask)
    ui.step("UPS", 3, 5, purpose="Monitor a USB UPS on this machine.")
    assert ui.ask("Enable? ") == "n"
    ask.assert_called_once_with("Enable? ")
    assert "Purpose: Monitor a USB UPS" in capsys.readouterr().out
