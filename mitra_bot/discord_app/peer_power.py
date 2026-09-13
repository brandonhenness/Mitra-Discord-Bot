"""Transferable, signed confirmation messages; execution is journaled at the target."""
from __future__ import annotations
from mitra_bot.discord_app.message_style import embed as styled_embed
from mitra_bot.discord_app.message_style import notice

import base64
import hashlib
import hmac
import struct
import time
from typing import Literal

import discord
from pydantic import BaseModel, ConfigDict, Field

from mitra_bot.discord_app.interaction_routing import claim_interaction, response_delay
from mitra_bot.services.peer_service import PeerError, PowerRequest

PAYLOAD = struct.Struct("!8s16sIIBQ")


class PowerIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    network: str
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    server: str
    action: Literal["restart", "shutdown"]
    delay_seconds: int = Field(ge=0, le=86400)
    force: bool
    requester_id: int = Field(gt=0, le=2**64 - 1)
    channel_id: int = Field(gt=0)
    guild_id: int = Field(gt=0)
    expires_at: int = Field(ge=0, le=2**32 - 1)


def operation_id(network: str, interaction_id: int) -> str:
    return hashlib.sha256(f"{network}:power:{interaction_id}".encode()).hexdigest()[:32]


def signature(key: bytes, decision: str, payload: str, scope: str) -> str:
    if not key:
        raise PeerError("Power confirmation signing is not configured")
    digest = hmac.new(key, f"{scope}:{decision}:{payload}".encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def render_intent(intent: PowerIntent, key: bytes):
    # Compact signed IDs keep operational metadata out of the visible message.
    raw = base64.urlsafe_b64encode(PAYLOAD.pack(
        hashlib.sha256(intent.server.encode()).digest()[:8], bytes.fromhex(intent.operation_id),
        intent.expires_at, intent.delay_seconds, (1 if intent.action == "restart" else 0) | (2 if intent.force else 0),
        intent.requester_id,
    )).decode().rstrip("=")
    scope = f"{intent.network}:{intent.guild_id}:{intent.channel_id}"
    embed = styled_embed(title=f"Confirm {intent.action} on {intent.server}", color=discord.Color.orange(),
                          description=f"This will {'restart' if intent.action == 'restart' else 'shut down'} **{intent.server}**. Review the details before confirming.\n\nConfirmation expires <t:{intent.expires_at}:R>.")
    embed.add_field(name="When", value="Immediately" if not intent.delay_seconds else f"After {intent.delay_seconds} seconds")
    embed.add_field(name="Force apps to close", value="Yes — unsaved work may be lost" if intent.force else "No", inline=False)
    embed.add_field(name="Requested by", value=f"<@{intent.requester_id}>")
    embed.set_footer(text="Only administrators can confirm. The named server executes the action.")
    view = discord.ui.View(timeout=None)
    for decision, style in (("confirm", discord.ButtonStyle.danger), ("cancel", discord.ButtonStyle.secondary)):
        view.add_item(discord.ui.Button(label=decision.title(), style=style,
                                       custom_id=f"mitra-power:{decision}:{raw}:{signature(key, decision, raw, scope)}"))
    # Do not register process-local callbacks. Every instance handles these IDs.
    view.stop()
    return embed, view


def decode_intent(mesh, interaction) -> tuple[PowerIntent, str]:
    custom_id = str((interaction.data or {}).get("custom_id", ""))
    parts = custom_id.split(":")
    if len(parts) != 4 or parts[0] != "mitra-power" or parts[1] not in {"confirm", "cancel"}:
        raise PeerError("Invalid power confirmation")
    message = interaction.message
    if message is None or message.author.id != interaction.client.user.id or not message.embeds:
        raise PeerError("Power confirmation must be an original bot message")
    raw = parts[2]
    guild_id = getattr(interaction.guild, "id", None)
    scope = f"{mesh.config.network_id}:{guild_id}:{interaction.channel_id}"
    if not hmac.compare_digest(parts[3], signature(mesh.signing_key, parts[1], raw, scope)):
        raise PeerError("Power confirmation signature is invalid")
    try:
        target_tag, op_bytes, expires, delay, flags, requester = PAYLOAD.unpack(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        targets = [node for node in [mesh.config.node_id, *mesh.peers]
                   if hashlib.sha256(node.encode()).digest()[:8] == target_tag]
        if len(targets) != 1 or flags & ~3:
            raise ValueError("Unknown target or flags")
        intent = PowerIntent(network=mesh.config.network_id, operation_id=op_bytes.hex(), server=targets[0],
                             action="restart" if flags & 1 else "shutdown", delay_seconds=delay, force=bool(flags & 2),
                             requester_id=requester, channel_id=interaction.channel_id, guild_id=guild_id, expires_at=expires)
    except (ValueError, struct.error) as exc:
        raise PeerError("Invalid power confirmation data") from exc
    if intent.expires_at < time.time():
        raise PeerError("Power confirmation expired. Issue a new command.")
    mesh.resolve(intent.server)
    return intent, parts[1]


async def send_power_prompt(ctx, mesh, *, action, target, delay_seconds, force):
    intent = PowerIntent(network=mesh.config.network_id,
                         operation_id=operation_id(mesh.config.network_id, ctx.interaction.id),
                         server=target, action=action, delay_seconds=delay_seconds, force=force,
                         requester_id=ctx.user.id, channel_id=ctx.channel_id, guild_id=ctx.guild.id,
                         expires_at=int(time.time()) + 600)
    embed, view = render_intent(intent, mesh.signing_key)
    await ctx.respond(embed=embed, view=view, ephemeral=True)


async def handle_power_component(bot, interaction):
    mesh = bot.peer_service
    try:
        intent, decision = decode_intent(mesh, interaction)
    except PeerError as exc:
        if await claim_interaction(interaction, delay=response_delay(mesh, mesh.config.resolved_state_owner, interaction.id)):
            await interaction.followup.send(notice('Action could not finish', str(exc), tone='error'), ephemeral=True)
        return
    if not await claim_interaction(interaction, delay=response_delay(mesh, intent.server, interaction.id)):
        return
    user = interaction.user
    if not isinstance(user, discord.Member) or not any(role.name == bot.state.admin_role_name for role in user.roles):
        await interaction.followup.send(notice('Administrator access required', "Only members with the admin role can use power confirmations.", tone='warning'), ephemeral=True)
        return
    request = PowerRequest(action=intent.action, delay_seconds=intent.delay_seconds, force=intent.force,
                           requester_id=str(intent.requester_id), confirmer_id=str(user.id),
                           decision="execute" if decision == "confirm" else "cancel")
    try:
        result = await mesh.power(intent.server, request, operation_id=intent.operation_id)
        await interaction.edit_original_response(content=notice('Power control', f"**{intent.server}**: {result}", tone='info'), embed=None, view=None)
    except PeerError as exc:
        await interaction.followup.send(notice('Power control', f"`{intent.server}`: {exc}", tone='info'), ephemeral=True)
