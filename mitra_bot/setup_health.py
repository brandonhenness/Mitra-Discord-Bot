"""Setup checks that never start the bot or write DNS/Discord state."""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sqlite3
import ssl
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values
from mitra_bot import cli_ui as ui


def peer_connection(config, peer):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # Verify the pinned certificate below.
    context.load_verify_locations(cafile=config.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.cert_file, config.key_file)
    with socket.create_connection((peer.host, peer.port), timeout=3) as connection:
        with context.wrap_socket(connection, server_hostname=peer.host) as secure:
            actual = hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()
            if actual != peer.fingerprint:
                raise ValueError("Peer certificate does not match configuration")


def database_check(path):
    if not path.is_file():
        raise ValueError("Database not created yet")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Database integrity check failed")
        db.execute("SELECT ts FROM ups_samples LIMIT 1").fetchall()


def health_results(env_file=".env"):
    from mitra_bot.storage.config_store import read_config_dict
    from mitra_bot.peer_config import load_peer_config
    from mitra_bot.setup_wizard import DiscordSetup
    from mitra_bot.services.cloudflare_verify import verify_targets

    rows = []
    def check(name, action, success, remedy):
        ui.message("Checking " + name + "...")
        try:
            action()
            rows.append((name, "OK - " + success))
        except Exception:
            # Never reflect provider exceptions containing credentials or URLs.
            rows.append((name, "CHECK - " + remedy))

    try:
        cfg = read_config_dict()
        secrets = {**dotenv_values(env_file), **os.environ}
    except Exception:
        return [("Configuration", "CHECK - repair config.toml and the selected env file, then rerun setup")]
    token = secrets.get("DISCORD_APPLICATION_TOKEN")
    if token:
        api = DiscordSetup(token)
        check("Discord token", api.application, "authenticated",
              "rerun Discord setup; check token and internet access")
        channel = cfg["bot"].get("channel_id")
        if channel:
            check("Discord channel", lambda: api.request("GET", f"/channels/{channel}"),
                  "channel visible; sending and role permissions still require a live bot test",
                  "check the channel ID and bot's View Channel permission")
        else:
            rows.append(("Discord channel", "SKIPPED - select a notification channel in setup"))
    else:
        rows.append(("Discord token", "CHECK - rerun setup to save a bot token"))
    ups = cfg["ups"]
    if ups.get("enabled"):
        path = Path(ups.get("database_file") or ups.get("log_file", "ups_stats.db")).expanduser()
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            path = path.with_suffix(".db")
        check("UPS database", lambda: database_check(path), "readable; integrity check passed",
              "rerun UPS setup; check database path/access and restore a backup if damaged")
    else:
        rows.append(("UPS database", "SKIPPED - UPS monitoring disabled"))
    try:
        peer = load_peer_config()
        if peer.enabled:
            for remote in peer.peers:
                check("Peer " + remote.node_id, lambda remote=remote: peer_connection(peer, remote),
                      "TLS connection and server certificate verified; run /servers doctor after startup",
                      "start this peer; check its address, firewall and matching certificate bundle")
        else:
            rows.append(("Private network", "SKIPPED - single-server configuration"))
    except Exception:
        rows.append(("Private network", "CHECK - repair peer-network.toml and certificate paths"))
    cloudflare = cfg["cloudflare"]
    if cloudflare.get("enabled"):
        targets = cloudflare.get("targets")
        if targets is None:
            targets = [dict(name="legacy", node_id="local", zone_id=cloudflare.get("zone_id"),
                            record_ids=cloudflare.get("record_ids", []))]
        def verify():
            count = verify_targets(targets, lambda name: secrets.get(name), write=False)
            if not count:
                raise ValueError("No records assigned to this server")
        check("Cloudflare", verify, "assigned A records readable; no DNS writes performed",
              "rerun mitra-cloudflare-setup; check credentials, zone access and server assignments")
    else:
        rows.append(("Cloudflare", "SKIPPED - DNS updates disabled"))
    return rows


def show_health(*, env_file=".env"):
    ui.step("Setup health checks")
    # Individual requests provide status output; avoid nesting live displays.
    rows = health_results(env_file)
    for name, result in rows:
        ui.message(f"{name}: {result}")
    return not any(result.startswith("CHECK") for _, result in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--plain", action="store_true")
    args = parser.parse_args()
    ui.configure(plain=args.plain)
    if not show_health(env_file=args.env_file):
        parser.exit(1)


if __name__ == "__main__":
    main()
