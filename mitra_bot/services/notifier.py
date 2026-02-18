# mitra_bot/services/notifier.py
from __future__ import annotations

import logging
from typing import Iterable, Optional

import discord


class Notifier:
    """
    Centralized notification helper.

    Supports:
      - posting to a configured channel
      - DMing a list of subscriber user IDs
    """

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    async def send_to_channel(self, channel_id: Optional[int], message: str) -> None:
        if not channel_id:
            return

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))

            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await channel.send(message)
            else:
                logging.warning("Channel %s is not a text channel/thread.", channel_id)

        except Exception:
            logging.exception("Failed to send message to channel %s", channel_id)

    async def dm_subscribers(self, subscriber_ids: Iterable[int], message: str) -> None:
        for user_id in list(subscriber_ids):
            try:
                user = await self.bot.fetch_user(int(user_id))
                await user.send(message)
            except Exception:
                # Common case: user has DMs closed or blocked the bot
                logging.debug("Failed to DM subscriber %s", user_id)

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
