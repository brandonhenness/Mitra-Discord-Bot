# mitra_bot/discord_app/cogs/power_cog.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.services.power_service import execute_power_action
from mitra_bot.storage.cache_store import (
    clear_power_restart_notice,
    set_power_restart_notice,
)


class PowerActionView(discord.ui.View):
    def __init__(
        self,
        *,
        action: str,
        delay_seconds: int,
        force: bool,
        requester_id: int,
        channel_id: int | None,
    ) -> None:
        super().__init__(timeout=None)
        self.action = action
        self.delay_seconds = delay_seconds
        self.force = force
        self.requester_id = requester_id
        self.channel_id = channel_id
        self.confirmed = False
        self.mode = "immediate" if delay_seconds == 0 else "delayed"
        self.requested_at_epoch = int(datetime.now(timezone.utc).timestamp())
        self.confirmed_by_id: int | None = None
        self.confirmed_at_epoch: int | None = None
        self.canceled_by_id: int | None = None
        self.canceled_at_epoch: int | None = None
        self.message_id: int | None = None
        self.guild_id: int | None = None

    def _is_admin_user(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            return False

        role_name = getattr(
            getattr(interaction.client, "state", None), "admin_role_name", None
        )
        if not role_name:
            return False

        return any(getattr(role, "name", None) == role_name for role in user.roles)

    def _build_embed(self, *, state: str) -> discord.Embed:
        if state == "pending":
            color = discord.Color.orange()
            title = f"Confirm {self.action.title()}"
            summary = "This will modify server power state."
        elif state == "confirmed":
            color = discord.Color.gold()
            title = f"{self.action.title()} Confirmed"
            summary = "Action has been queued."
        else:
            color = discord.Color.green()
            title = f"{self.action.title()} Canceled"
            summary = "Pending power action has been aborted."

        embed = discord.Embed(
            title=title,
            description=summary,
            color=color,
        )
        embed.add_field(name="Action", value=f"`{self.action}`", inline=True)
        embed.add_field(name="Mode", value=f"`{self.mode}`", inline=True)
        embed.add_field(name="Delay", value=f"`{self.delay_seconds}` sec", inline=True)
        embed.add_field(name="Force", value=f"`{self.force}`", inline=True)

        embed.add_field(name="Requested By", value=f"<@{self.requester_id}>", inline=True)
        embed.add_field(
            name="Requested At", value=f"<t:{self.requested_at_epoch}:F>", inline=True
        )

        if state == "confirmed" and self.confirmed_at_epoch is not None:
            embed.add_field(
                name="Confirmed By",
                value=f"<@{self.confirmed_by_id}>" if self.confirmed_by_id else "Unknown",
                inline=True,
            )
            embed.add_field(
                name="Confirmed At",
                value=f"<t:{self.confirmed_at_epoch}:F>",
                inline=True,
            )
            eta_epoch = self.confirmed_at_epoch + self.delay_seconds
            embed.add_field(name="Scheduled For", value=f"<t:{eta_epoch}:F>", inline=True)
            embed.add_field(name="Time Remaining", value=f"<t:{eta_epoch}:R>", inline=True)
            embed.set_footer(
                text="Only members with the admin role can use these buttons. Use Cancel to abort while pending."
            )
        elif state == "canceled" and self.canceled_at_epoch is not None:
            embed.add_field(
                name="Canceled By",
                value=f"<@{self.canceled_by_id}>" if self.canceled_by_id else "Unknown",
                inline=True,
            )
            embed.add_field(
                name="Canceled At",
                value=f"<t:{self.canceled_at_epoch}:F>",
                inline=True,
            )
        elif state == "pending":
            embed.set_footer(text="Only members with the admin role can use these buttons.")
        return embed

    async def _run_action(self) -> None:
        try:
            logging.warning(
                "Power %s requested by user_id=%s delay=%s force=%s",
                self.action,
                self.requester_id,
                self.delay_seconds,
                self.force,
            )
            await asyncio.to_thread(
                execute_power_action,
                self.action,
                delay_seconds=self.delay_seconds,
                force=self.force,
            )
        except Exception:
            if self.action == "restart":
                clear_power_restart_notice()
            logging.exception("Power %s failed.", self.action)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if not self._is_admin_user(interaction):
            await interaction.response.send_message(
                "Only members with the admin role can confirm this action.",
                ephemeral=True,
            )
            return

        if self.confirmed:
            await interaction.response.send_message(
                "This action has already been confirmed.", ephemeral=True
            )
            return

        self.confirmed = True
        self.confirmed_by_id = getattr(interaction.user, "id", None)
        self.confirmed_at_epoch = int(datetime.now(timezone.utc).timestamp())
        self.message_id = getattr(interaction.message, "id", None)
        self.guild_id = getattr(interaction.guild, "id", None)

        if self.action == "restart":
            set_power_restart_notice(
                {
                    "action": "restart",
                    "channel_id": self.channel_id,
                    "guild_id": self.guild_id,
                    "message_id": self.message_id,
                    "requested_by_user_id": self.requester_id,
                    "requested_at_epoch": self.requested_at_epoch,
                    "confirmed_by_user_id": self.confirmed_by_id,
                    "confirmed_at_epoch": self.confirmed_at_epoch,
                    "delay_seconds": self.delay_seconds,
                    "force": self.force,
                }
            )

        button.label = "Confirmed"
        button.disabled = True
        await interaction.response.edit_message(
            content=None,
            embed=self._build_embed(state="confirmed"),
            view=self,
        )
        asyncio.create_task(self._run_action())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if not self._is_admin_user(interaction):
            await interaction.response.send_message(
                "Only members with the admin role can cancel this action.",
                ephemeral=True,
            )
            return

        try:
            self.canceled_by_id = getattr(interaction.user, "id", None)
            self.canceled_at_epoch = int(datetime.now(timezone.utc).timestamp())
            await asyncio.to_thread(
                execute_power_action,
                "cancel",
                delay_seconds=0,
                force=False,
            )
            logging.warning(
                "Power cancel executed by user_id=%s via %s view",
                self.requester_id,
                self.action,
            )
            clear_power_restart_notice()
            await interaction.response.edit_message(
                content=None,
                embed=self._build_embed(state="canceled"),
                view=None,
            )
        except Exception as ex:
            logging.exception("Power cancel failed from view.")
            await interaction.response.send_message(
                f"Cancel failed:\n```{ex}```", ephemeral=True
            )
            return

        self.stop()


class PowerCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    power = discord.SlashCommandGroup(
        name="power",
        description="Admin power controls",
    )

    # -----------------------------
    # /power restart
    # -----------------------------

    @power.command(
        name="restart", description="Restart the server running this bot (admins only)."
    )
    async def restart(
        self,
        ctx: discord.ApplicationContext,
        delay_seconds: int = discord.Option(
            int,
            description="Delay in seconds. Use 0 for immediate restart.",
            required=False,
            default=0,
            min_value=0,
            max_value=86400,
        ),
        force: bool = discord.Option(
            bool,
            description="Force close apps without warning (Windows only).",
            required=False,
            default=False,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        view = PowerActionView(
            action="restart",
            delay_seconds=delay_seconds,
            force=force,
            requester_id=getattr(ctx.user, "id", 0),
            channel_id=ctx.channel_id,
        )
        embed = view._build_embed(state="pending")
        await ctx.respond(
            embed=embed,
            view=view,
        )

    # -----------------------------
    # /power shutdown
    # -----------------------------

    @power.command(
        name="shutdown",
        description="Shut down (power off) the server running this bot (admins only).",
    )
    async def shutdown(
        self,
        ctx: discord.ApplicationContext,
        delay_seconds: int = discord.Option(
            int,
            description="Delay in seconds. Use 0 for immediate shutdown.",
            required=False,
            default=0,
            min_value=0,
            max_value=86400,
        ),
        force: bool = discord.Option(
            bool,
            description="Force close apps without warning (Windows only).",
            required=False,
            default=False,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        view = PowerActionView(
            action="shutdown",
            delay_seconds=delay_seconds,
            force=force,
            requester_id=getattr(ctx.user, "id", 0),
            channel_id=ctx.channel_id,
        )
        embed = view._build_embed(state="pending")
        await ctx.respond(
            embed=embed,
            view=view,
        )

    # -----------------------------
    # /power cancel
    # -----------------------------

    @power.command(
        name="cancel", description="Cancel a pending shutdown/restart (admins only)."
    )
    async def cancel(
        self,
        ctx: discord.ApplicationContext,
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.respond(
            "Attempting to cancel any pending shutdown/restart.", ephemeral=True
        )

        try:
            await asyncio.to_thread(
                execute_power_action,
                "cancel",
                delay_seconds=0,
                force=False,
            )
            logging.warning(
                "Power cancel executed by user_id=%s",
                getattr(ctx.user, "id", "unknown"),
            )
            clear_power_restart_notice()
        except Exception as ex:
            logging.exception("Power cancel failed.")
            await ctx.followup.send(f"Cancel failed:\n```{ex}```", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PowerCog(bot))
