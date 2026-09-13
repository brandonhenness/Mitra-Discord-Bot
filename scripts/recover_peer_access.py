"""Recover a running peer from the owner, including with Mitra v0.2.2 installed.

Run from the owner's bot installation directory using its Python environment.
Only outbound authenticated RPCs are used; no listener or second bot is started.
"""
import argparse
import asyncio
from pathlib import Path
import sys
import time
import uuid

# This standalone script may be downloaded outside the installed package.
sys.path.insert(0, str(Path.cwd()))

from packaging.version import Version
from mitra_bot.peer_config import load_peer_config
from mitra_bot.services.peer_service import PeerError, PeerService
from mitra_bot.settings import load_settings


async def recover(mesh, ids, target, *, timeout=900, poll_seconds=5):
    version = "0.2.3"
    if not mesh.is_state_owner:
        raise PeerError("Run this on the configured settings owner, using its normal configuration")
    mesh.resolve(target)
    if target == mesh.config.node_id:
        raise PeerError("Choose a remote peer, not the settings owner")
    if not ids or any(type(value) is not int or not 0 < value < 2**64 for value in ids):
        raise ValueError("The owner's infrastructure allowlist must contain valid Discord server IDs")
    ids = sorted(set(ids))
    job = uuid.uuid5(uuid.NAMESPACE_URL,
        f"mitra-access-recovery:{mesh.config.network_id}:{mesh.config.node_id}:{target}:{version}").hex
    print(f"Owner: {mesh.config.node_id}; target: {target}; update reference: {job}", flush=True)
    status = await mesh.request(target, "update_status", {"job": job})
    original_boot = status["boot"]
    needs_update = Version(status["version"]) < Version(version)
    if needs_update:
        existing = status.get("job")
        if existing and existing.get("state") in {"failed", "complete"}:
            raise PeerError(f"Previous recovery job ended as {existing['state']}: {existing.get('error')}. Inspect it before another update.")
        if not existing:
            print(f"Requesting {target} update to {version} through the private peer connection...", flush=True)
            try:
                await mesh.request(target, "update_install", {"job": job, "version": version})
            except PeerError:
                # An acknowledgement can be lost after acceptance. Poll the same job;
                # never silently create or submit another install request.
                print("Update acknowledgement not confirmed; checking the same job.", flush=True)
    deadline = time.monotonic() + timeout
    good = 0
    last_report = None
    while time.monotonic() < deadline:
        try:
            status = await mesh.request(target, "update_status", {"job": job}, timeout=10)
        except PeerError:
            status = None
        if status:
            entry = status.get("job") or {}
            if entry.get("state") == "failed":
                raise PeerError(f"Remote update failed: {entry.get('error')}")
            if needs_update and not entry:
                raise PeerError(f"Update acceptance remains unconfirmed. Rerun this script to check/reuse reference {job}.")
            ready = (Version(status["version"]) >= Version(version) and status.get("discord") is True
                     and (not needs_update or status["boot"] != original_boot)
                     and (not needs_update or entry.get("state") == "complete"))
            report = f"{target}: version={status['version']}; update={entry.get('state', 'not required')}; Discord={status.get('discord')}"
        else:
            ready, report = False, f"{target}: waiting for peer reconnection..."
        if report != last_report:
            print(report, flush=True)
            last_report = report
        good = good + 1 if ready else 0
        if good >= 2:
            break
        await asyncio.sleep(poll_seconds)
    else:
        raise PeerError(f"Timed out waiting for recovery job {job}. The remote update may still be running; rerun to check it.")
    capabilities = await mesh.request(target, "capabilities", {})
    if "infrastructure_access" not in capabilities.get("operations", []):
        raise PeerError("Target still does not support access synchronization; no config change attempted")
    result = await mesh.request(target, "infrastructure_access", {"infrastructure_guild_ids": ids})
    if result.get("saved") is not True or result.get("infrastructure_guild_ids") != ids:
        raise PeerError("Configuration change was not confirmed; inspect the target before retrying")
    print(f"SUCCESS: {target} saved and applied infrastructure_guild_ids = {ids}. No further restart required.", flush=True)


async def run(target):
    settings = load_settings(interactive_token=False)
    config = load_peer_config()
    if not config.enabled:
        raise PeerError("Private peer networking is not enabled in this environment")
    mesh = PeerService(config, snapshot=lambda: {}, power=None)
    mesh.configure_discord_identity(settings.token)
    _, mesh.client_ssl = mesh._tls_contexts()
    await recover(mesh, settings.infrastructure_guild_ids, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer", help="Remote peer node ID, such as Anubis")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.peer))
    except KeyboardInterrupt:
        parser.exit(1, "Stopped monitoring. An accepted remote update continues; rerun to check its status.\n")
    except (PeerError, ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Recovery not confirmed: {exc}\n")


if __name__ == "__main__":
    main()
