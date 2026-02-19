# mitra_bot/tasks/ups_monitor_task.py
from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from mitra_bot.services.notifier import Notifier
from mitra_bot.storage.cache_store import read_cache_with_defaults


class UPSMonitorTask:
    def __init__(self, bot: discord.Bot, *, poll_seconds: int = 30) -> None:
        self.bot = bot
        self.poll_seconds = poll_seconds
        self.loop.change_interval(seconds=self.poll_seconds)

    async def start(self) -> None:
        self.loop.start()

    @tasks.loop(seconds=30)
    async def loop(self) -> None:
        cfg = read_cache_with_defaults()
        ups_cfg = cfg.get("ups", {}) if isinstance(cfg.get("ups"), dict) else {}

        if not bool(ups_cfg.get("enabled", True)):
            return

        cog = self.bot.get_cog("UPSCog")
        if cog is None:
            logging.warning("UPSCog not loaded; cannot poll UPS.")
            return

        try:
            event = cog.poll_for_event()  # type: ignore[attr-defined]
        except Exception:
            logging.exception("UPS poll failed.")
            return

        if not event:
            return

        await self._dispatch_event(event.message)

    async def _dispatch_event(self, message: str) -> None:
        notifier = Notifier(self.bot)

        channel_id = getattr(getattr(self.bot, "state", None), "channel_id", None)
        subscribers = getattr(getattr(self.bot, "state", None), "subscribers", set())

        await notifier.notify(
            channel_id=channel_id,
            subscriber_ids=subscribers,
            message=message,
            send_channel=True,
            send_dms=True,
        )

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()
