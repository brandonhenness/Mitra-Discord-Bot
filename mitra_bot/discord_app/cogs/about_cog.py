from mitra_bot.discord_app.message_style import embed as styled_embed
from mitra_bot.discord_app.message_style import notice

import platform
import time

import discord
from discord.ext import commands

from mitra_bot import __version__
from mitra_bot.discord_app.node_commands import selected_nodes, read_nodes
from mitra_bot.services.peer_service import PeerError
from mitra_bot.discord_app.access import infrastructure_guild

POLICY_LINKS = (
    "[Terms of Service](https://github.com/brandonhenness/Mitra-Discord-Bot/blob/main/TERMS_OF_SERVICE.md) · "
    "[Privacy Policy](https://github.com/brandonhenness/Mitra-Discord-Bot/blob/main/PRIVACY_POLICY.md)"
)


class AboutCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._started_at_epoch = int(time.time())

    @discord.slash_command(name="about", description="Show bot info and runtime details.")
    async def about(self, ctx: discord.ApplicationContext,
                    server: str = discord.Option(str, description="Server ID or all (default: all servers)", required=False, default=None)) -> None:
        if not infrastructure_guild(self.bot, ctx.guild):
            await ctx.respond(embed=styled_embed(
                title="Mitra Bot",
                description="Shared to-do lists and task threads for your Discord server.\n"
                    "Use `/todo list_create` to get started (Manage Channels required). "
                    "Lists and task history belong to this server.\n\n" + POLICY_LINKS,
            ), ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        if getattr(self.bot, "peer_service", None):
            try:
                nodes = selected_nodes(self.bot, server, default_all=True)
            except PeerError as exc:
                await ctx.respond(notice('Bot information unavailable', str(exc), tone='error'), ephemeral=True)
                return
            results = await read_nodes(self.bot, nodes, "node_info")
            for start in range(0, len(results), 8):
                embed = styled_embed(title="Mitra Bot · network", color=discord.Color.blurple())
                for node, data in results[start:start+8]:
                    value = "Unavailable: check connectivity and node version."
                    if data and data.get("error"):
                        value = "Unavailable: " + data["error"]
                    elif data:
                        health = data.get("health", {})
                        uptime = health.get("process_uptime_seconds", 0)
                        value = (f"Version `{data['version']}` · Python `{data['python']}` · Py-Cord `{data['pycord']}`\n"
                                 f"Process uptime: {uptime//3600}h {uptime//60%60}m\n"
                                 f"Discord: {'connected' if health.get('discord_connected') else 'disconnected'}\n"
                                 f"Discord servers: {data['discord_servers']}")
                    embed.add_field(name=node, value=value, inline=False)
                embed.description = POLICY_LINKS
                await ctx.respond(embed=embed, ephemeral=True)
            return
        try:
            selected_nodes(self.bot, server, default_all=True)
        except PeerError as exc:
            await ctx.respond(notice('Bot information unavailable', str(exc), tone='error'), ephemeral=True)
            return
        now = int(time.time())
        embed = styled_embed(
            title="Mitra Bot",
            description="Operations helper bot for monitoring, power controls, and utility workflows.\n\n" + POLICY_LINKS,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Version", value=f"`{__version__}`", inline=True)
        embed.add_field(name="Python", value=f"`{platform.python_version()}`", inline=True)
        embed.add_field(name="Py-Cord", value=f"`{discord.__version__}`", inline=True)
        embed.add_field(
            name="Uptime",
            value=f"<t:{self._started_at_epoch}:R>",
            inline=True,
        )
        embed.add_field(name="Discord servers", value=f"`{len(self.bot.guilds)}`", inline=True)
        embed.add_field(name="Now", value=f"<t:{now}:F>", inline=True)
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AboutCog(bot))
