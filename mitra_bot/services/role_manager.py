# mitra_bot/services/role_manager.py
from __future__ import annotations

import logging
from typing import Optional

import discord


async def ensure_role(guild: discord.Guild, role_name: str) -> discord.Role:
    """
    Ensure a role exists in the guild. Create it if missing.
    """
    existing = discord.utils.get(guild.roles, name=role_name)
    if existing:
        return existing

    logging.info(
        "Creating role '%s' in guild '%s' (%s)", role_name, guild.name, guild.id
    )

    # Reason helps audit logs
    role = await guild.create_role(
        name=role_name,
        mentionable=True,
        reason="Mitra bot auto-created required role",
    )
    return role


def member_has_role(member: discord.Member, role_name: str) -> bool:
    return discord.utils.get(member.roles, name=role_name) is not None


def get_role_id(guild: discord.Guild, role_name: str) -> Optional[int]:
    role = discord.utils.get(guild.roles, name=role_name)
    return role.id if role else None
