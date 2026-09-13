"""Connect Cloudflare accounts and assign DNS records to this server."""
from __future__ import annotations

import argparse
from mitra_bot import cli_ui as ui
import getpass
import hashlib
import ipaddress
import os
import re
import webbrowser

from mitra_bot.cloudflare_config import CloudflareTarget, select_targets
from mitra_bot.peer_config import load_peer_config
from mitra_bot.services.cloudflare_auth import DEFAULT_CLIENT_ID, authorize, save_profile
from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.setup_wizard import choose, menu, save_token, yes
from mitra_bot.storage.config_store import read_config_dict, write_config_dict

TOKEN_PAGE = "https://dash.cloudflare.com/profile/api-tokens"


def record_name(value, zone):
    zone = zone.rstrip(".").lower().encode("idna").decode("ascii")
    value = value.strip().lower()
    if value == "@":
        return zone
    absolute = value.endswith(".")
    value = value.rstrip(".").encode("idna").decode("ascii")
    if absolute and value != zone and not value.endswith("."+zone):
        raise ValueError("Record name is outside the selected domain")
    name = value if value == zone or value.endswith("."+zone) else value+"."+zone
    if len(name) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
        raise ValueError("Use @ or a valid subdomain name")
    return name


def plan_records(zone, requested, existing):
    names = list(dict.fromkeys(record_name(value, zone["name"]) for value in requested))
    if not names:
        raise ValueError("Select at least one DNS name")
    planned = []
    for name in names:
        matches = [r for r in existing if r["name"].rstrip(".").lower() == name and r["type"] in {"A", "CNAME"}]
        if any(r["type"] == "CNAME" for r in matches) or len(matches) > 1:
            raise ValueError(f"{name} has a CNAME or multiple A records. Resolve it manually before assigning it to Mitra.")
        planned.append((name, matches[0] if matches else None))
    return planned


