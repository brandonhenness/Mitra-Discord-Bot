"""Run on the settings owner: python -m mitra_bot.sync_access <peer>."""
import argparse
import asyncio
from types import SimpleNamespace

from mitra_bot.peer_config import load_peer_config
from mitra_bot.services.access_sync import sync_peer_access
from mitra_bot.services.peer_service import PeerError, PeerService
from mitra_bot.settings import load_settings


async def sync(target):
    settings = load_settings(interactive_token=False)
    config = load_peer_config()
    if not config.enabled:
        raise PeerError("Private peer networking is not enabled")
    mesh = PeerService(config, snapshot=lambda: {}, power=None)
    if not mesh.is_state_owner:
        raise PeerError("Run this operation on the configured shared settings owner")
    mesh.configure_discord_identity(settings.token)
    _, mesh.client_ssl = mesh._tls_contexts()
    # Outbound RPC only: do not start another bot, listener, or database writer.
    ids = await sync_peer_access(SimpleNamespace(peer_service=mesh, state=settings), target)
    print(f"{target}: infrastructure_guild_ids = {ids}; saved and applied. No restart required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer")
    args = parser.parse_args()
    try:
        asyncio.run(sync(args.peer))
    except (PeerError, ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Access synchronization failed: {exc}\n")


if __name__ == "__main__":
    main()
