import asyncio
import base64
import hashlib
import json
import io
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest

from mitra_bot.cloudflare_config import CloudflareTarget, select_targets
from mitra_bot import cloudflare_setup
from mitra_bot.services import cloudflare_auth as auth
from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.tasks import ip_monitor_task as monitor_module
from mitra_bot.storage.config_store import read_config_dict, write_config_dict


def target(name="home", node="primary", zone="zone1", records=None, **kwargs):
    return dict(name=name, node_id=node, zone_id=zone, record_ids=records or [name], **kwargs)


@pytest.fixture
def local_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MITRA_CONFIG_PATH", str(tmp_path/"config.toml"))
    monkeypatch.setenv("MITRA_STATE_PATH", str(tmp_path/"state.db"))
    monkeypatch.delenv("MITRA_PEER_CONFIG_PATH", raising=False)
    return tmp_path


def test_target_selection_supports_three_servers_and_rejects_double_ownership():
    targets = [target(node="primary", records=["root", "pq"]), target("pryor", "secondary"), target("third", "third", "other")]
    assert select_targets(targets, "primary")[0].record_ids == ["root", "pq"]
    assert select_targets(targets, "secondary")[0].record_ids == ["pryor"]
    assert select_targets(targets, "fourth") == []
    with pytest.raises(ValueError, match="one server"):
        select_targets(targets+[target("bad", "secondary", records=["root"])], "primary")


def test_local_config_survives_unrelated_state_write(local_config):
    from mitra_bot.storage.storage_store import save_ip
    config = read_config_dict()
    config["cloudflare"] = dict(enabled=True, targets=[target(token_env="CLOUDFLARE_HOME_API_TOKEN")])
    write_config_dict(config)
    asyncio.run(save_ip("192.0.2.1"))
    assert read_config_dict()["cloudflare"]["targets"][0]["token_env"] == "CLOUDFLARE_HOME_API_TOKEN"
    assert "api_token" not in read_config_dict()["cloudflare"]["targets"][0]


def test_multiple_accounts_update_only_this_servers_records(monkeypatch):
    targets = [target(records=["root", "pq"], token_env="HOME_TOKEN"),
               target("other", zone="zone2", token_env="WORK_TOKEN"), target("pryor", "secondary")]
    monkeypatch.setattr(monitor_module, "get_cloudflare_config", lambda: dict(enabled=True, targets=targets))
    monkeypatch.setenv("HOME_TOKEN", "home-secret")
    monkeypatch.setenv("WORK_TOKEN", "work-secret")
    services = {}
    for token, ids in [("home-secret", ["root", "pq"]), ("work-secret", ["other"])]:
        records = [dict(id=id, type="A", name=id+".example.com", content="192.0.2.1", ttl=300, proxied=True) for id in ids]
        service = Mock()
        service.get_dns_records.side_effect = [records, [dict(r, content="192.0.2.2") for r in records]]
        services[token] = service
    factory = Mock(side_effect=lambda api_token: services[api_token])
    monkeypatch.setattr(monitor_module, "CloudflareService", factory)
    monitor = monitor_module.IPMonitorTask(SimpleNamespace(peer_service=SimpleNamespace(config=SimpleNamespace(node_id="primary"))))
    assert asyncio.run(monitor._update_cloudflare_dns("192.0.2.2")) == 3
    assert factory.call_count == 2
    assert services["home-secret"].update_dns_record.call_count == 2
    assert services["work-secret"].update_dns_record.call_args.kwargs["proxied"] is True


def test_bad_account_does_not_block_healthy_account(monkeypatch):
    targets = [target(token_env="BAD_TOKEN"), target("healthy", zone="other", token_env="GOOD_TOKEN")]
    monkeypatch.setattr(monitor_module, "get_cloudflare_config", lambda: dict(enabled=True, targets=targets))
    monkeypatch.setattr(monitor_module, "target_token", lambda target, _: "secret")
    monitor = monitor_module.IPMonitorTask(SimpleNamespace(peer_service=SimpleNamespace(config=SimpleNamespace(node_id="primary"))))
    visited = []
    async def update(ip, cfg):
        visited.append(cfg.zone_id)
        if cfg.zone_id == "zone1":
            raise RuntimeError("revoked")
        return 1
    monkeypatch.setattr(monitor, "_update_cloudflare_zone", update)
    with pytest.raises(RuntimeError, match="home"):
        asyncio.run(monitor._update_cloudflare_dns("192.0.2.2"))
    assert visited == ["zone1", "other"]


def test_missing_local_assignments_never_uses_remote_credentials(monkeypatch):
    monkeypatch.setattr(monitor_module, "get_cloudflare_config", lambda: dict(enabled=True, targets=[target()]))
    secret = Mock()
    monkeypatch.setattr(monitor_module, "target_token", secret)
    monitor = monitor_module.IPMonitorTask(SimpleNamespace(peer_service=None))
    assert asyncio.run(monitor._update_cloudflare_dns("192.0.2.2")) == 0
    secret.assert_not_called()


