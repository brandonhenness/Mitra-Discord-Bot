from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.services.update_service import (
    InstallResult,
    ReleaseInfo,
    UpdateCheckResult,
    check_latest_release,
    install_release,
    spawn_replacement_process,
)
from mitra_bot.storage.cache_store import (
    get_notification_channel_map,
    get_updater_config,
    set_updater_config,
)


def _trim(text: str, limit: int = 900) -> str:
    value = (text or "").strip()
    if not value:
        return "No release notes."
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


class UpdatePromptView(discord.ui.View):
    def __init__(self, cog: "UpdateCog", release: ReleaseInfo, *, source: str) -> None:
        super().__init__(timeout=3600)
        self.cog = cog
        self.release = release
        self.source = source

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

    @discord.ui.button(label="Install Update", style=discord.ButtonStyle.danger)
    async def install_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if not self._is_admin_user(interaction):
            await interaction.response.send_message(
                "Only members with the admin role can install updates.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await self.cog.install_release_with_feedback(
            release=self.release,
            message=interaction.message,
            source=self.source,
        )
        self.stop()

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if not self._is_admin_user(interaction):
            await interaction.response.send_message(
                "Only members with the admin role can dismiss updates.",
                ephemeral=True,
            )
            return

        set_updater_config(
            {
                "last_notified_version": self.release.version,
                "pending_version": self.release.version,
                "pending_release_url": self.release.html_url,
                "pending_notes": self.release.notes,
                "pending_notified_epoch": int(time.time()),
            }
        )
        await interaction.response.edit_message(
            embed=self.cog.build_embed(
                title="Update Available (Dismissed)",
                check=None,
                release=self.release,
                color=discord.Color.dark_grey(),
                description="Update was dismissed. Use `/update install` to apply later.",
            ),
            view=None,
        )
        self.stop()


class UpdateCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._install_lock = asyncio.Lock()

    update = discord.SlashCommandGroup(
        name="update",
        description="Bot update commands",
    )

    def build_embed(
        self,
        *,
        title: str,
        check: Optional[UpdateCheckResult],
        release: Optional[ReleaseInfo],
        color: discord.Color,
        description: Optional[str] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description or "",
            color=color,
        )
        if check is not None:
            embed.add_field(
                name="Current",
                value=f"`{check.current_version}`",
                inline=True,
            )
            embed.add_field(
                name="Latest",
                value=f"`{check.latest_version or 'unknown'}`",
                inline=True,
            )
            if check.repo:
                embed.add_field(
                    name="Repository", value=f"`{check.repo}`", inline=False
                )
            if check.error:
                embed.add_field(
                    name="Error", value=_trim(check.error, 300), inline=False
                )

        if release is not None:
            embed.add_field(name="Version", value=f"`{release.version}`", inline=True)
            if release.html_url:
                embed.add_field(
                    name="Release",
                    value=f"[Open Release]({release.html_url})",
                    inline=True,
                )
            embed.add_field(
                name="Notes",
                value=_trim(release.notes),
                inline=False,
            )
        return embed

    async def _find_announce_channel(self) -> Optional[discord.abc.Messageable]:
        channel_map = get_notification_channel_map()
        for guild in self.bot.guilds:
            per_guild = channel_map.get(guild.id)
            if not per_guild:
                continue
            ch = self.bot.get_channel(per_guild)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(per_guild)
                except Exception:
                    continue
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch

        legacy_channel_id = getattr(
            getattr(self.bot, "state", None), "channel_id", None
        )
        if legacy_channel_id:
            ch = self.bot.get_channel(int(legacy_channel_id))
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(int(legacy_channel_id))
                except Exception:
                    return None
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch
        return None

    async def notify_if_update_available(self, *, source: str) -> None:
        cfg = get_updater_config()
        if not bool(cfg.get("enabled", True)):
            return

        check = await asyncio.to_thread(check_latest_release)
        if check.error:
            logging.warning("Update check failed: %s", check.error)
            return
        if not check.available or check.release is None:
            set_updater_config(
                {
                    "pending_version": None,
                    "pending_release_url": None,
                    "pending_notes": None,
                    "pending_notified_epoch": None,
                }
            )
            return

        current_cfg = get_updater_config()
        if current_cfg.get("last_notified_version") == check.release.version:
            return

        set_updater_config(
            {
                "pending_version": check.release.version,
                "pending_release_url": check.release.html_url,
                "pending_notes": check.release.notes,
                "pending_notified_epoch": int(time.time()),
            }
        )

        channel = await self._find_announce_channel()
        if channel is None:
            logging.info(
                "Update is available but no notification channel is configured."
            )
            return

        embed = self.build_embed(
            title="Bot Update Available",
            check=check,
            release=check.release,
            color=discord.Color.orange(),
            description="A new release is available. Admins can install it using the button below.",
        )
        await channel.send(
            embed=embed,
            view=UpdatePromptView(self, check.release, source=source),
        )
        set_updater_config({"last_notified_version": check.release.version})

    async def _restart_after_update(self, *, origin_message: discord.Message) -> None:
        try:
            spawn_replacement_process()
        except Exception as exc:
            await origin_message.channel.send(
                f"Update installed, but failed to spawn replacement process: `{exc}`"
            )
            return

        await origin_message.channel.send(
            "Update installed. Restarting bot process now."
        )
        setattr(self.bot, "_mitra_restart_requested", True)
        try:
            await self.bot.close()
        except Exception:
            logging.exception("Bot close raised during updater restart.")

    async def install_release_with_feedback(
        self,
        *,
        release: ReleaseInfo,
        message: Optional[discord.Message],
        source: str,
    ) -> None:
        if message is None:
            return
        if self._install_lock.locked():
            await message.reply("An update install is already in progress.")
            return

        async with self._install_lock:
            installing_embed = self.build_embed(
                title="Installing Update",
                check=None,
                release=release,
                color=discord.Color.gold(),
                description=f"Starting install (triggered from `{source}`).",
            )
            await message.edit(embed=installing_embed, view=None)

            result: InstallResult = await asyncio.to_thread(install_release, release)
            if not result.ok:
                failed_embed = self.build_embed(
                    title="Update Failed",
                    check=None,
                    release=release,
                    color=discord.Color.red(),
                    description=f"Install failed: {_trim(result.error or 'unknown error', 500)}",
                )
                await message.edit(embed=failed_embed, view=None)
                return

            success_embed = self.build_embed(
                title="Update Installed",
                check=None,
                release=release,
                color=discord.Color.green(),
                description=(
                    f"Release `{result.version or release.version}` installed successfully. "
                    "Restarting now."
                ),
            )
            await message.edit(embed=success_embed, view=None)
            await self._restart_after_update(origin_message=message)

    async def _send_latest_changelog(
        self,
        *,
        ctx: discord.ApplicationContext,
        force_name: Optional[str] = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        check = await asyncio.to_thread(check_latest_release)
        if check.error or check.release is None:
            await ctx.followup.send(
                embed=self.build_embed(
                    title="Changelog Unavailable",
                    check=check,
                    release=None,
                    color=discord.Color.red(),
                    description="Could not load the latest release changelog.",
                ),
                ephemeral=True,
            )
            return

        title = "Latest Changelog"
        if force_name:
            title = force_name
        await ctx.followup.send(
            embed=self.build_embed(
                title=title,
                check=check,
                release=check.release,
                color=discord.Color.blurple(),
                description=(
                    "Showing notes for the latest GitHub release."
                    if check.available
                    else "You are already on this release. Showing its notes."
                ),
            ),
            ephemeral=True,
        )

    @update.command(
        name="check",
        description="Check GitHub for a new bot release (admins only).",
    )
    async def check(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.defer()
        check = await asyncio.to_thread(check_latest_release)
        if check.error:
            await ctx.followup.send(
                embed=self.build_embed(
                    title="Update Check Failed",
                    check=check,
                    release=None,
                    color=discord.Color.red(),
                    description="The update check did not complete successfully.",
                ),
                ephemeral=True,
            )
            return

        if not check.available or check.release is None:
            set_updater_config(
                {
                    "pending_version": None,
                    "pending_release_url": None,
                    "pending_notes": None,
                    "pending_notified_epoch": None,
                }
            )
            await ctx.followup.send(
                embed=self.build_embed(
                    title="No Update Available",
                    check=check,
                    release=None,
                    color=discord.Color.green(),
                    description="You are already on the latest release.",
                ),
                ephemeral=True,
            )
            return

        set_updater_config(
            {
                "pending_version": check.release.version,
                "pending_release_url": check.release.html_url,
                "pending_notes": check.release.notes,
                "pending_notified_epoch": int(time.time()),
                "last_notified_version": check.release.version,
            }
        )

        await ctx.followup.send(
            embed=self.build_embed(
                title="Update Available",
                check=check,
                release=check.release,
                color=discord.Color.orange(),
                description="Choose Install to apply now or Dismiss to keep running this version.",
            ),
            view=UpdatePromptView(self, check.release, source="manual-check"),
        )

    @update.command(
        name="install",
        description="Install the latest bot release now (admins only).",
    )
    async def install(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.defer()
        check = await asyncio.to_thread(check_latest_release)
        if check.error or not check.available or check.release is None:
            await ctx.followup.send(
                "No installable update found. Run `/update check` for details.",
                ephemeral=True,
            )
            return

        set_updater_config(
            {
                "pending_version": check.release.version,
                "pending_release_url": check.release.html_url,
                "pending_notes": check.release.notes,
                "pending_notified_epoch": int(time.time()),
                "last_notified_version": check.release.version,
            }
        )
        await ctx.followup.send(
            embed=self.build_embed(
                title="Confirm Update Install",
                check=check,
                release=check.release,
                color=discord.Color.orange(),
                description="Click Install Update to apply this release now.",
            ),
            view=UpdatePromptView(self, check.release, source="manual-install"),
        )

    @update.command(
        name="status",
        description="Show updater settings and pending version (admins only).",
    )
    async def status(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        cfg = get_updater_config()
        embed = discord.Embed(title="Updater Status", color=discord.Color.blurple())
        embed.add_field(
            name="Enabled", value=f"`{bool(cfg.get('enabled', True))}`", inline=True
        )
        embed.add_field(
            name="Startup Check",
            value=f"`{bool(cfg.get('check_on_startup', True))}`",
            inline=True,
        )
        embed.add_field(
            name="Beta Releases",
            value=f"`{bool(cfg.get('include_prerelease', False))}`",
            inline=True,
        )
        embed.add_field(
            name="Interval (sec)",
            value=f"`{int(cfg.get('check_interval_seconds', 21600))}`",
            inline=True,
        )
        embed.add_field(
            name="Pending Version",
            value=f"`{cfg.get('pending_version') or 'none'}`",
            inline=True,
        )
        embed.add_field(
            name="Installed Version",
            value=f"`{cfg.get('installed_version') or 'unknown'}`",
            inline=True,
        )
        embed.add_field(
            name="Last Notified",
            value=f"`{cfg.get('last_notified_version') or 'none'}`",
            inline=True,
        )
        embed.add_field(
            name="Last Checked",
            value=f"`{cfg.get('last_checked_epoch') or 'never'}`",
            inline=True,
        )
        repo = cfg.get("github_repo") or "auto"
        embed.add_field(name="Repository", value=f"`{repo}`", inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

    @update.command(
        name="auto",
        description="Enable/disable automatic periodic update checks (admins only).",
    )
    async def auto(
        self,
        ctx: discord.ApplicationContext,
        enabled: bool = discord.Option(
            bool,
            description="Set true to enable periodic checks, false to disable.",
            required=True,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        set_updater_config({"enabled": enabled})
        await ctx.respond(
            f"Automatic update checks are now {'enabled' if enabled else 'disabled'}.",
            ephemeral=True,
        )

    @update.command(
        name="beta",
        description="Enable/disable beta (pre-release) update channel (admins only).",
    )
    async def beta(
        self,
        ctx: discord.ApplicationContext,
        enabled: bool = discord.Option(
            bool,
            description="Set true to include pre-releases, false for stable-only.",
            required=True,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        set_updater_config(
            {
                "include_prerelease": enabled,
                "last_notified_version": None,
                "pending_version": None,
                "pending_release_url": None,
                "pending_notes": None,
                "pending_notified_epoch": None,
            }
        )
        await ctx.respond(
            (
                "Updater now includes pre-releases."
                if enabled
                else "Updater is now stable-only (pre-releases disabled)."
            )
            + " Run `/update check` to refresh availability.",
            ephemeral=True,
        )

    @update.command(
        name="startup",
        description="Enable/disable update checks during bot startup (admins only).",
    )
    async def startup(
        self,
        ctx: discord.ApplicationContext,
        enabled: bool = discord.Option(
            bool,
            description="Set true to check on startup, false to skip startup checks.",
            required=True,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        set_updater_config({"check_on_startup": enabled})
        await ctx.respond(
            f"Startup update checks are now {'enabled' if enabled else 'disabled'}.",
            ephemeral=True,
        )

    @update.command(
        name="interval",
        description="Set periodic update-check interval in seconds (admins only).",
    )
    async def interval(
        self,
        ctx: discord.ApplicationContext,
        seconds: int = discord.Option(
            int,
            description="Check interval in seconds.",
            required=True,
            min_value=60,
            max_value=604800,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        set_updater_config({"check_interval_seconds": int(seconds)})
        await ctx.respond(
            f"Update check interval set to `{int(seconds)}` seconds.",
            ephemeral=True,
        )

    @update.command(
        name="repo",
        description="Set GitHub repo as owner/name, or 'auto' to detect from git remote (admins only).",
    )
    async def repo(
        self,
        ctx: discord.ApplicationContext,
        repository: str = discord.Option(
            str,
            description="Example: brandonhenness/Mitra-Discord-Bot, or auto",
            required=True,
        ),
    ) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        value = repository.strip()
        if value.lower() == "auto":
            set_updater_config({"github_repo": None})
            await ctx.respond(
                "Updater repository reset to auto-detect from git remote.",
                ephemeral=True,
            )
            return

        if "/" not in value or value.count("/") != 1:
            await ctx.respond(
                "Repository must be in `owner/name` format, or `auto`.",
                ephemeral=True,
            )
            return

        owner, name = value.split("/", 1)
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            await ctx.respond(
                "Repository must be in `owner/name` format, or `auto`.",
                ephemeral=True,
            )
            return

        normalized = f"{owner}/{name}"
        set_updater_config({"github_repo": normalized})
        await ctx.respond(
            f"Updater repository set to `{normalized}`.",
            ephemeral=True,
        )

    @update.command(
        name="dismiss",
        description="Dismiss the currently pending update notification (admins only).",
    )
    async def dismiss(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        cfg = get_updater_config()
        pending_version = cfg.get("pending_version")
        if not pending_version:
            await ctx.respond("There is no pending update to dismiss.", ephemeral=True)
            return

        set_updater_config(
            {
                "last_notified_version": str(pending_version),
            }
        )
        await ctx.respond(
            f"Dismissed update `{pending_version}`. Use `/update install` anytime to apply it.",
            ephemeral=True,
        )

    @update.command(
        name="changelog",
        description="Show notes for the latest GitHub release (admins only).",
    )
    async def changelog(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return
        await self._send_latest_changelog(ctx=ctx)

    @update.command(
        name="changelong",
        description="Alias for /update changelog (admins only).",
    )
    async def changelong(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return
        await self._send_latest_changelog(ctx=ctx, force_name="Latest Changelog")


def setup(bot: discord.Bot) -> None:
    bot.add_cog(UpdateCog(bot))
