from __future__ import annotations
from mitra_bot.discord_app.message_style import embed as styled_embed
from mitra_bot.discord_app.message_style import notice

import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.discord_app.access import infrastructure_guild, infrastructure_channel
from mitra_bot.services.alert_roles import shared_role
from mitra_bot.discord_app.command_errors import report_command_error
from mitra_bot.services.update_service import (
    InstallResult,
    ReleaseInfo,
    UpdateCheckResult,
    check_latest_release,
    install_release,
    spawn_replacement_process,
)
from mitra_bot.storage.storage_store import (
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
    def __init__(self, cog: "UpdateCog", release: ReleaseInfo, *, source: str, server: str = "all") -> None:
        super().__init__(timeout=3600)
        self.cog = cog
        self.release = release
        self.source = source
        self.server = server

    async def on_error(self, error, item, interaction):
        ctx = await interaction.client.get_application_context(interaction)
        await report_command_error(ctx, error)

    def _is_admin_user(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        user = interaction.user
        if not infrastructure_guild(interaction.client, guild) or not isinstance(user, discord.Member):
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
                notice('Administrator access required', "Only members with the admin role can install updates.", tone='warning'),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        fleet = getattr(self.cog.bot, "fleet_updates", None)
        if fleet is not None:
            plan = await fleet.begin(self.server, self.release.version, interaction.user.id,
                                     channel_id=interaction.channel_id)
            await interaction.edit_original_response(content=notice('Rolling update started', f"**Servers** `{self.server}`\n**Update reference** `{plan['id']}`\n\n"
                "Progress is posted in this channel. Use `/update status` to check at any time.\n"
                "The rollout stops if a server fails to recover.", tone='info'), embed=None, view=None)
            self.stop()
            return
        await self.cog.install_release_with_feedback(
            release=self.release,
            message=interaction.message,
            source=self.source,
            interaction=interaction,
        )
        self.stop()

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        if not self._is_admin_user(interaction):
            await interaction.response.send_message(
                notice('Administrator access required', "Only members with the admin role can dismiss updates.", tone='warning'),
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
                title="Update dismissed",
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
        embed = styled_embed(
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
            if check is None:
                embed.add_field(name="Release version", value=f"`{release.version}`", inline=True)
            if release.html_url:
                embed.add_field(
                    name="Release",
                    value=f"[Open Release]({release.html_url})",
                    inline=True,
                )
            embed.add_field(
                name="What changed",
                value=_trim(release.notes),
                inline=False,
            )
        return embed

    async def _find_announce_channel(self) -> Optional[discord.abc.Messageable]:
        channel_map = get_notification_channel_map()
        for guild in self.bot.guilds:
            if not infrastructure_guild(self.bot, guild):
                continue
            per_guild = channel_map.get(guild.id)
            if not per_guild:
                continue
            ch = self.bot.get_channel(per_guild)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(per_guild)
                except Exception:
                    continue
            if isinstance(ch, (discord.TextChannel, discord.Thread)) and infrastructure_channel(self.bot, ch):
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
            if isinstance(ch, (discord.TextChannel, discord.Thread)) and infrastructure_channel(self.bot, ch):
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
            description="A new release is available. Install updates all configured nodes one at a time (or this installation in standalone mode).",
        )
        role = shared_role(channel.guild) if getattr(channel, "guild", None) else None
        await channel.send(
            content=role.mention if role else None,
            embed=embed,
            view=UpdatePromptView(self, check.release, source=source),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else []),
        )
        set_updater_config({"last_notified_version": check.release.version})

    async def _restart_after_update(self, *, origin_message: discord.Message) -> None:
        try:
            spawn_replacement_process()
        except Exception as exc:
            await origin_message.channel.send(
                notice('Update request could not finish', f"Update installed, but failed to spawn replacement process: `{exc}`", tone='error')
            )
            return

        await origin_message.channel.send(
            notice('Update installed', "Update installed. Restarting bot process now.", tone='success')
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
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        if message is None:
            return
        if self._install_lock.locked():
            if interaction is not None:
                await interaction.followup.send(notice('An action is already in progress', "An update install is already in progress.", tone='warning'), ephemeral=True)
            else:
                await message.reply("An update install is already in progress.")
            return

        async def edit_feedback(embed):
            if interaction is not None:
                # Component messages may be ephemeral after a peer claims /update.
                # Message.edit uses the channel endpoint, which cannot edit them.
                await interaction.edit_original_response(embed=embed, view=None)
            else:
                await message.edit(embed=embed, view=None)

        async with self._install_lock:
            installing_embed = self.build_embed(
                title="Installing update",
                check=None,
                release=release,
                color=discord.Color.gold(),
                description=f"Starting install (triggered from `{source}`).",
            )
            await edit_feedback(installing_embed)

            result: InstallResult = await asyncio.to_thread(install_release, release)
            if not result.ok:
                failed_embed = self.build_embed(
                    title="Update failed",
                    check=None,
                    release=release,
                    color=discord.Color.red(),
                    description=f"Install failed: {_trim(result.error or 'unknown error', 500)}",
                )
                await edit_feedback(failed_embed)
                return

            success_embed = self.build_embed(
                title="Update installed",
                check=None,
                release=release,
                color=discord.Color.green(),
                description=(
                    f"Release `{result.version or release.version}` installed successfully. "
                    "Restarting now."
                ),
            )
            await edit_feedback(success_embed)
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

    async def fleet_prompt(self, ctx, server):
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        await ctx.defer(ephemeral=True)
        try:
            fleet = self.bot.fleet_updates
            states = await fleet.preview(server)
            check = await asyncio.to_thread(check_latest_release)
            if check.error or check.release is None:
                raise ValueError(check.error or "No release available")
            from packaging.version import Version
            lines = [f"`{node}`: `{state['version'][:40]}`" for node, state in list(states.items())[:12]]
            if len(states) > 12:
                lines.append(f"...and {len(states)-12} additional nodes.")
            newer = any(Version(state["version"]) < Version(check.release.version) for state in states.values())
            text = (f"**Servers to update** `{server}`\n**Release** `{check.release.version}` · [View release]({check.release.html_url})\n\n" + "\n".join(lines)
                    + ("\nInstall updates one node at a time, coordinator last. A failed restart stops the rollout."
                       if newer else "\nEvery selected node is already at this release or newer."))
            await ctx.respond(notice('Review the server update', text), ephemeral=True,
                view=UpdatePromptView(self, check.release, source="fleet", server=server) if newer else None,
                allowed_mentions=discord.AllowedMentions.none())
        except ValueError as exc:
            await ctx.respond(notice('Update request could not finish', f"Could not prepare rolling update: {exc}", tone='error'), ephemeral=True)

    @update.command(
        name="check",
        description="Check GitHub for a new bot release (admins only).",
    )
    async def check(self, ctx: discord.ApplicationContext,
                    server: str = discord.Option(str, "Node ID or all (peer networks default to all)", default="all", required=False)) -> None:
        if getattr(self.bot, "fleet_updates", None) is not None:
            await self.fleet_prompt(ctx, server)
            return
        if server not in {"all", "local"}:
            await ctx.respond(notice('Bot updates', "Standalone mode supports only server:local or server:all.", tone='info'), ephemeral=True)
            return
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.defer()
        check = await asyncio.to_thread(check_latest_release)
        if check.error:
            await ctx.followup.send(
                embed=self.build_embed(
                    title="Update check failed",
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
                    title="Mitra is up to date",
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
                title="Update available",
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
    async def install(self, ctx: discord.ApplicationContext,
                      server: str = discord.Option(str, "Node ID or all (peer networks default to all)", default="all", required=False)) -> None:
        if getattr(self.bot, "fleet_updates", None) is not None:
            await self.fleet_prompt(ctx, server)
            return
        if server not in {"all", "local"}:
            await ctx.respond(notice('Bot updates', "Standalone mode supports only server:local or server:all.", tone='info'), ephemeral=True)
            return
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        await ctx.defer()
        check = await asyncio.to_thread(check_latest_release)
        if check.error or not check.available or check.release is None:
            await ctx.followup.send(
                notice('No update to install', "No installable update found. Run `/update check` for details.", tone='info'),
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
                title="Review and install the update",
                check=check,
                release=check.release,
                color=discord.Color.orange(),
                description="Click Install Update to apply this release now.",
            ),
            view=UpdatePromptView(self, check.release, source="manual-install"),
        )

    @update.command(name="cancel", description="Stop a rolling update before starting any further nodes (admins only)")
    async def cancel(self, ctx: discord.ApplicationContext):
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        fleet = getattr(self.bot, "fleet_updates", None)
        if fleet is None:
            await ctx.respond(notice('Bot updates', "No peer update coordinator is running.", tone='info'), ephemeral=True)
            return
        try:
            plan_id = fleet.cancel(ctx.author.id)
            await ctx.respond(notice('Rolling update cancelled', f"Cancelled rollout `{plan_id}`. An installation already started will finish; no further nodes will start.", tone='warning'), ephemeral=True)
        except ValueError as exc:
            await ctx.respond(notice('Update request could not finish', str(exc), tone='error'), ephemeral=True)

    @update.command(
        name="status",
        description="Show updater settings and pending version (admins only).",
    )
    async def status(self, ctx: discord.ApplicationContext) -> None:
        admin_guard = ensure_admin(ctx)
        if admin_guard:
            await admin_guard
            return

        fleet = getattr(self.bot, "fleet_updates", None)
        if fleet is not None:
            plans = fleet.rows("update_plans")
            if plans:
                plan = plans[0]
                from mitra_bot.services.fleet_updates import FleetUpdates
                from mitra_bot.discord_app.message_style import pages
                for text in pages(FleetUpdates.progress_title(plan), FleetUpdates.progress_sections(plan, full=True)):
                    await ctx.respond(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return
        cfg = get_updater_config()
        embed = styled_embed(title="Update settings", color=discord.Color.blurple())
        embed.add_field(
            name="Enabled", value="Enabled" if cfg.get("enabled", True) else "Disabled", inline=True
        )
        embed.add_field(
            name="Startup Check",
            value="Enabled" if cfg.get("check_on_startup", True) else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Beta Releases",
            value="Included" if cfg.get("include_prerelease", False) else "Stable releases only",
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
            value=f"<t:{int(cfg['last_checked_epoch'])}:R>" if cfg.get("last_checked_epoch") else "Never",
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
            notice('Update settings saved', f"Automatic update checks are now {'enabled' if enabled else 'disabled'}.", tone='info'),
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
            notice('Bot updates', (
                "Updater now includes pre-releases."
                if enabled
                else "Updater is now stable-only (pre-releases disabled)."
            )
            + " Run `/update check` to refresh availability.", tone='info'),
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
            notice('Update settings saved', f"Startup update checks are now {'enabled' if enabled else 'disabled'}.", tone='info'),
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
            notice('Update settings saved', f"Update check interval set to `{int(seconds)}` seconds.", tone='info'),
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
            description="Example: owner/repo, or auto",
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
                notice('Bot updates', "Updater repository reset to auto-detect from git remote.", tone='info'),
                ephemeral=True,
            )
            return

        if "/" not in value or value.count("/") != 1:
            await ctx.respond(
                notice('Bot updates', "Repository must be in `owner/name` format, or `auto`.", tone='info'),
                ephemeral=True,
            )
            return

        owner, name = value.split("/", 1)
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            await ctx.respond(
                notice('Bot updates', "Repository must be in `owner/name` format, or `auto`.", tone='info'),
                ephemeral=True,
            )
            return

        normalized = f"{owner}/{name}"
        set_updater_config({"github_repo": normalized})
        await ctx.respond(
            notice('Bot updates', f"Updater repository set to `{normalized}`.", tone='info'),
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
            await ctx.respond(notice('No pending update', "There is no pending update to dismiss.", tone='info'), ephemeral=True)
            return

        set_updater_config(
            {
                "last_notified_version": str(pending_version),
            }
        )
        await ctx.respond(
            notice('Update dismissed', f"Dismissed update `{pending_version}`. Use `/update install` anytime to apply it.", tone='info'),
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
