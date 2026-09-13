# mitra_bot/discord_app/checks.py
from __future__ import annotations
from mitra_bot.discord_app.message_style import notice

import discord

from mitra_bot.services.role_manager import member_has_role
from mitra_bot.discord_app.access import infrastructure_guild


def ensure_admin(ctx: discord.ApplicationContext):
    """
    Guard for admin-only commands.
    Returns a response if blocked, else None.
    """
    if not infrastructure_guild(ctx.bot, ctx.guild):
        return ctx.respond(notice('Command unavailable', 'This command is not available in this Discord server.', tone='warning'), ephemeral=True)
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        return ctx.respond(notice('Use this command in Discord', "This command can only be used in a server.", tone='warning'), ephemeral=True)

    role_name = ctx.bot.state.admin_role_name  # type: ignore[attr-defined]
    if member_has_role(ctx.author, role_name):
        return None

    return ctx.respond(
        notice('Permission required', "You do not have permission to use this command.", tone='warning'), ephemeral=True
    )
