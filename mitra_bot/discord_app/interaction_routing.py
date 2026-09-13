"""Select a responder, never treating an uncertain acknowledgement as ownership."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import discord


def command_route(data: dict, mesh) -> tuple[str, bool]:
    """Return preferred node and whether another node may handle the command."""
    path = [data.get("name", "")]
    options = data.get("options", [])
    while options and options[0].get("type") in (1, 2):
        path.append(options[0]["name"])
        options = options[0].get("options", [])
    target = next((o.get("value") for o in options if o.get("name") == "server"), None)
    if path[:2] == ["servers", "sync-access"]:
        return mesh.config.resolved_state_owner, False
    if path[0] == "power" or path[:2] == ["ups", "status"]:
        return str(target or mesh.config.resolved_state_owner), True
    if path[0] in {"servers", "alerts", "about"} or path[:2] == ["ip", "status"]:
        return mesh.config.resolved_state_owner, True
    # Existing unreplicated application state has a fixed owner, never a random writer.
    return mesh.config.resolved_state_owner, False


def response_delay(mesh, preferred: str, interaction_id: int) -> float:
    if mesh.config.node_id == preferred:
        return 0.0
    others = sorted(
        {mesh.config.node_id, *mesh.peers} - {preferred},
        key=lambda node: hashlib.sha256(f"{interaction_id}:{node}".encode()).digest(),
    )
    return 0.4 + 0.6 * others.index(mesh.config.node_id) / max(1, len(others))


async def claim_interaction(interaction, *, delay: float = 0, ephemeral: bool = True) -> bool:
    """Only a successful initial ACK permits a handler to run. No followup fallback."""
    created = interaction.created_at.timestamp()
    remaining = created + 2.7 - time.time()
    if remaining <= delay:
        return False
    if delay:
        await asyncio.sleep(delay)
    try:
        await asyncio.wait_for(
            interaction.response.defer(ephemeral=ephemeral),
            timeout=max(0.01, created + 2.7 - time.time()),
        )
        return True
    except (discord.HTTPException, discord.InteractionResponded, OSError, asyncio.TimeoutError):
        # Includes 40060 (another instance acknowledged) and ambiguous network errors.
        logging.debug("Interaction %s was not claimed by this node", interaction.id)
        return False


class ClaimedContext(discord.ApplicationContext):
    @property
    def defer(self):
        async def defer(**kwargs):
            if not self.interaction.response.is_done():
                await self.interaction.response.defer(**kwargs)
        return defer
