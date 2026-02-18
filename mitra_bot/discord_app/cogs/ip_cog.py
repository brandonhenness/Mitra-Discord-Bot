# mitra_bot/discord_app/cogs/ip_cog.py
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.services.notifier import Notifier
from mitra_bot.services.role_manager import ensure_role
from mitra_bot.storage.cache_store import save_ip


def _format_ip_message(ip: str, *, is_change: bool) -> str:
    title = "🌐 Public IP changed" if is_change else "🌐 Current public IP"
    return f"{title}:\n```{ip}```"


class IPCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    ip = discord.SlashCommandGroup(
        name="ip",
        description="Public IP monitoring commands",
    )

    @ip.command(name="status", description="Show current public IP")
    async def status(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        ip = get_public_ip()
        if not ip:
            await ctx.respond("Failed to fetch public IP.", ephemeral=True)
            return

        await ctx.respond(_format_ip_message(ip, is_change=False), ephemeral=True)

    @ip.command(
        name="subscribe", description="Subscribe to IP change alerts (adds a role)"
    )
    async def subscribe(self, ctx: discord.ApplicationContext):
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
        role = await ensure_role(ctx.guild, role_name)

        await ctx.author.add_roles(role, reason="User subscribed to IP alerts")
        await ctx.respond(f"Subscribed. Added role: **{role.name}**", ephemeral=True)

    @ip.command(
        name="unsubscribe", description="Unsubscribe from IP alerts (removes a role)"
    )
    async def unsubscribe(self, ctx: discord.ApplicationContext):
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
        role = discord.utils.get(ctx.guild.roles, name=role_name)

        if not role:
            await ctx.respond("Subscriber role does not exist.", ephemeral=True)
            return

        await ctx.author.remove_roles(role, reason="User unsubscribed from IP alerts")
        await ctx.respond(
            f"Unsubscribed. Removed role: **{role.name}**", ephemeral=True
        )

    async def notify_ip_change(self, new_ip: str):
        """
        Called by the IP monitor task when IP changes.
        Sends to the configured channel and mentions the subscriber role.
        """

        logging.info("IP changed to %s — sending notification.", new_ip)

        msg_body = _format_ip_message(new_ip, is_change=True)

        channel_id = self.bot.state.channel_id  # type: ignore[attr-defined]
        if not channel_id:
            logging.warning("No channel_id configured; cannot send IP change alert.")
            return

        notifier = Notifier(self.bot)

        # Try to resolve subscriber role from any guild
        mention_prefix = ""
        for guild in self.bot.guilds:
            role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                # Ensure it is mentionable (safe even if already true)
                if not role.mentionable:
                    try:
                        await role.edit(
                            mentionable=True,
                            reason="Mitra bot needs to mention this role",
                        )
                    except Exception:
                        logging.debug("Could not set role to mentionable.")

                mention_prefix = f"{role.mention}\n"
                break

        await notifier.send_to_channel(channel_id, mention_prefix + msg_body)

        await save_ip(new_ip)
