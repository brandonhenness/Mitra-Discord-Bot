from __future__ import annotations

import platform
import time

import discord
from discord.ext import commands

from mitra_bot import __version__


class AboutCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._started_at_epoch = int(time.time())

    @discord.slash_command(name="about", description="Show bot info and runtime details.")
    async def about(self, ctx: discord.ApplicationContext) -> None:
        now = int(time.time())
        embed = discord.Embed(
            title="Mitra Bot",
            description="Operations helper bot for monitoring, power controls, and utility workflows.",
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
        embed.add_field(name="Servers", value=f"`{len(self.bot.guilds)}`", inline=True)
        embed.add_field(name="Now", value=f"<t:{now}:F>", inline=True)
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AboutCog(bot))