def test_oauth_pkce_and_state_validation():
    verifier = "x"*64
    query = parse_qs(urlsplit(auth.authorization_url("client", "state", verifier)).query)
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query and "code_verifier" not in query
    assert "offline_access" in query["scope"][0]
    assert auth.callback_code("/cloudflare/callback?code=valid&state=state", "state") == "valid"
    for value in ("/cloudflare/callback?code=valid&state=wrong", "/wrong?code=valid&state=state", "/cloudflare/callback?code=a&code=b&state=state"):
        with pytest.raises(ValueError):
            auth.callback_code(value, "state")
    with pytest.raises(RuntimeError, match="access_denied"):
        auth.callback_code("/cloudflare/callback?error=access_denied&state=state", "state")


def test_refresh_rotation_is_persisted_once_across_threads(local_config, monkeypatch):
    auth.save_profile("home", dict(client_id="client", access_token="old", refresh_token="refresh-old", expires_at=0))
    response = Mock(status_code=200)
    response.json.return_value = dict(access_token="new", refresh_token="refresh-new", expires_in=3600, token_type="Bearer")
    post = Mock(return_value=response)
    monkeypatch.setattr(auth.requests, "post", post)
    cfg = CloudflareTarget(**target(oauth_profile="home"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.target_token(cfg), range(4)))
    assert results == ["new"]*4
    assert post.call_count == 1
    assert auth.read_credentials()["home"]["refresh_token"] == "refresh-new"
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert post.call_args.kwargs["allow_redirects"] is False


def test_failed_refresh_preserves_old_credentials_without_logging_tokens(local_config, monkeypatch, caplog):
    auth.save_profile("home", dict(client_id="client", access_token="SECRET", refresh_token="REFRESH", expires_at=0))
    original = auth.credentials_path().read_bytes()
    response = Mock(status_code=400)
    response.json.return_value = {"error": "invalid_grant", "sensitive": "SECRET"}
    monkeypatch.setattr(auth.requests, "post", Mock(return_value=response))
    with pytest.raises(RuntimeError) as error:
        auth.target_token(CloudflareTarget(**target(oauth_profile="home")))
    assert auth.credentials_path().read_bytes() == original
    assert "SECRET" not in str(error.value)+caplog.text
    assert "REFRESH" not in str(error.value)+caplog.text


def test_unexpired_oauth_and_dotenv_profiles(local_config, monkeypatch):
    auth.save_profile("home", dict(access_token="oauth-secret", expires_at=time.time()+3600))
    assert auth.target_token(CloudflareTarget(**target(oauth_profile="home"))) == "oauth-secret"
    (local_config/".env").write_text("CLOUDFLARE_OTHER_API_TOKEN=dotenv-secret\n")
    cfg = CloudflareTarget(**target(token_env="CLOUDFLARE_OTHER_API_TOKEN"))
    monkeypatch.delenv("CLOUDFLARE_OTHER_API_TOKEN", raising=False)
    assert auth.target_token(cfg) == "dotenv-secret"
    monkeypatch.setenv("CLOUDFLARE_OTHER_API_TOKEN", "process-secret")
    assert auth.target_token(cfg) == "process-secret"


def test_domain_selection_and_conflict_detection():
    zone = dict(id="zone", name="henness.info")
    plan = cloudflare_setup.plan_records(zone, ["@", "pq", "pryor.henness.info", "pq"], [])
    assert [name for name, _ in plan] == ["henness.info", "pq.henness.info", "pryor.henness.info"]
    with pytest.raises(ValueError, match="CNAME"):
        cloudflare_setup.plan_records(zone, ["pq"], [dict(name="pq.henness.info", type="CNAME")])
    with pytest.raises(ValueError, match="outside"):
        cloudflare_setup.record_name("another.example.", "henness.info")
    with pytest.raises(ValueError):
        cloudflare_setup.record_name("invalid name", "henness.info")


