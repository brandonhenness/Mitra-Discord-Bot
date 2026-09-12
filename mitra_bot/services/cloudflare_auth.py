"""Cloudflare PKCE authorization and local, refreshable credentials."""
from __future__ import annotations

import base64
from mitra_bot import cli_ui as ui
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from dotenv import dotenv_values

from mitra_bot.storage.config_store import get_config_path

AUTH_URL = "https://dash.cloudflare.com/oauth2/auth"
TOKEN_URL = "https://dash.cloudflare.com/oauth2/token"
REDIRECT_URI = "http://localhost:9876/cloudflare/callback"
SCOPES = "zone.read dns.write offline_access"
# Registered Mitra client; publisher verification/public visibility is managed in Cloudflare.
# Never put a client secret here; desktop/CLI installs use PKCE.
DEFAULT_CLIENT_ID = "dbf6bc3487bf8f65d528b018d2374fdf"
_LOCK = threading.RLock()


class CloudflareOAuthError(RuntimeError):
    """Safe provider error reporting without reflecting callback URLs or credentials."""

    def __init__(self, code, *, phase="authorization"):
        remedies = {
            "access_denied": "Access was denied by the user or Cloudflare policy. If you clicked Authorize, check account membership, public-client visibility and account OAuth restrictions.",
            "invalid_scope": "The requested scopes were rejected. Keep DNS Edit and Zone Read, and enable BOTH Authorization Code and Refresh Token grant types on the OAuth client. Cloudflare adds offline_access automatically for Refresh Token. Retry after saving, or run mitra-cloudflare-setup --repair-client.",
            "invalid_client": "Cloudflare did not accept the client. Check the client ID and token authentication method (none for Mitra's PKCE flow).",
            "unauthorized_client": "This client is not allowed to use the requested flow. Check Authorization Code, PKCE and whether your account may authorize the client.",
            "invalid_request": "Cloudflare rejected the OAuth request. Check the exact callback URL, Authorization Code response type and PKCE configuration.",
            "invalid_grant": "The authorization code or refresh grant is expired, revoked or invalid. Start a fresh connection; do not reuse an old authorization link.",
            "unsupported_response_type": "The client must allow response type code.",
            "unsupported_grant_type": "Check Authorization Code support and refresh-token support for the client.",
            "server_error": "Cloudflare reported an internal error. Retry the connection.",
            "temporarily_unavailable": "Cloudflare is temporarily unavailable. Retry shortly.",
            "login_required": "Sign in to Cloudflare and start a fresh connection.",
            "consent_required": "Cloudflare requires consent. Start a fresh connection and approve access.",
        }
        self.code = code if isinstance(code, str) and code in remedies else "unrecognized_oauth_error"
        remedy = remedies.get(self.code, "Check the Cloudflare browser page and client configuration. Provider descriptions are omitted to protect credentials.")
        super().__init__(f"Cloudflare {phase} failed ({self.code}). {remedy}")


def credentials_path():
    return get_config_path().resolve().parent / ".env.cloudflare-oauth.json"


@contextmanager
def credential_lock():
    # Refresh tokens rotate: serialize setup and bot refreshes across processes.
    path = credentials_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def read_credentials():
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, OSError):
        raise RuntimeError("Cannot read local Cloudflare credentials; restore the secret file or reconnect.") from None


def write_credentials(data):
    path = credentials_path()
    fd, temporary = tempfile.mkstemp(prefix=".env.cloudflare-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_profile(name, value):
    with credential_lock():
        data = read_credentials()
        data[name] = value
        write_credentials(data)


def exchange_token(data, previous=None):
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=20, allow_redirects=False)
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise CloudflareOAuthError(payload["error"], phase="token exchange/refresh")
        response.raise_for_status()
        if response.status_code != 200 or not isinstance(payload, dict):
            raise ValueError()
        access = payload.get("access_token")
        lifetime = int(payload.get("expires_in", 0))
        refresh = payload.get("refresh_token") or (previous or {}).get("refresh_token")
        if not isinstance(access, str) or not access or lifetime <= 0 or not isinstance(refresh, str) or not refresh:
            raise ValueError()
        if payload.get("token_type", "Bearer").lower() != "bearer":
            raise ValueError()
        return dict(client_id=data["client_id"], access_token=access, refresh_token=refresh,
                    expires_at=time.time()+lifetime)
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        raise RuntimeError("Cloudflare authorization/refresh failed. Reconnect the account; no credentials were logged.") from None


