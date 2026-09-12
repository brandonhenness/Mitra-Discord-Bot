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
        try:
            return await mesh.request(target, operation, payload, timeout=15)
        except PeerError as original:
            if original.__cause__ is not None:
                raise
            # Distinguish a reachable older peer from a broken connection.
            try:
                capability = await mesh.request(target, "capabilities", {}, timeout=3)
            except PeerError:
                try:
                    await mesh.request(target, "health", {}, timeout=3)
                except PeerError:
                    raise original
                raise PeerError(f"{target} is reachable but rejected {operation} and cannot advertise feature support. "
                                "Update this node to the same release, then retry.") from original
            if operation not in capability.get("operations", []):
                raise PeerError(f"{target} (version {capability.get('version', 'unknown')}) does not support {operation}. "
                                "Update that node before using this command.") from original
            raise original
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
            except PeerError as exc:
                return node, {"error": str(exc)}
    return await asyncio.gather(*(read(node) for node in nodes))
