# mitra_bot/discord_app/checks.py
from __future__ import annotations

import discord

from mitra_bot.services.role_manager import member_has_role


def ensure_admin(ctx: discord.ApplicationContext):
    """
    Guard for admin-only commands.
    Returns a response if blocked, else None.
    """
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        return ctx.respond("This command can only be used in a server.", ephemeral=True)

    role_name = ctx.bot.state.admin_role_name  # type: ignore[attr-defined]
    if member_has_role(ctx.author, role_name):
        return None

    return ctx.respond(
        "You do not have permission to use this command.", ephemeral=True
    )