def target_token(target, default_token=None):
    if not target.oauth_profile:
        value = os.getenv(target.token_env)
        if not value and target.token_env == "CLOUDFLARE_API_TOKEN":
            value = default_token
        if not value:
            value = dotenv_values(os.getenv("MITRA_ENV_FILE", ".env")).get(target.token_env)
        if not value or not value.strip():
            raise RuntimeError(f"Missing Cloudflare credential: {target.token_env}")
        return value.strip()
    with credential_lock():
        credentials = read_credentials()
        saved = credentials.get(target.oauth_profile)
        if not isinstance(saved, dict):
            raise RuntimeError(f"Reconnect Cloudflare profile {target.oauth_profile}")
        if float(saved.get("expires_at", 0)) <= time.time()+120:
            saved = exchange_token(dict(grant_type="refresh_token", client_id=saved["client_id"],
                                        refresh_token=saved["refresh_token"]), previous=saved)
            credentials[target.oauth_profile] = saved
            write_credentials(credentials)  # Persist rotated token before returning it.
        return saved["access_token"]


def authorization_url(client_id, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return AUTH_URL + "?" + urlencode(dict(client_id=client_id, redirect_uri=REDIRECT_URI,
        response_type="code", scope=SCOPES, state=state, code_challenge=challenge,
        code_challenge_method="S256"))


def callback_code(url, expected_state):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    if parsed.path != "/cloudflare/callback" or query.get("state") != [expected_state]:
        raise ValueError("Invalid OAuth callback state or path")
    if query.get("error"):
        if len(query["error"]) != 1:
            raise ValueError("Ambiguous OAuth error")
        raise CloudflareOAuthError(query["error"][0])
    code = query.get("code", [])
    if len(code) != 1 or not code[0]:
        raise ValueError("Missing OAuth authorization code")
    return code[0]


def authorize(client_id, *, open_browser=True, timeout=300):
    if not client_id or not client_id.strip():
        raise ValueError("Register a Cloudflare public/PKCE OAuth client and provide its client ID first.")
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            if self.headers.get("Host") not in {"localhost:9876", "127.0.0.1:9876"}:
                self.send_error(400)
                return
            try:
                result["code"] = callback_code(self.path, state)
            except ValueError:
                self.send_error(400, "Invalid callback")
                return
            except CloudflareOAuthError as exc:
                result["error"] = exc
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            message = str(result["error"]) if "error" in result else "Cloudflare returned an authorization code. Return to Mitra to finish setup."
            self.wfile.write((message+"\nReturn to the setup console for next steps.").encode("utf-8"))

    class LocalServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

        def handle_error(self, request, client_address):
            pass  # Never print an authorization request traceback.

    with LocalServer(("127.0.0.1", 9876), Callback) as server:
        server.timeout = 1
        url = authorization_url(client_id.strip(), state, verifier)
        ui.message("Authorize Mitra in Cloudflare, select the permitted resources, then return here.")
        ui.message(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic()+timeout
        with ui.status("Waiting for browser authorization · approve in Cloudflare to continue"):
            while not result and time.monotonic() < deadline:
                server.handle_request()
    if result.get("error"):
        raise result["error"]
    if not result.get("code"):
        raise RuntimeError("Cloudflare authorization timed out. Retry, or use manual API-token setup.")
    return ui.run("Completing Cloudflare connection", exchange_token, dict(grant_type="authorization_code", client_id=client_id.strip(),
        code=result["code"], redirect_uri=REDIRECT_URI, code_verifier=verifier))
