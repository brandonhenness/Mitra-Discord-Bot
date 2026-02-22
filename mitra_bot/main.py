# mitra_bot/main.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import discord
from mitra_bot.discord_app.bot_factory import AppState, create_bot
from mitra_bot.logging_setup import setup_logging
from mitra_bot.storage.cache_schema import RestartNoticeRuntimeModel
from mitra_bot.storage.cache_store import (
    clear_power_restart_notice,
    get_notification_channel_map,
    get_power_restart_notice,
    get_updater_config,
)
from mitra_bot.settings import load_settings
from mitra_bot.services.role_manager import ensure_role
from mitra_bot.tasks.ip_monitor_task import IPMonitorTask
from mitra_bot.tasks.update_monitor_task import UpdateMonitorTask
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
    updater_cfg = get_updater_config()
    update_task = UpdateMonitorTask(
        bot,
        interval_seconds=int(updater_cfg.get("check_interval_seconds", 21600)),
    )

    started = {"done": False}

    def _ctx_value(ctx: discord.ApplicationContext, name: str, default: Any = None) -> Any:
        return getattr(ctx, name, default)

    def _command_name(ctx: discord.ApplicationContext) -> str:
        cmd = _ctx_value(ctx, "command")
        if cmd is not None:
            qualified = getattr(cmd, "qualified_name", None)
            if qualified:
                return str(qualified)
            plain = getattr(cmd, "name", None)
            if plain:
                return str(plain)

        interaction = _ctx_value(ctx, "interaction")
        data = getattr(interaction, "data", None) if interaction is not None else None
        if isinstance(data, dict):
            raw_name = data.get("name")
            if raw_name:
                return str(raw_name)
        return "unknown"

    def _ctx_scope(ctx: discord.ApplicationContext) -> tuple[str, str, str]:
        guild = _ctx_value(ctx, "guild")
        user = _ctx_value(ctx, "user") or _ctx_value(ctx, "author")
        channel = _ctx_value(ctx, "channel")

        guild_id = str(getattr(guild, "id", "dm"))
        channel_id = str(_ctx_value(ctx, "channel_id", None) or getattr(channel, "id", "unknown"))
        user_id = str(getattr(user, "id", "unknown"))
        return guild_id, channel_id, user_id

    def _command_options(ctx: discord.ApplicationContext) -> str:
        selected = _ctx_value(ctx, "selected_options")
        if selected is not None:
            text = repr(selected)
            return text if len(text) <= 500 else (text[:497] + "...")

        interaction = _ctx_value(ctx, "interaction")
        data = getattr(interaction, "data", None) if interaction is not None else None
        if isinstance(data, dict):
            options = data.get("options")
            if options is not None:
                text = repr(options)
                return text if len(text) <= 500 else (text[:497] + "...")
        return "{}"

    @bot.event
    async def on_application_command(ctx: discord.ApplicationContext) -> None:
        command_name = _command_name(ctx)
        guild_id, channel_id, user_id = _ctx_scope(ctx)
        options = _command_options(ctx)
        logging.info(
            "Command invoke: /%s guild_id=%s channel_id=%s user_id=%s options=%s",
            command_name,
            guild_id,
            channel_id,
            user_id,
            options,
        )

    @bot.event
    async def on_application_command_completion(ctx: discord.ApplicationContext) -> None:
        command_name = _command_name(ctx)
        guild_id, channel_id, user_id = _ctx_scope(ctx)
        logging.info(
            "Command complete: /%s guild_id=%s channel_id=%s user_id=%s",
            command_name,
            guild_id,
            channel_id,
            user_id,
        )

    @bot.event
    async def on_application_command_error(
        ctx: discord.ApplicationContext, error: Exception
    ) -> None:
        command_name = _command_name(ctx)
        guild_id, channel_id, user_id = _ctx_scope(ctx)
        logging.exception(
            "Command error: /%s guild_id=%s channel_id=%s user_id=%s err=%s",
            command_name,
            guild_id,
            channel_id,
            user_id,
            error,
        )

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

        logging.info(
            "Starting tasks: ip_monitor=%ss ups_monitor=%ss update_monitor=%ss",
            settings.ip_poll_seconds,
            settings.ups.poll_seconds,
            int(updater_cfg.get("check_interval_seconds", 21600)),
        )
        await ip_task.start()
        await ups_task.start()
        await update_task.start()
        logging.info("Background tasks started.")

        if bool(updater_cfg.get("enabled", True)) and bool(
            updater_cfg.get("check_on_startup", True)
        ):
            update_cog = bot.get_cog("UpdateCog")
            if update_cog is not None:
                try:
                    await update_cog.notify_if_update_available(source="startup")  # type: ignore[attr-defined]
                except Exception:
                    logging.exception("Startup update check failed.")

        restart_notice = get_power_restart_notice()
        if restart_notice:
            notice = RestartNoticeRuntimeModel.model_validate(restart_notice)
            channel_id = notice.channel_id
            message_id = notice.message_id
            delay = notice.delay_seconds
            force = notice.force
            requester = notice.requested_by_user_id
            confirmer = notice.confirmed_by_user_id
            requested_at_epoch = notice.requested_at_epoch
            confirmed_at_epoch = notice.confirmed_at_epoch
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

        per_guild_notify = get_notification_channel_map()
        if per_guild_notify:
            rendered = ", ".join(
                f"{gid}->{cid}" for gid, cid in sorted(per_guild_notify.items())
            )
            logging.info("Configured notify channels per guild: %s", rendered)
        elif settings.channel_id:
            logging.info(
                "Configured legacy notify channel id: %s (from channel/channel_id)",
                settings.channel_id,
            )
        else:
            logging.info(
                "No notify channels configured. Use /notifications channel set."
            )

        logging.info("To-Do lists are managed via per-guild To-Do category and list channels.")

    try:
        await bot.start(settings.token)
    except RuntimeError as exc:
        if getattr(bot, "_mitra_restart_requested", False) and "Session is closed" in str(
            exc
        ):
            logging.info(
                "Ignoring session-closed runtime during intentional updater restart."
            )
            return
        raise


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
