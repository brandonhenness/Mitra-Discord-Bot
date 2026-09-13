# mitra_bot/services/notifier.py
from __future__ import annotations

import logging
from typing import Iterable, Optional

import discord

from mitra_bot.services.peer_service import Notification
from mitra_bot.discord_app.access import infrastructure_channel


class Notifier:
    """
    Centralized notification helper.

    Supports:
      - posting to a configured channel
      - DMing a list of subscriber user IDs
    """

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    async def send_to_channel(self, channel_id: Optional[int], message: str) -> bool:
        if not channel_id:
            return False

        mesh = getattr(self.bot, "peer_service", None)
        if mesh is not None:
            return await mesh.notify(Notification(channel_id=int(channel_id), message=message))

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))

            if isinstance(channel, (discord.TextChannel, discord.Thread)) and infrastructure_channel(self.bot, channel):
                await channel.send(message)
                return True
            else:
                logging.warning("Channel %s is not a text channel/thread.", channel_id)

        except Exception:
            logging.exception("Failed to send message to channel %s", channel_id)
        return False

    async def dm_subscribers(self, subscriber_ids: Iterable[int], message: str) -> None:
        # Legacy user IDs carry no guild ownership or current authorization.
        # Operational alerts are delivered only to authorized guild channels.
        return

    async def notify(
        self,
        *,
        channel_id: Optional[int],
        subscriber_ids: Iterable[int],
        message: str,
        send_channel: bool = True,
        send_dms: bool = True,
    ) -> None:
        if send_channel:
            await self.send_to_channel(channel_id, message)
        if send_dms:
            await self.dm_subscribers(subscriber_ids, message)
