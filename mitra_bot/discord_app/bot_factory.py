# mitra_bot/discord_app/bot_factory.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import discord


@dataclass
class AppState:
    channel_id: Optional[int]
    admin_role_name: str
    ip_subscriber_role_name: str


def create_bot(*, state: AppState) -> discord.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = os.getenv("MITRA_ENABLE_MEMBERS_INTENT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    intents.message_content = False

    bot = discord.Bot(intents=intents)
    bot.state = state  # type: ignore[attr-defined]

    _register_cogs(bot)
    return bot


def _register_cogs(bot: discord.Bot) -> None:
    try:
        from mitra_bot.discord_app.cogs.about_cog import AboutCog
        from mitra_bot.discord_app.cogs.ip_cog import IPCog
        from mitra_bot.discord_app.cogs.power_cog import PowerCog
        from mitra_bot.discord_app.cogs.settings_cog import SettingsCog
        from mitra_bot.discord_app.cogs.todo_cog import TodoCog
        from mitra_bot.discord_app.cogs.update_cog import UpdateCog
        from mitra_bot.discord_app.cogs.ups_cog import UPSCog

        bot.add_cog(AboutCog(bot))  # type: ignore[arg-type]
        bot.add_cog(IPCog(bot))  # type: ignore[arg-type]
        bot.add_cog(PowerCog(bot))  # type: ignore[arg-type]
        bot.add_cog(SettingsCog(bot))  # type: ignore[arg-type]
        bot.add_cog(TodoCog(bot))  # type: ignore[arg-type]
        bot.add_cog(UpdateCog(bot))  # type: ignore[arg-type]
        bot.add_cog(UPSCog(bot))  # type: ignore[arg-type]

        logging.info("Cogs registered.")
    except Exception:
        logging.exception("Cog registration failed.")
