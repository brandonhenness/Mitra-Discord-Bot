from __future__ import annotations

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.storage.cache_store import (
    clear_notification_channel_id_for_guild,
    get_notification_channel_id_for_guild,
    set_notification_channel_id_for_guild,
)


class SettingsCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    settings = discord.SlashCommandGroup(
        name="settings",
        description="Admin bot settings",
    )

    @settings.command(
        name="notification_channel_set",
        description="Set this server's notification channel (admins only).",
    )
    async def notification_channel_set(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.TextChannel = discord.Option(
            discord.TextChannel,
            description="Channel for IP change notifications.",
            required=True,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return
        if ctx.guild is None:
            await ctx.respond("This command can only be used in a server.", ephemeral=True)
            return

        set_notification_channel_id_for_guild(ctx.guild.id, channel.id)
        await ctx.respond(
            f"Notification channel set to {channel.mention} for this server.",
            ephemeral=True,
        )

    @settings.command(
        name="notification_channel_show",
        description="Show this server's configured notification channel.",
    )
    async def notification_channel_show(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can only be used in a server.", ephemeral=True)
            return

        channel_id = get_notification_channel_id_for_guild(ctx.guild.id)
        if not channel_id:
            await ctx.respond(
                "No notification channel is configured for this server.",
                ephemeral=True,
            )
            return
        await ctx.respond(
            f"Notification channel for this server is <#{channel_id}>.",
            ephemeral=True,
        )

    @settings.command(
        name="notification_channel_clear",
        description="Clear this server's notification channel (admins only).",
    )
    async def notification_channel_clear(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return
        if ctx.guild is None:
            await ctx.respond("This command can only be used in a server.", ephemeral=True)
            return

        clear_notification_channel_id_for_guild(ctx.guild.id)
        await ctx.respond(
            "Cleared this server's notification channel setting.",
            ephemeral=True,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SettingsCog(bot))
