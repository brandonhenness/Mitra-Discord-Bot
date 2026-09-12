import io
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from mitra_bot.services import cloudflare_auth as auth
from mitra_bot import cloudflare_setup


@pytest.mark.parametrize("code", ["invalid_scope", "access_denied", "unauthorized_client", "invalid_request"])
def test_callback_reports_provider_code_without_reflecting_secrets(code):
    with pytest.raises(auth.CloudflareOAuthError) as error:
        auth.callback_code(f"/cloudflare/callback?state=expected&error={code}&error_description=PRIVATE&code=PRIVATE", "expected")
    assert error.value.code == code
    assert code in str(error.value)
    assert "PRIVATE" not in str(error.value)


def test_unknown_error_and_invalid_state_do_not_echo_provider_content():
    with pytest.raises(auth.CloudflareOAuthError) as error:
        auth.callback_code("/cloudflare/callback?state=expected&error=SECRET", "expected")
    assert "SECRET" not in str(error.value)
    with pytest.raises(ValueError):
        auth.callback_code("/cloudflare/callback?state=wrong&error=invalid_scope", "expected")


def test_browser_and_console_preserve_scope_error(monkeypatch):
    opened, messages = [], []
    monkeypatch.setattr(auth.webbrowser, "open", opened.append)
    class Server:
        def __init__(self, address, handler):
            self.handler = handler
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def handle_request(self):
            state = parse_qs(urlsplit(opened[0]).query)["state"][0]
            handler = self.handler.__new__(self.handler)
            handler.path = "/cloudflare/callback?error=invalid_scope&error_description=SECRET&state="+state
            handler.headers = {"Host": "localhost:9876"}
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_GET()
            messages.append(handler.wfile.getvalue())
    monkeypatch.setattr(auth, "HTTPServer", Server)
    exchange = Mock()
    monkeypatch.setattr(auth, "exchange_token", exchange)
    with pytest.raises(auth.CloudflareOAuthError, match="invalid_scope"):
        auth.authorize("client")
    exchange.assert_not_called()
    assert b"invalid_scope" in messages[0]
    assert b"SECRET" not in messages[0]


def test_token_exchange_reports_invalid_grant_without_secret_body(monkeypatch):
    response = Mock(status_code=400)
    response.json.return_value = dict(error="invalid_grant", error_description="SECRET")
    monkeypatch.setattr(auth.requests, "post", Mock(return_value=response))
    with pytest.raises(auth.CloudflareOAuthError, match="invalid_grant") as error:
        auth.exchange_token(dict(client_id="client", grant_type="refresh_token", refresh_token="SECRET"))
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("choice,mode", [("1", None), ("2", "2")])
def test_retry_or_manual_fallback_does_not_repeat_discord(monkeypatch, choice, mode):
    configure = Mock(side_effect=[auth.CloudflareOAuthError("invalid_scope"), True])
    monkeypatch.setattr(cloudflare_setup, "configure_cloudflare", configure)
    monkeypatch.setattr("builtins.input", lambda _: choice)
    assert cloudflare_setup.run_cloudflare_setup(env_file="custom.env", open_browser=False)
    assert configure.call_count == 2
    assert configure.call_args.kwargs["auth_method"] == mode
    assert configure.call_args.kwargs["env_file"] == "custom.env"
    assert configure.call_args.kwargs["open_browser"] is False


def test_leave_cloudflare_for_later_returns_incomplete(monkeypatch, capsys):
    configure = Mock(side_effect=auth.CloudflareOAuthError("invalid_scope"))
    monkeypatch.setattr(cloudflare_setup, "configure_cloudflare", configure)
    monkeypatch.setattr("builtins.input", lambda _: "3")
    assert cloudflare_setup.run_cloudflare_setup() is False
    assert configure.call_count == 1
    assert "uv run mitra-cloudflare-setup" in capsys.readouterr().out


@pytest.mark.parametrize("approve", [True, False])
def test_client_repair_only_changes_grants_after_approval(monkeypatch, tmp_path, capsys, approve):
    monkeypatch.chdir(tmp_path)
    client_id = "a"*32
    account_id = "b"*32
    secret = "temporary-management-secret"
    before = dict(client_id=client_id, grant_types=["authorization_code"], scopes=["zone.read", "dns.write"])
    after = dict(before, grant_types=["authorization_code", "refresh_token"], scopes=["zone.read", "dns.write", "offline_access"])
    request = Mock(side_effect=[{"result": before}, {"result": after}, {"result": after}])
    monkeypatch.setattr(cloudflare_setup, "CloudflareService", Mock(return_value=Mock(_request=request)))
    monkeypatch.setattr(cloudflare_setup.getpass, "getpass", lambda _: secret)
    inputs = iter([account_id, "y" if approve else "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    cloudflare_setup.repair_client(client_id=client_id, open_browser=False)
    assert not list(tmp_path.iterdir())
    assert secret not in capsys.readouterr().out
    if approve:
        assert request.call_count == 3
        assert request.call_args_list[1].args == ("PATCH", f"/accounts/{account_id}/oauth_clients/{client_id}")
        assert request.call_args_list[1].kwargs == {"json_body": {"grant_types": ["authorization_code", "refresh_token"]}}
    else:
        assert request.call_count == 1


def test_client_repair_rejects_wrong_client_before_any_write(monkeypatch):
    request = Mock(return_value={"result": {"client_id": "c"*32}})
    monkeypatch.setattr(cloudflare_setup, "CloudflareService", Mock(return_value=Mock(_request=request)))
    monkeypatch.setattr(cloudflare_setup.getpass, "getpass", lambda _: "temporary-token")
    monkeypatch.setattr("builtins.input", lambda _: "b"*32)
    with pytest.raises(RuntimeError, match="unexpected client"):
        cloudflare_setup.repair_client(client_id="a"*32, open_browser=False)
    assert request.call_count == 1
