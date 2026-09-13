# mitra_bot/discord_app/bot_factory.py
from __future__ import annotations
from mitra_bot.discord_app.message_style import notice

import logging
import os
import asyncio
import random
from dataclasses import dataclass
from typing import Optional

import discord

from mitra_bot.discord_app.interaction_routing import ClaimedContext, claim_interaction, command_route, response_delay
from mitra_bot.discord_app.access import PUBLIC_COMMANDS, command_allowed


class MitraBot(discord.Bot):
    """All nodes connect as one application; only a successful claimant runs a command."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.peer_service = None
        self.gateway_connected = False

    @property
    def owns_application_state(self):
        return self.peer_service is None or self.peer_service.is_state_owner

    async def before_identify_hook(self, shard_id, *, initial=False):
        if self.peer_service:
            nodes = sorted([self.peer_service.config.node_id, *self.peer_service.peers])
            # Spread simultaneous starts over Discord's per-shard IDENTIFY windows.
            await asyncio.sleep(5.5 * nodes.index(self.peer_service.config.node_id) + random.uniform(0, 1))
        await super().before_identify_hook(shard_id, initial=initial)

    async def get_application_context(self, interaction, cls=None):
        return await super().get_application_context(
            interaction, cls or (ClaimedContext if self.peer_service else discord.ApplicationContext),
        )

    async def process_application_commands(self, interaction, auto_sync=None):
        if not command_allowed(self, interaction.guild, (interaction.data or {}).get("name", "")):
            if interaction.type == discord.InteractionType.auto_complete:
                await interaction.response.send_autocomplete_result([])
            elif await claim_interaction(interaction, delay=0, ephemeral=True):
                await interaction.followup.send(
                    notice('Command unavailable', 'This command is not available in this Discord server.', tone='warning'),
                    ephemeral=True,
                )
            return
        mesh = self.peer_service
        if mesh is None:
            return await super().process_application_commands(interaction, auto_sync)
        if interaction.type != discord.InteractionType.application_command:
            # No autocomplete providers currently registered. Keep legacy behavior on the state owner.
            if self.owns_application_state:
                return await super().process_application_commands(interaction, False)
            return
        preferred, fallback = command_route(interaction.data or {}, mesh)
        if not await claim_interaction(
            interaction, delay=response_delay(mesh, preferred, interaction.id), ephemeral=True,
        ):
            return
        if not fallback and not self.owns_application_state:
            await interaction.followup.send(
                notice('Service temporarily unavailable', "The service that stores these lists and settings is unavailable; no change was made. Please try again later.", tone='info'),
                ephemeral=True,
            )
            return
        await super().process_application_commands(interaction, False)

    async def on_interaction(self, interaction):
        if self.peer_service and str((interaction.data or {}).get("custom_id", "")).startswith("mitra-health:"):
            from mitra_bot.discord_app.peer_dashboard import handle_dashboard_component
            await handle_dashboard_component(self, interaction)
            return
        if self.peer_service and str((interaction.data or {}).get("custom_id", "")).startswith("mitra-power:"):
            from mitra_bot.discord_app.peer_power import handle_power_component
            await handle_power_component(self, interaction)
            return
        await super().on_interaction(interaction)

    async def _run_event(self, coro, event_name, *args, **kwargs):
        # To-Do's listeners and periodic work use an unreplicated database.
        if type(getattr(coro, "__self__", None)).__name__ == "TodoCog" and not self.owns_application_state:
            return
        await super()._run_event(coro, event_name, *args, **kwargs)


@dataclass
class AppState:
    channel_id: Optional[int]
    admin_role_name: str
    ip_subscriber_role_name: str
    infrastructure_guild_ids: tuple[int, ...] = ()


def create_bot(*, state: AppState) -> discord.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = os.getenv("MITRA_ENABLE_MEMBERS_INTENT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    intents.message_content = False

    bot = MitraBot(intents=intents)
    bot.state = state  # type: ignore[attr-defined]

    _register_cogs(bot)
    for command in bot.pending_application_commands:
        if hasattr(discord, "InteractionContextType"):
            command.contexts = {discord.InteractionContextType.guild}
            command.integration_types = {discord.IntegrationType.guild_install}
        else:
            command.guild_only = True
        if command.name not in PUBLIC_COMMANDS:
            # [] registers nowhere; None would expose the command globally.
            command.guild_ids = list(state.infrastructure_guild_ids)
    return bot


def _register_cogs(bot: discord.Bot) -> None:
    try:
        from mitra_bot.discord_app.cogs.about_cog import AboutCog
        from mitra_bot.discord_app.cogs.ip_cog import IPCog
        from mitra_bot.discord_app.cogs.power_cog import PowerCog
        from mitra_bot.discord_app.cogs.settings_cog import SettingsCog
        from mitra_bot.discord_app.cogs.todo_cog import TodoCog
        from mitra_bot.discord_app.cogs.update_cog import UpdateCog
        from mitra_bot.discord_app.cogs.ups_cog import UPSCog
        from mitra_bot.discord_app.cogs.servers_cog import ServersCog

        bot.add_cog(AboutCog(bot))  # type: ignore[arg-type]
        bot.add_cog(IPCog(bot))  # type: ignore[arg-type]
        bot.add_cog(PowerCog(bot))  # type: ignore[arg-type]
        bot.add_cog(SettingsCog(bot))  # type: ignore[arg-type]
        bot.add_cog(TodoCog(bot))  # type: ignore[arg-type]
        bot.add_cog(UpdateCog(bot))  # type: ignore[arg-type]
        bot.add_cog(UPSCog(bot))  # type: ignore[arg-type]
        bot.add_cog(ServersCog(bot))

        logging.info("Cogs registered.")
    except Exception:
        logging.exception("Cog registration failed.")
