from mitra_bot.discord_app.message_style import notice
from mitra_bot.discord_app.message_style import pages
# mitra_bot/discord_app/cogs/ip_cog.py

import logging

import discord
from discord.ext import commands

from mitra_bot.services.notifier import Notifier
from mitra_bot.services.peer_service import Notification
from mitra_bot.services.alert_roles import shared_role
from mitra_bot.discord_app.node_commands import selected_nodes, read_nodes
from mitra_bot.services.peer_service import PeerError
from mitra_bot.storage.storage_store import (
    get_notification_channel_id_for_guild,
    get_notification_channel_map,
)


def _format_ip_message(ip: str, *, is_change: bool, server: str | None = None) -> str:
    title = (f"🌐 {server}'s public IP address changed" if server else "🌐 Public IP address changed") if is_change else "🌐 Current public IP address"
    label = "New public IP address" if is_change else "Public IP address"
    return f"### {title}\n\n**{label}**\n```\n{ip}\n```"


class IPCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    ip = discord.SlashCommandGroup(
        name="ip",
        description="Public IP monitoring commands",
    )

    @ip.command(name="status", description="Show current public IP")
    async def status(self, ctx: discord.ApplicationContext,
                     server: str = discord.Option(str, description="Server ID or all (default: all servers)", required=False, default=None)):
        await ctx.defer(ephemeral=True)
        try:
            nodes = selected_nodes(self.bot, server, default_all=True)
        except PeerError as exc:
            await ctx.respond(notice('IP address unavailable', str(exc), tone='error'), ephemeral=True)
            return
        results = await read_nodes(self.bot, nodes, "public_ip")
        lines = [f"**{node}**\n" + (f"```\n{data['ip']}\n```" if data and data.get("ip") else
                 "⚠️ Unavailable — " + discord.utils.escape_markdown(str(data.get("error", "Public-IP lookup failed.") if data else "Public-IP lookup failed.")[:180])) for node, data in results]
        for message in pages("🌐 Public IP addresses", lines):
            await ctx.respond(message, ephemeral=True,
                              allowed_mentions=discord.AllowedMentions.none())

    async def notify_ip_change(self, new_ip: str) -> bool:
        """
        Called by the IP monitor task when IP changes.
        Sends to the configured channel and mentions the subscriber role.

        Returns True when all configured destinations accepted the message. The
        monitor logs False as a best-effort notification failure after committing
        the authoritative DNS/IP state.
        """

        logging.info("IP changed to %s — sending notification.", new_ip)

        mesh = getattr(self.bot, "peer_service", None)
        msg_body = _format_ip_message(new_ip, is_change=True, server=mesh.config.node_id if mesh else None)

        notifier = Notifier(self.bot)

        # Every active instance reports its own IP events using local destinations.
        if mesh is not None:
            destinations = dict(get_notification_channel_map())
            for setting in mesh.monitor.store.settings():
                if setting["subject"] == "*" and setting.get("role"):
                    destinations.pop(setting["guild"], None)
                    if setting["enabled"] and setting.get("channel"):
                        destinations[setting["guild"]] = setting["channel"]
            channels = set(destinations.values())
            if not destinations and not any(s["subject"] == "*" for s in mesh.monitor.store.settings()) and self.bot.state.channel_id:
                channels.add(self.bot.state.channel_id)
            results = [await mesh.notify(Notification(channel_id=int(channel_id), message=msg_body,
                                                       mention_ip_subscribers=True)) for channel_id in channels]
            return all(results)

        configured = 0
        failed = 0
        for guild in self.bot.guilds:
            per_guild_channel_id = get_notification_channel_id_for_guild(guild.id)
            if not per_guild_channel_id:
                continue
            configured += 1

            role_name = self.bot.state.ip_subscriber_role_name  # type: ignore[attr-defined]
            role = shared_role(guild) or discord.utils.get(guild.roles, name=role_name)
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
            role = shared_role(guild) or discord.utils.get(guild.roles, name=role_name)
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
