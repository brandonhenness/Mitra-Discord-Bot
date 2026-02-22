from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from mitra_bot.storage.cache_store import get_updater_config


class UpdateMonitorTask:
    def __init__(self, bot: discord.Bot, *, interval_seconds: int = 21600) -> None:
        self.bot = bot
        self.interval_seconds = max(60, int(interval_seconds))
        self.loop.change_interval(seconds=self.interval_seconds)

    async def start(self) -> None:
        self.loop.start()

    @tasks.loop(seconds=21600)
    async def loop(self) -> None:
        cfg = get_updater_config()
        enabled = bool(cfg.get("enabled", True))
        desired_interval = max(60, int(cfg.get("check_interval_seconds", 21600)))
        if desired_interval != self.interval_seconds:
            self.interval_seconds = desired_interval
            self.loop.change_interval(seconds=self.interval_seconds)

        if not enabled:
            return

        cog = self.bot.get_cog("UpdateCog")
        if cog is None:
            logging.warning("UpdateCog not loaded; cannot run periodic update checks.")
            return

        try:
            await cog.notify_if_update_available(source="periodic")  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Periodic update check failed.")

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()
