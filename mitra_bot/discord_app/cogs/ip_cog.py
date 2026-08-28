# mitra_bot/discord_app/cogs/ip_cog.py
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.services.notifier import Notifier
from mitra_bot.services.role_manager import ensure_role
from mitra_bot.storage.storage_store import (
    get_notification_channel_id_for_guild,
)


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

        ip = await asyncio.to_thread(get_public_ip)
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

    async def notify_ip_change(self, new_ip: str) -> bool:
        """
        Called by the IP monitor task when IP changes.
        Sends to the configured channel and mentions the subscriber role.

        Returns True when all configured destinations accepted the message. The
        monitor logs False as a best-effort notification failure after committing
        the authoritative DNS/IP state.
        """

        logging.info("IP changed to %s — sending notification.", new_ip)

        msg_body = _format_ip_message(new_ip, is_change=True)

        notifier = Notifier(self.bot)

        configured = 0
        failed = 0
        for guild in self.bot.guilds:
            per_guild_channel_id = get_notification_channel_id_for_guild(guild.id)
            if not per_guild_channel_id:
                continue
            configured += 1

            role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
            role = discord.utils.get(guild.roles, name=role_name)
            mention_prefix = ""
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

            delivered = await notifier.send_to_channel(
                per_guild_channel_id, mention_prefix + msg_body
            )
            if not delivered:
                failed += 1

        if configured:
            if failed:
                logging.warning(
                    "IP change notification failed for %s of %s configured channel(s).",
                    failed,
                    configured,
                )
                return False
            return True

        # Backward compatibility for older single-channel config.
        channel_id = self.bot.state.channel_id  # type: ignore[attr-defined]
        if not channel_id:
            logging.warning(
                "No per-guild notification channel or legacy channel_id configured; "
                "cannot send IP change alert."
            )
            # There is no configured notification to retry. The monitor may
            # safely persist the new baseline without sending a message.
            return True

        mention_prefix = ""
        for guild in self.bot.guilds:
            role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
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

        return await notifier.send_to_channel(channel_id, mention_prefix + msg_body)
