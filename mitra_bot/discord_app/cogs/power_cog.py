# mitra_bot/discord_app/cogs/power_cog.py
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.services.power_service import execute_power_action


class PowerCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    power = discord.SlashCommandGroup(
        name="power",
        description="Admin power controls",
    )

    # -----------------------------
    # /power restart
    # -----------------------------

    @power.command(
        name="restart", description="Restart the server running this bot (admins only)."
    )
    async def restart(
        self,
        ctx: discord.ApplicationContext,
        confirm: str = discord.Option(
            str,
            description="Type RESTART exactly to confirm.",
            required=True,
        ),
        mode: str = discord.Option(
            str,
            description="Restart timing.",
            required=False,
            default="immediate",
            choices=["immediate", "delayed"],
        ),
        delay_seconds: int = discord.Option(
            int,
            description="Delay in seconds (only used when mode=delayed). Example: 60",
            required=False,
            default=0,
            min_value=0,
            max_value=86400,
        ),
        force: bool = discord.Option(
            bool,
            description="Force close apps without warning (Windows only).",
            required=False,
            default=False,
        ),
    ) -> None:
        # ensure_admin is async (it calls ctx.respond), so it MUST be awaited
        if ensure_admin(ctx):
            return

        if confirm.strip().upper() != "RESTART":
            await ctx.respond(
                "Confirmation failed. You must type RESTART exactly.", ephemeral=True
            )
            return

        mode = (mode or "immediate").strip().lower()
        if mode == "immediate":
            delay_seconds = 0
        else:
            if delay_seconds < 1:
                await ctx.respond(
                    "For delayed mode, delay must be at least 1 second.", ephemeral=True
                )
                return

        await ctx.respond(
            f"Server restart scheduled.\nMode: `{mode}`\nDelay: `{delay_seconds}` seconds\nForce: `{force}`",
            ephemeral=True,
        )

        async def _do() -> None:
            try:
                logging.warning(
                    "Power restart requested by user_id=%s mode=%s delay=%s force=%s",
                    getattr(ctx.user, "id", "unknown"),
                    mode,
                    delay_seconds,
                    force,
                )
                # execute_power_action is synchronous/blocking (subprocess), so run off-thread
                await asyncio.to_thread(
                    execute_power_action,
                    "restart",
                    delay_seconds=delay_seconds,
                    force=force,
                )
            except Exception:
                logging.exception("Power restart failed.")

        asyncio.create_task(_do())

    # -----------------------------
    # /power shutdown
    # -----------------------------

    @power.command(
        name="shutdown",
        description="Shut down (power off) the server running this bot (admins only).",
    )
    async def shutdown(
        self,
        ctx: discord.ApplicationContext,
        confirm: str = discord.Option(
            str,
            description="Type SHUTDOWN exactly to confirm.",
            required=True,
        ),
        mode: str = discord.Option(
            str,
            description="Shutdown timing.",
            required=False,
            default="immediate",
            choices=["immediate", "delayed"],
        ),
        delay_seconds: int = discord.Option(
            int,
            description="Delay in seconds (only used when mode=delayed). Example: 60",
            required=False,
            default=0,
            min_value=0,
            max_value=86400,
        ),
        force: bool = discord.Option(
            bool,
            description="Force close apps without warning (Windows only).",
            required=False,
            default=False,
        ),
    ) -> None:
        if ensure_admin(ctx):
            return

        if confirm.strip().upper() != "SHUTDOWN":
            await ctx.respond(
                "Confirmation failed. You must type SHUTDOWN exactly.", ephemeral=True
            )
            return

        mode = (mode or "immediate").strip().lower()
        if mode == "immediate":
            delay_seconds = 0
        else:
            if delay_seconds < 1:
                await ctx.respond(
                    "For delayed mode, delay must be at least 1 second.", ephemeral=True
                )
                return

        await ctx.respond(
            f"Server shutdown scheduled.\nMode: `{mode}`\nDelay: `{delay_seconds}` seconds\nForce: `{force}`",
            ephemeral=True,
        )

        async def _do() -> None:
            try:
                logging.warning(
                    "Power shutdown requested by user_id=%s mode=%s delay=%s force=%s",
                    getattr(ctx.user, "id", "unknown"),
                    mode,
                    delay_seconds,
                    force,
                )
                await asyncio.to_thread(
                    execute_power_action,
                    "shutdown",
                    delay_seconds=delay_seconds,
                    force=force,
                )
            except Exception:
                logging.exception("Power shutdown failed.")

        asyncio.create_task(_do())

    # -----------------------------
    # /power cancel
    # -----------------------------

    @power.command(
        name="cancel", description="Cancel a pending shutdown/restart (admins only)."
    )
    async def cancel(
        self,
        ctx: discord.ApplicationContext,
        confirm: str = discord.Option(
            str,
            description="Type CANCEL exactly to confirm.",
            required=True,
        ),
    ) -> None:
        if ensure_admin(ctx):
            return

        if confirm.strip().upper() != "CANCEL":
            await ctx.respond(
                "Confirmation failed. You must type CANCEL exactly.", ephemeral=True
            )
            return

        await ctx.respond(
            "Attempting to cancel any pending shutdown/restart.", ephemeral=True
        )

        try:
            await asyncio.to_thread(
                execute_power_action,
                "cancel",
                delay_seconds=0,
                force=False,
            )
            logging.warning(
                "Power cancel executed by user_id=%s",
                getattr(ctx.user, "id", "unknown"),
            )
        except Exception as ex:
            logging.exception("Power cancel failed.")
            await ctx.respond(f"Cancel failed:\n```{ex}```", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PowerCog(bot))