@pytest.mark.parametrize("approve", [True, False])
def test_manual_setup_review_create_and_save(local_config, monkeypatch, approve):
    service = Mock()
    service.get_zones.return_value = [dict(id="zone1", name="henness.info")]
    service.get_dns_records.return_value = [dict(id="root", type="A", name="henness.info")]
    service.create_dns_record.return_value = dict(id="pq", type="A", name="pq.henness.info")
    monkeypatch.setattr(cloudflare_setup, "CloudflareService", Mock(return_value=service))
    monkeypatch.setattr(cloudflare_setup, "get_public_ip", lambda: "192.0.2.1")
    monkeypatch.setattr(cloudflare_setup.getpass, "getpass", lambda _: "synthetic-token-"+"x"*25)
    browser = Mock()
    monkeypatch.setattr(cloudflare_setup.webbrowser, "open", browser)
    replies = iter(["home", "2", "1", "@,pq", "n", "y" if approve else "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    cloudflare_setup.configure_cloudflare(open_browser=False)
    browser.assert_not_called()
    service.update_dns_record.assert_not_called()
    if approve:
        service.create_dns_record.assert_called_once_with("zone1", name="pq.henness.info", content="192.0.2.1", proxied=False)
        cfg = read_config_dict()["cloudflare"]["targets"][0]
        assert cfg["record_ids"] == ["root", "pq"]
        assert cfg["token_env"] == "CLOUDFLARE_HOME_API_TOKEN"
        assert "synthetic-token" not in (local_config/"config.toml").read_text()
    else:
        service.create_dns_record.assert_not_called()
        assert not (local_config/"config.toml").exists()
        assert not (local_config/".env").exists()


def test_paginated_domains_and_error_redaction(monkeypatch, caplog):
    service = CloudflareService("SECRET")
    api = Mock(side_effect=[dict(result=[dict(id="z1", name="first.com")], result_info=dict(total_pages=2)),
                           dict(result=[dict(id="z2", name="second.com")], result_info=dict(total_pages=2))])
    monkeypatch.setattr(service, "_request", api)
    assert [z["id"] for z in service.get_zones()] == ["z1", "z2"]
    response = Mock()
    response.json.return_value = dict(success=False, errors=[dict(code=10000, message="SECRET")])
    monkeypatch.setattr("mitra_bot.services.cloudflare_service.requests.request", Mock(return_value=response))
    with pytest.raises(RuntimeError) as error:
        CloudflareService("SECRET").get_zones()
    assert "SECRET" not in str(error.value)+caplog.text


def test_browser_callback_exchanges_code_with_its_pkce_verifier(monkeypatch):
    opened = []
    monkeypatch.setattr(auth.webbrowser, "open", opened.append)
    class CallbackServer:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 9876)
            self.handler = handler
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def handle_request(self):
            state = parse_qs(urlsplit(opened[0]).query)["state"][0]
            handler = self.handler.__new__(self.handler)
            handler.path = "/cloudflare/callback?code=test-code&state="+state
            handler.headers = {"Host": "localhost:9876"}
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_GET()
            assert b"test-code" not in handler.wfile.getvalue()
    monkeypatch.setattr(auth, "HTTPServer", CallbackServer)
    response = Mock(status_code=200)
    response.json.return_value = dict(access_token="automatic-token", refresh_token="refresh", expires_in=3600)
    post = Mock(return_value=response)
    monkeypatch.setattr(auth.requests, "post", post)
    result = auth.authorize("client-id")
    assert result["access_token"] == "automatic-token"
    body = post.call_args.kwargs["data"]
    assert body["code"] == "test-code"
    assert body["redirect_uri"] == auth.REDIRECT_URI
    query = parse_qs(urlsplit(opened[0]).query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]


def test_oauth_wizard_saves_automatic_credentials_and_retains_other_zones(local_config, monkeypatch):
    config = read_config_dict()
    config["cloudflare"] = dict(enabled=True, zone_id="old-zone", record_ids=["old-record"])
    write_config_dict(config)
    service = Mock()
    service.get_zones.return_value = [dict(id="zone1", name="henness.info")]
    service.get_dns_records.return_value = [dict(id="pq", name="pq.henness.info", type="A")]
    monkeypatch.setattr(cloudflare_setup, "CloudflareService", Mock(return_value=service))
    monkeypatch.setattr(cloudflare_setup, "authorize", Mock(return_value=dict(access_token="OAUTH", refresh_token="REFRESH", expires_at=time.time()+3600, client_id="client")))
    replies = iter(["home", "1", "1", "pq", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    cloudflare_setup.configure_cloudflare(client_id="client", open_browser=False)
    targets = read_config_dict()["cloudflare"]["targets"]
    assert {t["zone_id"] for t in targets} == {"old-zone", "zone1"}
    assert next(t for t in targets if t["zone_id"] == "zone1")["oauth_profile"] == "home"
    assert auth.read_credentials()["home"]["access_token"] == "OAUTH"
    assert "OAUTH" not in (local_config/"config.toml").read_text()
    assert not (local_config/".env").exists()
    service.create_dns_record.assert_not_called()
    service.update_dns_record.assert_not_called()


def test_deployment_verifies_only_local_targets(monkeypatch):
    from mitra_bot.services import cloudflare_verify
    monkeypatch.setattr(cloudflare_verify, "load_peer_config", lambda: SimpleNamespace(enabled=True, node_id="primary"))
    service = Mock()
    service.get_dns_records.return_value = [dict(id="home", type="A", name="henness.info", content="192.0.2.1")]
    monkeypatch.setattr(cloudflare_verify, "CloudflareService", Mock(return_value=service))
    selected_secret = Mock(return_value="token")
    count = cloudflare_verify.verify_targets([target(), target("remote", "secondary")], selected_secret)
    assert count == 1
    selected_secret.assert_called_once_with("CLOUDFLARE_API_TOKEN")
    service.update_dns_record.assert_not_called()


def test_oauth_secret_files_are_not_release_assets():
    from mitra_bot.release_tools import allowed_file
    for path in (".env.cloudflare-oauth.json", "docs/.env.cloudflare-oauth.json", ".env.cloudflare-oauth.lock"):
        assert not allowed_file(path)
