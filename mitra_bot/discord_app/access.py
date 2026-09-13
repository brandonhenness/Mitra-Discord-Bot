"""Operator-owned infrastructure boundary, independent of Discord role names."""
from __future__ import annotations

PUBLIC_COMMANDS = frozenset({"about", "todo"})


def infrastructure_guild(bot, guild) -> bool:
    guild_id = getattr(guild, "id", guild)
    return guild_id is not None and guild_id in getattr(
        getattr(bot, "state", None), "infrastructure_guild_ids", ()
    )


def infrastructure_channel(bot, channel) -> bool:
    return infrastructure_guild(bot, getattr(channel, "guild", None))


def command_allowed(bot, guild, name: str) -> bool:
    # New commands are private unless explicitly reviewed for public use.
    return guild is not None and (name in PUBLIC_COMMANDS or infrastructure_guild(bot, guild))
