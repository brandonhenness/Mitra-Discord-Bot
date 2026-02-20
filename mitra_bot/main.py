# mitra_bot/main.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from mitra_bot.discord_app.bot_factory import AppState, create_bot
from mitra_bot.logging_setup import setup_logging
from mitra_bot.storage.cache_store import (
    clear_power_restart_notice,
    get_power_restart_notice,
)
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
        if not bot.intents.members:
            logging.warning(
                "Members intent is disabled. Thread leave -> auto-unassign may not work reliably. "
                "Enable Server Members Intent in Discord portal and set MITRA_ENABLE_MEMBERS_INTENT=true."
            )

        # Ensure roles exist in every guild the bot is in
        for guild in bot.guilds:
            await ensure_role(guild, settings.admin_role_name)
            await ensure_role(guild, settings.ip_subscriber_role_name)

        await ip_task.start()
        await ups_task.start()

        restart_notice = get_power_restart_notice()
        if restart_notice:
            channel_id = restart_notice.get("channel_id")
            message_id = restart_notice.get("message_id")
            delay = int(restart_notice.get("delay_seconds", 0))
            force = bool(restart_notice.get("force", False))
            requester = restart_notice.get("requested_by_user_id")
            confirmer = restart_notice.get("confirmed_by_user_id")
            requested_at_epoch = restart_notice.get("requested_at_epoch")
            confirmed_at_epoch = restart_notice.get("confirmed_at_epoch")
            restarted_at_epoch = int(datetime.now(timezone.utc).timestamp())
            mode = "immediate" if delay == 0 else "delayed"

            if channel_id and message_id:
                try:
                    channel = bot.get_channel(int(channel_id))
                    if channel is None:
                        channel = await bot.fetch_channel(int(channel_id))

                    if isinstance(channel, (discord.TextChannel, discord.Thread)):
                        msg = await channel.fetch_message(int(message_id))
                        embed = discord.Embed(
                            title="Restart Completed",
                            description="Server restart finished and bot is online.",
                            color=discord.Color.green(),
                        )
                        embed.add_field(name="Action", value="`restart`", inline=True)
                        embed.add_field(name="Mode", value=f"`{mode}`", inline=True)
                        embed.add_field(name="Delay", value=f"`{delay}` sec", inline=True)
                        embed.add_field(name="Force", value=f"`{force}`", inline=True)
                        if requester:
                            embed.add_field(
                                name="Requested By",
                                value=f"<@{requester}>",
                                inline=True,
                            )
                        if requested_at_epoch:
                            embed.add_field(
                                name="Requested At",
                                value=f"<t:{int(requested_at_epoch)}:F>",
                                inline=True,
                            )
                        if confirmer:
                            embed.add_field(
                                name="Confirmed By",
                                value=f"<@{confirmer}>",
                                inline=True,
                            )
                        if confirmed_at_epoch:
                            embed.add_field(
                                name="Confirmed At",
                                value=f"<t:{int(confirmed_at_epoch)}:F>",
                                inline=True,
                            )
                        embed.add_field(
                            name="Restarted At",
                            value=f"<t:{restarted_at_epoch}:F>",
                            inline=True,
                        )
                        await msg.edit(content=None, embed=embed, view=None)
                    else:
                        logging.warning("Restart notice channel is not a text channel/thread.")
                except Exception:
                    logging.exception("Failed to edit restart confirmation message after boot.")
            clear_power_restart_notice()

        if settings.channel_id:
            logging.info("Configured notify channel id: %s", settings.channel_id)
        else:
            logging.info(
                "No notify channel configured in cache.json (channel/channel_id)."
            )

        logging.info("To-Do lists are managed via per-guild To-Do category and list channels.")

    await bot.start(settings.token)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
