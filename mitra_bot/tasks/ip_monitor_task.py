# mitra_bot/tasks/ip_monitor_task.py
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import tasks

from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.storage.cache_store import load_ip


class IPMonitorTask:
    """
    Background loop that checks public IP and notifies subscribers on change.
    """

    def __init__(self, bot: discord.Bot, *, interval_seconds: int = 60) -> None:
        self.bot = bot
        self.interval_seconds = interval_seconds

        self._last_ip: Optional[str] = None

        # bind loop
        self.loop.change_interval(seconds=self.interval_seconds)

    async def start(self) -> None:
        # load last ip from cache
        self._last_ip = await load_ip()
        if not self._last_ip:
            logging.info("No cached IP found.")
        else:
            logging.info("Cached IP loaded: %s", self._last_ip)

        self.loop.start()

    @tasks.loop(seconds=60)
    async def loop(self) -> None:
        ip = get_public_ip()
        if not ip:
            return

        if self._last_ip is None:
            self._last_ip = ip
            return

        if ip == self._last_ip:
            return

        logging.info("Public IP changed: %s -> %s", self._last_ip, ip)
        self._last_ip = ip

        # Find the IPCog and call its notifier
        cog = self.bot.get_cog("IPCog")
        if cog is None:
            logging.warning("IPCog not loaded; cannot notify subscribers.")
            return

        try:
            await cog.notify_ip_change(ip)  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to notify IP change.")

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()
