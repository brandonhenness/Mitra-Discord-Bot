# mitra_bot/main.py
from __future__ import annotations

import asyncio
import logging

from mitra_bot.discord_app.bot_factory import AppState, create_bot
from mitra_bot.logging_setup import setup_logging
from mitra_bot.settings import load_settings
from mitra_bot.services.role_manager import ensure_role
from mitra_bot.tasks.ip_monitor_task import IPMonitorTask
from mitra_bot.tasks.ups_monitor_task import UPSMonitorTask


async def main_async() -> None:
    setup_logging(level=logging.INFO, logfile="bot.log", add_file_handler=True)

    settings = load_settings(interactive_token=True)

    state = AppState(
        channel_id=settings.channel_id,
        admin_role_name=settings.admin_role_name,
        ip_subscriber_role_name=settings.ip_subscriber_role_name,
    )

    bot = create_bot(state=state)

    ip_task = IPMonitorTask(bot, interval_seconds=settings.ip_poll_seconds)
    ups_task = UPSMonitorTask(bot, poll_seconds=settings.ups.poll_seconds)

    started = {"done": False}

    @bot.event
    async def on_ready():
        if started["done"]:
            return
        started["done"] = True

        logging.info(
            "Logged in as %s (id=%s)",
            bot.user,
            bot.user.id if bot.user else "unknown",
        )

        # Ensure roles exist in every guild the bot is in
        for guild in bot.guilds:
            await ensure_role(guild, settings.admin_role_name)
            await ensure_role(guild, settings.ip_subscriber_role_name)

        await ip_task.start()
        await ups_task.start()

        if settings.channel_id:
            logging.info("Configured notify channel id: %s", settings.channel_id)
        else:
            logging.info(
                "No notify channel configured in cache.json (channel/channel_id)."
            )

    await bot.start(settings.token)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