def configure_cloudflare(*, env_file=".env", open_browser=True, client_id=None, auth_method=None):
    ui.banner("MITRA  /  CLOUDFLARE", "Connect an account. Choose domains. Review your DNS assignments.")
    ui.step("Connect your account", 1, 3, purpose="Authorize the Cloudflare account that owns the DNS records this machine should update.")
    cfg = read_config_dict()
    peer = load_peer_config()
    node_id = peer.node_id if peer.enabled else "local"
    ui.message(f"Cloudflare setup for server {node_id}. Each assigned A record will track this machine's public IPv4.")
    ui.message("Run setup separately on each server/account. Stop this bot while changing credentials or assignments.")
    ui.message("Give this Cloudflare account connection a short label so you can recognize it later, such as home or pryor.")
    while True:
        profile = ui.ask("Connection label (for example home or pryor): ").strip()
        if re.fullmatch(r"[a-z][a-z0-9_]{0,23}", profile):
            break
        ui.message("Start with a lowercase letter. Use only lowercase letters, numbers or underscores, up to 24 characters. Please try again.")
    mode = auth_method or menu("1. Sign in to Cloudflare in your browser  2. Paste an API token manually", ("1", "2"), "1")
    oauth = None
    variable = "CLOUDFLARE_"+profile.upper()+"_API_TOKEN"
    if mode == "1":
        client_id = client_id or os.getenv("MITRA_CLOUDFLARE_CLIENT_ID") or DEFAULT_CLIENT_ID
        if not client_id:
            ui.message("Browser connection requires a registered Mitra OAuth client. See docs/cloudflare.md for the one-time registration.")
            client_id = ui.ask("OAuth client ID (blank switches to manual API token): ").strip()
        if client_id:
            oauth = authorize(client_id, open_browser=open_browser)
            token = oauth["access_token"]
        else:
            mode = "2"
    if mode == "2":
        ui.message("Create a token with Zone Read and DNS Edit, restricted to the domains this machine should manage.")
        ui.message(TOKEN_PAGE)
        if open_browser:
            webbrowser.open(TOKEN_PAGE)
        while True:
            token = ui.secret("Cloudflare API token (hidden): ").strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{20,256}", token):
                break
            ui.message("That does not look like an API token. Copy only the token value and paste it again; input is hidden.")
    elif mode != "1":
        raise ValueError("Unknown authentication option")
    service = CloudflareService(api_token=token)
    zones = ui.run("Finding your Cloudflare domains", service.get_zones)
    if not zones:
        raise RuntimeError("No accessible domains found. Grant Zone Read and DNS Edit for the desired zones, then retry.")
    ui.step("Choose domains & records", 2, 3, purpose="Select the domain and subdomains that should follow this machine's public IP.")
    plans = []
    while zones:
        zone = choose("Domain", zones, lambda z: z["name"]+" ("+str(z.get("account", {}).get("name", "account"))+")")
        if zone is None:
            break
        existing = ui.run("Loading DNS records", service.get_dns_records, zone["id"])
        ui.message("Existing A records: "+", ".join(r["name"] for r in existing if r["type"] == "A"))
        ui.message(f"Use @ for {zone['name']}, or names such as mitra, pq. Separate names with commas. Full names also work.")
        while True:
            values = ui.ask("Names to update (blank skips this domain): ").strip()
            if not values:
                planned = None
                break
            try:
                planned = plan_records(zone, [v.strip() for v in values.split(",") if v.strip()], existing)
                assigned_elsewhere = {record for target in cfg["cloudflare"].get("targets") or []
                                      if target["zone_id"] == zone["id"] and target.get("node_id", "local") not in {node_id, "local"}
                                      for record in target["record_ids"]}
                if any(record and record["id"] in assigned_elsewhere for _, record in planned):
                    raise ValueError("One of these names is assigned to another machine. Choose different names, or fix the saved assignment before retrying.")
                break
            except ValueError as exc:
                ui.message(str(exc))
                ui.message("Enter corrected names below. Your account connection and earlier domain selections are retained.")
        if planned is None:
            zones = [z for z in zones if z["id"] != zone["id"]]
            continue
        plans.append((zone, planned))
        zones = [z for z in zones if z["id"] != zone["id"]]
        if not zones or not yes("Add another domain from this account/connection?", False):
            break
    if not plans:
        ui.message("No assignments saved.")
        return
    creates = any(record is None for _, records in plans for _, record in records)
    public_ip = ui.run("Detecting this server's public IP", get_public_ip) if creates else None
    if creates and (not public_ip or ipaddress.ip_address(public_ip).version != 4):
        raise RuntimeError("Could not determine this server's IPv4 address; no DNS records were created.")
    proxied = yes("Enable Cloudflare's orange-cloud proxy for NEW records?", False) if creates else False
    ui.step("Review & apply", 3, 3, purpose="Check the proposed DNS assignments before saving this machine's configuration.")
    ui.message(f"Assignments for {node_id} (existing record TTL/proxy settings are preserved):")
    for zone, planned in plans:
        for name, record in planned:
            ui.message("  "+name+(" — manage existing A record on bot startup" if record else f" — create A record now → {public_ip}, proxied={proxied}"))
    ui.message("This replaces this server's assignments for the selected zones. Other zones/servers are retained.")
    ui.message("Reusing a connection name updates its credentials for every assignment referencing it.")
    if not yes("Apply these DNS creations and save these assignments?", False):
        ui.message("Cancelled; no DNS changes or configuration writes.")
        return
    current = cfg["cloudflare"]
    targets = list(current.get("targets") or [])
    if current.get("targets") is None and current.get("zone_id") and current.get("record_ids"):
        targets.append(dict(name="legacy", node_id=node_id, zone_id=current["zone_id"], record_ids=current["record_ids"]))
    chosen_zones = {zone["id"] for zone, _ in plans}
    targets = [t for t in targets if not (t.get("node_id", "local") in {node_id, "local"} and t["zone_id"] in chosen_zones)]
    # Refuse overlapping assignments before saving secrets or creating DNS records.
    remote = {(t["zone_id"], record) for t in targets for record in t["record_ids"]}
    if any((zone["id"], record["id"]) in remote for zone, rows in plans for _, record in rows if record):
        raise ValueError("A selected DNS record is already assigned to another target/server")
    def make_target(zone, ids):
        identity = hashlib.sha256((node_id+":"+zone["id"]).encode()).hexdigest()[:12]
        return CloudflareTarget(name=profile+"-"+identity, node_id=node_id, zone_id=zone["id"],
            record_ids=ids, token_env=variable, oauth_profile=profile if oauth else None).model_dump(exclude_none=True)
    proposed = [make_target(zone, [record["id"] if record else "new-"+name for name, record in rows]) for zone, rows in plans]
    select_targets(targets+proposed, node_id)
    if oauth:
        save_profile(profile, oauth)
    else:
        save_token(env_file, token, variable=variable)
    for zone, planned in plans:
        ids = []
        for name, record in planned:
            if record is None:
                record = ui.run("Creating DNS record", service.create_dns_record, zone["id"], name=name, content=public_ip, proxied=proxied)
                ui.message(f"Created {name} ({record['id']}).")
            ids.append(record["id"])
        targets.append(make_target(zone, ids))
    select_targets(targets, node_id)
    cfg["cloudflare"] = dict(enabled=True, targets=targets)
    write_config_dict(cfg)
    ui.message("Cloudflare setup saved. Restart the bot to reconcile DNS for this server.")
    return True


