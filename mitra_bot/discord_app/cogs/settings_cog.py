from __future__ import annotations
from mitra_bot.discord_app.message_style import notice

import discord
from discord.ext import commands
from pydantic import BaseModel, ConfigDict, Field

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.storage.storage_store import (
    clear_notification_channel_id_for_guild,
    get_notification_channel_id_for_guild,
    set_notification_channel_id_for_guild,
)


class NotificationChannelSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)


class GuildScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int = Field(gt=0)


class SettingsCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    notifications = discord.SlashCommandGroup(
        name="notifications",
        description="Notification settings",
    )
    channel = notifications.create_subgroup(
        name="channel",
        description="Notification channel settings",
    )

    @channel.command(
        name="set",
        description="Set this server's notification channel (admins only).",
    )
    async def channel_set(
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
            await ctx.respond(notice('Use this command in Discord', "This command can only be used in a server.", tone='warning'), ephemeral=True)
            return

        if channel.guild.id != ctx.guild.id:
            await ctx.respond(notice('Choose a channel', 'Choose a channel in this Discord server.', tone='warning'), ephemeral=True)
            return
        setting = NotificationChannelSetting(
            guild_id=ctx.guild.id,
            channel_id=channel.id,
        )
        set_notification_channel_id_for_guild(setting.guild_id, setting.channel_id)
        await ctx.respond(
            notice('Notification channel saved', f"Notification channel set to {channel.mention} for this server.", tone='success'),
            ephemeral=True,
        )

    @channel.command(
        name="show",
        description="Show this server's configured notification channel.",
    )
    async def channel_show(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(notice('Use this command in Discord', "This command can only be used in a server.", tone='warning'), ephemeral=True)
            return

        scope = GuildScope(guild_id=ctx.guild.id)
        channel_id = get_notification_channel_id_for_guild(scope.guild_id)
        if not channel_id:
            await ctx.respond(
                notice('Choose a notification channel', "No notification channel is configured for this server.", tone='info'),
                ephemeral=True,
            )
            return
        await ctx.respond(
            notice('Notification settings', f"Notification channel for this server is <#{channel_id}>.", tone='info'),
            ephemeral=True,
        )

    @channel.command(
        name="clear",
        description="Clear this server's notification channel (admins only).",
    )
    async def channel_clear(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return
        if ctx.guild is None:
            await ctx.respond(notice('Use this command in Discord', "This command can only be used in a server.", tone='warning'), ephemeral=True)
            return

        scope = GuildScope(guild_id=ctx.guild.id)
        clear_notification_channel_id_for_guild(scope.guild_id)
        await ctx.respond(
            notice('Notification channel cleared', "Cleared this server's notification channel setting.", tone='success'),
            ephemeral=True,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SettingsCog(bot))
