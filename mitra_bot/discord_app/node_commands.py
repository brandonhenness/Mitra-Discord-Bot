"""Node-local operations exposed through the authenticated peer connection."""
import asyncio
import platform

import discord

from mitra_bot import __version__
from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.services.peer_service import PeerError


async def local_operation(bot, operation, payload):
    if operation == "node_info" and not payload:
        mesh = getattr(bot, "peer_service", None)
        return {
            "version": __version__, "python": platform.python_version(),
            "pycord": discord.__version__, "discord_servers": len(bot.guilds),
            "health": mesh.local_health() if mesh else {},
        }
    if operation == "public_ip" and not payload:
        return {"ip": await asyncio.to_thread(get_public_ip)}
    if operation == "ups_settings":
        cog = bot.get_cog("UPSCog")
        if cog is None:
            raise PeerError("UPS controls are unavailable")
        return cog.apply_settings(payload)
    raise PeerError("Invalid node operation or parameters")


async def node_operation(bot, target, operation, payload=None):
    mesh = getattr(bot, "peer_service", None)
    payload = payload or {}
    if mesh and target != mesh.config.node_id:
        # Public-IP discovery has its own ten-second HTTP timeout.
        return await mesh.request(target, operation, payload, timeout=15)
    return await local_operation(bot, operation, payload)


def selected_nodes(bot, server, *, default_all=False):
    mesh = getattr(bot, "peer_service", None)
    if server == "all" or (server is None and default_all):
        return sorted([mesh.config.node_id, *mesh.peers]) if mesh else ["local"]
    from mitra_bot.discord_app.server_target import resolve_server
    return [resolve_server(bot, server)]


async def read_nodes(bot, nodes, operation):
    semaphore = asyncio.Semaphore(8)
    async def read(node):
        async with semaphore:
            try:
                return node, await node_operation(bot, node, operation)
            except PeerError:
                return node, None
    return await asyncio.gather(*(read(node) for node in nodes))