def run_cloudflare_setup(*, env_file=".env", open_browser=True, client_id=None):
    # True: saved; None: skipped/cancelled; False: failed and deferred.
    mode = None
    while True:
        try:
            return configure_cloudflare(env_file=env_file, open_browser=open_browser, client_id=client_id, auth_method=mode)
        except (RuntimeError, ValueError, OSError) as exc:
            ui.message(f"Cloudflare setup could not finish: {exc}")
            ui.message("Completed Discord setup is retained. Any DNS records already reported as created remain; retrying discovers them.")
            ui.message("Resume later with: uv run mitra-cloudflare-setup")
            while True:
                choice = ui.ask("Next step: 1. Retry Cloudflare  2. Use a manual API token  3. Leave Cloudflare for later [3]: ").strip() or "3"
                if choice in {"1", "2", "3"}:
                    break
                ui.message("Choose 1, 2 or 3.")
            if choice == "3":
                ui.message("Cloudflare setup is unfinished. Existing Cloudflare configuration has been retained.")
                return False
            mode = "2" if choice == "2" else None


def repair_client(*, client_id=None, open_browser=True):
    client_id = client_id or os.getenv("MITRA_CLOUDFLARE_CLIENT_ID") or DEFAULT_CLIENT_ID
    if not re.fullmatch(r"[a-fA-F0-9]{32}", client_id or ""):
        raise ValueError("Provide the OAuth client's 32-character client ID")
    ui.message("One-time maintainer repair: enable the Refresh Token grant on the registered OAuth client.")
    ui.message("You can instead edit the client in Cloudflare and enable Authorization Code AND Refresh Token.")
    account_id = ui.ask("Cloudflare account ID that owns this OAuth client (32 characters): ").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
        raise ValueError("Use the account ID from the Cloudflare dashboard, not a zone ID")
    ui.message("Create a temporary API token with Account > OAuth Client Write/Edit for this account.")
    ui.message("This management token is used in memory only and is not saved. Revoke it after repair.")
    ui.message(TOKEN_PAGE)
    if open_browser:
        webbrowser.open(TOKEN_PAGE)
    token = ui.secret("Temporary OAuth client management API token (hidden): ").strip()
    service = CloudflareService(api_token=token)
    endpoint = f"/accounts/{account_id}/oauth_clients/{client_id}"
    client = service._request("GET", endpoint).get("result")
    if not isinstance(client, dict) or client.get("client_id") != client_id:
        raise RuntimeError("Cloudflare returned an unexpected client; no changes made")
    grants = client.get("grant_types")
    if not isinstance(grants, list) or "authorization_code" not in grants or any(g not in {"authorization_code", "refresh_token"} for g in grants):
        raise RuntimeError("Unexpected OAuth grant configuration; review this client in Cloudflare")
    if "refresh_token" in grants:
        ui.message("Refresh Token is already enabled. Start a fresh browser authorization session.")
        return
    ui.message(f"Client {client_id}: grant_types will change from [authorization_code] to [authorization_code, refresh_token].")
    ui.message("Cloudflare will add offline_access automatically. This permits ongoing access through refresh tokens.")
    if not yes("Apply this client grant change?", False):
        ui.message("Cancelled; no client settings changed.")
        return
    service._request("PATCH", endpoint, json_body={"grant_types": [*grants, "refresh_token"]})
    updated = service._request("GET", endpoint).get("result")
    if not isinstance(updated, dict) or updated.get("client_id") != client_id or "refresh_token" not in updated.get("grant_types", []) or "offline_access" not in updated.get("scopes", []):
        raise RuntimeError("Client change was submitted but refresh access could not be verified. Check the client in Cloudflare.")
    ui.message("Refresh Token and offline_access verified. Revoke the temporary management token, then run uv run mitra-cloudflare-setup.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--plain", action="store_true", help="Disable colors and animations")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--client-id", help="Registered Cloudflare PKCE OAuth client ID (not a secret)")
    parser.add_argument("--repair-client", action="store_true", help="Maintainer-only guided repair to enable the client's Refresh Token grant")
    args = parser.parse_args()
    ui.configure(plain=args.plain)
    try:
        if args.repair_client:
            repair_client(client_id=args.client_id, open_browser=not args.no_browser)
            return
        if run_cloudflare_setup(env_file=args.env_file, open_browser=not args.no_browser, client_id=args.client_id) is False:
            parser.exit(1, "Cloudflare setup left unfinished; rerun this command when ready.\n")
    except (RuntimeError, ValueError, OSError, KeyboardInterrupt) as exc:
        parser.exit(1, f"Cloudflare setup stopped: {exc}\nAny DNS creations already reported remain in Cloudflare; rerun to reuse them.\n")


if __name__ == "__main__":
    main()
