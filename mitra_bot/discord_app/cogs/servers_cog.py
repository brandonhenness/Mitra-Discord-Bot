from mitra_bot.discord_app.message_style import embed as styled_embed
from mitra_bot.discord_app.message_style import notice
from mitra_bot.discord_app.message_style import pages
# Pycord evaluates Option objects while decorating commands. Postponed annotations
# turn these into strings, registering every option as text and breaking parsing.

import discord
import asyncio
import time
import inspect
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.services.alert_roles import configure_shared_role, subscription
from mitra_bot.discord_app.peer_dashboard import build_dashboard
from mitra_bot.services.peer_monitor import Setting, MonitorPolicy
from mitra_bot.services.peer_service import PeerError


class ServersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._dashboard_locks = {}

    servers = discord.SlashCommandGroup("servers", "Private network servers")

    @servers.command(name="sync-access", description="Replace one peer's trusted Discord servers with the owner's allowlist")
    async def sync_access(self, ctx: discord.ApplicationContext,
                          server: discord.Option(str, "Peer whose infrastructure allowlist will be replaced")):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        from mitra_bot.services.access_sync import sync_peer_access
        try:
            ids = await sync_peer_access(self.bot, server)
            await ctx.respond(notice('Infrastructure access synchronized',
                f"{server}: saved and applied the owner's trusted Discord server IDs: "
                + (", ".join(str(value) for value in ids) or "none")
                + ". No restart is required.", tone='success'), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        except (PeerError, ValueError, OSError) as exc:
            await ctx.respond(notice('Access synchronization not confirmed', str(exc), tone='warning'), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())

    @servers.command(name="doctor", description="Check connectivity, certificates, replication and alert permissions")
    async def doctor_command(self, ctx: discord.ApplicationContext):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        from mitra_bot.discord_app.peer_operations import doctor
        lines = await doctor(mesh,self.bot,ctx.guild)
        # Bound each message even with maximum-length node IDs and a large mesh.
        for message in pages("Server health check", lines):
            await ctx.respond(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @servers.command(name="alerts-test", description="Send a labeled test alert; optionally mention this server's subscribers")
    async def alerts_test(self, ctx: discord.ApplicationContext,
                          server: discord.Option(str,"Server ID"),
                          mention: discord.Option(bool,"Mention subscribers in this test") = False):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            mesh.resolve(server)
            setting = mesh.monitor.store.setting(ctx.guild.id,"*")
            if not setting or not setting["enabled"]:
                raise ValueError("Configure and enable /servers alerts first.")
            channel = self.bot.get_channel(setting["channel"]) or await self.bot.fetch_channel(setting["channel"])
            from mitra_bot.discord_app.peer_operations import verify_channel
            permissions = verify_channel(channel,ctx.guild)
            role_setting = setting if setting.get("role") else mesh.monitor.store.setting(ctx.guild.id,server)
            role = ctx.guild.get_role(role_setting["role"]) if role_setting and role_setting["role"] else None
            if mention and (role is None or not self._safe_role(role,ctx.guild)):
                raise ValueError("Configure a valid subscriber role before testing mentions.")
            if mention and not role.mentionable and not permissions.mention_everyone:
                raise ValueError("The subscriber role cannot be mentioned in this channel.")
            embed = styled_embed(title=f"TEST ONLY — {server} monitoring alert",
                description=f"Requested by <@{ctx.author.id}> through `{mesh.config.node_id}`.\n"
                            "This is a delivery test, not an outage. No health history or incident was changed.",
                color=discord.Color.blue())
            from discord.http import Route
            payload = dict(content=role.mention if mention else "", embeds=[embed.to_dict()],
                           nonce=str(ctx.interaction.id),enforce_nonce=True,
                           allowed_mentions={"parse":[],"users":[],"roles":[str(role.id)] if mention else []})
            result = await self.bot.http.request(Route("POST","/channels/{channel_id}/messages",channel_id=channel.id),json=payload)
            await ctx.respond(notice('Test alert sent', f"Test sent: https://discord.com/channels/{ctx.guild.id}/{channel.id}/{result['id']}\n"
                              + ("Subscriber role mentioned." if mention else "No subscribers were pinged. Use mention:true to test the role mention."), tone='success'),ephemeral=True)
        except (ValueError,PeerError,discord.HTTPException,OSError) as exc:
            await ctx.respond(notice('Action not confirmed', f"Test delivery was not confirmed: {exc}. Check the destination before retrying.", tone='warning'),ephemeral=True)

    @servers.command(name="maintenance", description="Pause a server's alerts network-wide while continuing health history")
    async def maintenance_command(self, ctx: discord.ApplicationContext,
                                  server: discord.Option(str,"Server ID"),
                                  minutes: discord.Option(int,"Duration in minutes; 0 ends maintenance",min_value=0,max_value=10080),
                                  reason: discord.Option(str,"Reason for planned work",max_length=200) = "Planned maintenance"):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            mesh.resolve(server)
            until = time.time()+minutes*60 if minutes else 0.0
            pending = await self._save_settings(mesh,[Setting(revision=str(ctx.interaction.id),guild=1,subject=server,
                maintenance_until=until,maintenance_reason=reason)])
            message = f"`{server}` maintenance ends <t:{int(until)}:R>. History continues; outage/recovery alerts are suppressed until then." if minutes else f"Maintenance ended for `{server}`."
            await ctx.respond(notice('Server monitoring', message+f" Replication pending on {pending} peer(s).", tone='info'),ephemeral=True)
        except (PeerError,ValueError) as exc:
            await ctx.respond(notice('Server request could not finish', str(exc), tone='error'),ephemeral=True)

    @servers.command(name="dashboard-pin", description="Create or update one shared, automatically refreshed dashboard in this guild")
    async def dashboard_pin(self, ctx: discord.ApplicationContext,
                            channel: discord.Option(discord.TextChannel,"Channel whose members may view server health"),
                            interval: discord.Option(int,"Refresh interval in seconds",min_value=60,max_value=3600) = 300,
                            hours: discord.Option(int,"History window in hours",min_value=1,max_value=2160) = 24,
                            page: discord.Option(int,"Dashboard page",min_value=1,max_value=9) = 1):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        from mitra_bot.discord_app.peer_operations import verify_channel,public_dashboard,find_dashboard,dashboard_marker
        payload = None
        lock = self._dashboard_locks.setdefault(ctx.guild.id,asyncio.Lock())
        await lock.acquire()
        try:
            verify_channel(channel,ctx.guild,dashboard=True,pin=True)
            if (page-1)*8 >= len(mesh.peers)+1:
                raise ValueError("That dashboard page does not exist.")
            setting = Setting(revision=str(ctx.interaction.id),guild=ctx.guild.id,subject="@dashboard",channel=channel.id,
                              dashboard_interval=interval,dashboard_hours=hours,dashboard_page=page-1)
            marker = dashboard_marker(mesh,ctx.guild.id)
            message = await find_dashboard(channel,self.bot,marker)
            payload = await public_dashboard(mesh,setting.model_dump())
            if message:
                await message.edit(**payload,attachments=[])
            else:
                options = dict(nonce=str(ctx.interaction.id))
                if "enforce_nonce" in inspect.signature(channel.send).parameters:
                    options["enforce_nonce"] = True
                message = await channel.send(**payload,**options)
            setting.message_id = message.id
            # Save before pinning: even a pin permission failure must not orphan a live dashboard.
            pending = await self._save_settings(mesh,[setting])
            await message.pin(reason="Mitra shared server dashboard")
            await ctx.respond(notice('Dashboard configured', f"Shared dashboard: {message.jump_url}\nRefresh every {interval}s, with peer takeover after a stale update. "
                              f"Replication pending on {pending} peer(s). Anyone with channel access can view it.", tone='success'),ephemeral=True)
        except (ValueError,PeerError,discord.HTTPException,OSError) as exc:
            await ctx.respond(notice('Dashboard setup not confirmed', f"{exc}\n\nInspect the channel and run `/servers doctor` before retrying.", tone='warning'),ephemeral=True)
        finally:
            if payload:
                payload["file"].close()
            lock.release()

    @servers.command(name="dashboard-stop", description="Stop automatic updates of this guild's shared dashboard")
    async def dashboard_stop(self, ctx: discord.ApplicationContext):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        current = mesh.monitor.store.setting(ctx.guild.id,"@dashboard")
        if not current:
            await ctx.respond(notice('No dashboard configured', "No shared dashboard is configured.", tone='info'),ephemeral=True)
            return
        current.update(revision=str(ctx.interaction.id),enabled=False)
        pending = await self._save_settings(mesh,[Setting.model_validate(current)])
        await ctx.respond(notice('Dashboard refresh stopped', f"Automatic updates stopped. The existing message remains as a timestamped snapshot. Replication pending on {pending} peer(s).", tone='success'),ephemeral=True)

    async def _mesh(self, ctx, admin=True):
        if admin:
            guard = ensure_admin(ctx)
            if guard:
                await guard
                return None
        elif ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(notice('Use this command in Discord', "Use this command in a Discord server.", tone='warning'), ephemeral=True)
            return None
        mesh = getattr(self.bot, "peer_service", None)
        if mesh is None or mesh.monitor is None:
            await ctx.respond(notice('Server monitoring', "Private peer monitoring is not enabled on this instance.", tone='info'), ephemeral=True)
            return None
        return mesh

    @servers.command(name="status", description="Show a server's availability, Discord connectivity and latency history")
    async def status(self, ctx: discord.ApplicationContext,
                     server: discord.Option(str, "Server ID from /servers list"),
                     hours: discord.Option(int, "History window in hours", min_value=1, max_value=2160) = 24,
                     observer: discord.Option(str, "Observer ID (default: responding instance)") = None):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            await ctx.respond(**await build_dashboard(mesh, server, observer, hours), ephemeral=True)
        except (PeerError, ValueError) as exc:
            await ctx.respond(notice('Server request could not finish', str(exc), tone='error'), ephemeral=True)

    @servers.command(name="dashboard", description="Show availability timelines for all servers")
    async def dashboard(self, ctx: discord.ApplicationContext,
                        hours: discord.Option(int, "History window in hours", min_value=1, max_value=2160) = 24,
                        observer: discord.Option(str, "Observer ID (default: responding instance)") = None,
                        page: discord.Option(int, "Page number", min_value=1, max_value=9) = 1):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            await ctx.respond(**await build_dashboard(mesh, None, observer, hours, page-1), ephemeral=True)
        except (PeerError, ValueError) as exc:
            await ctx.respond(notice('Server request could not finish', str(exc), tone='error'), ephemeral=True)

    @servers.command(name="incidents", description="Show observed outages and recoveries")
    async def incidents(self, ctx: discord.ApplicationContext,
                        server: discord.Option(str, "Server ID"),
                        observer: discord.Option(str, "Observer ID (default: responding instance)") = None,
                        page: discord.Option(int, "Page number", min_value=1, max_value=1000) = 1):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        try:
            mesh.resolve(server)
            observer = mesh.resolve(observer or mesh.config.node_id)
        except PeerError as exc:
            await ctx.respond(notice('Server request could not finish', str(exc), tone='error'), ephemeral=True)
            return
        rows = mesh.monitor.store.incidents(observer, server, limit=8, offset=(page-1)*8)
        embed = styled_embed(title=f"Connection history for {server}", description=f"Observed by **{observer}**", color=discord.Color.blue())
        for item in rows:
            end = f"<t:{int(item['recovered'])}:F>" if item["recovered"] else "Not yet observed recovered"
            kind = "Peer connection" if item['kind'] == 'peer' else "Discord connection"
            status = "Recovered" if item['recovered'] else "Recovery not observed"
            embed.add_field(name=f"{kind} — {status}",
                            value=f"**Reason** {item['reason']}\n**First failure** <t:{int(item['start'])}:f>\n**Confirmed** <t:{int(item['detected'])}:f>\n**Recovery** {end}", inline=False)
        if not rows:
            embed.description = "No recorded incidents on this page. Missing observations do not prove uptime."
        embed.set_footer(text=f"Page {page} · observer downtime is unknown coverage")
        await ctx.respond(embed=embed, ephemeral=True)

    async def _save_settings(self, mesh, values):
        with mesh.db:
            for value in values:
                mesh.monitor.store.append("setting", value.model_dump())
        mesh.monitor.apply_policy()
        semaphore = asyncio.Semaphore(8)
        async def share(peer):
            try:
                async with semaphore:
                    for value in values:
                        await mesh.request(peer, "monitor_settings", value.model_dump(), timeout=3)
                return True
            except PeerError:
                return False
        results = await asyncio.gather(*(share(peer) for peer in mesh.peers))
        return sum(not result for result in results)

    def _safe_role(self, role, guild):
        # Subscription must never grant privileges, including after a role is edited.
        return (role is not None and role.id != guild.id and not role.managed
                and role.permissions.value == 0 and role < guild.me.top_role
                and role.name != getattr(getattr(self.bot, "state", None), "admin_role_name", None)
                and not any(channel.overwrites_for(role).pair()[0].value for channel in guild.channels))

    @servers.command(name="alerts", description="Configure the shared Mitra Alerts role and health alert channel")
    async def alerts(self, ctx: discord.ApplicationContext,
                     channel: discord.Option(discord.TextChannel, "Alert destination"),
                     enabled: discord.Option(bool, "Enable IP and health alerts for this guild") = True):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            from mitra_bot.discord_app.peer_operations import verify_channel
            verify_channel(channel, ctx.guild)
            role = await configure_shared_role(self.bot, ctx.guild)
            revision = str(ctx.interaction.id)
            values = [Setting(revision=revision, guild=ctx.guild.id, subject="*", channel=channel.id, role=role.id, enabled=enabled)]
            for node in mesh.monitor.store.members:
                previous = mesh.monitor.store.setting(ctx.guild.id, node)
                value = Setting.model_validate(previous) if previous else Setting(revision=revision, guild=ctx.guild.id, subject=node)
                values.append(value.model_copy(update={"revision": revision, "role": role.id}))
            pending = await self._save_settings(mesh, values)
            await ctx.respond(notice('Alert settings saved', f"**Health alerts** {'Enabled' if enabled else 'Disabled'}\n"
                              f"**Channel** {channel.mention}\n**Subscriber role** {role.mention}\n\n"
                              "One subscription covers IP changes and health alerts for all servers. Existing subscribers migrated.\n\n"
                              f"**Settings sync** Waiting for {pending} peer(s).\n"
                              "Use `/alerts subscribe` or `/alerts unsubscribe`.", tone='success'),
                              allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)
        except (PeerError, ValueError, discord.HTTPException) as exc:
            await ctx.respond(notice('Server request could not finish', f"Could not configure alerts: {exc}", tone='error'), ephemeral=True)

    alerts_group = discord.SlashCommandGroup("alerts", "Unified Mitra alert subscriptions")

    @alerts_group.command(name="setup", description="Set up Mitra Alerts and migrate existing subscribers (admins only)")
    async def alerts_setup(self, ctx: discord.ApplicationContext,
                           channel: discord.Option(discord.TextChannel, "Alert destination")):
        if getattr(self.bot, "peer_service", None):
            await ServersCog.alerts.callback(self, ctx, channel)
            return
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        await ctx.defer(ephemeral=True)
        try:
            from mitra_bot.discord_app.peer_operations import verify_channel
            from mitra_bot.storage.storage_store import set_notification_channel_id_for_guild
            verify_channel(channel, ctx.guild)
            await configure_shared_role(self.bot, ctx.guild)
            set_notification_channel_id_for_guild(ctx.guild.id, channel.id)
            await ctx.respond(notice('Alerts are ready', "Mitra Alerts configured. Use /alerts subscribe for all operational alerts.", tone='success'), ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await ctx.respond(notice('Server request could not finish', f"Could not configure alerts: {exc}", tone='error'), ephemeral=True)

    @alerts_group.command(name="subscribe", description="Subscribe to all Mitra operational alerts")
    async def alerts_subscribe(self, ctx: discord.ApplicationContext,
                               user: discord.Option(discord.Member, "Member to subscribe (Mitra admins only)") = None):
        await subscription(ctx, True, user)

    @alerts_group.command(name="unsubscribe", description="Unsubscribe from all Mitra operational alerts")
    async def alerts_unsubscribe(self, ctx: discord.ApplicationContext,
                                 user: discord.Option(discord.Member, "Member to unsubscribe (Mitra admins only)") = None):
        await subscription(ctx, False, user)

    @servers.command(name="monitoring", description="View or change shared network monitoring thresholds and retention")
    async def monitoring(self, ctx: discord.ApplicationContext,
                         interval: discord.Option(int, "Probe interval seconds", min_value=5, max_value=300) = None,
                         timeout: discord.Option(int, "Probe timeout seconds", min_value=1, max_value=30) = None,
                         failures: discord.Option(int, "Consecutive failures", min_value=2, max_value=20) = None,
                         down_seconds: discord.Option(int, "Minimum loss duration", min_value=10, max_value=3600) = None,
                         cooldown: discord.Option(int, "Flapping mention cooldown seconds", min_value=0, max_value=86400) = None,
                         raw_days: discord.Option(int, "Raw sample retention days", min_value=1, max_value=30) = None,
                         history_days: discord.Option(int, "Graph retention days", min_value=7, max_value=365) = None):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        policy = {key: getattr(mesh.config, key) for key in MonitorPolicy.model_fields}
        options = dict(health_interval=interval, health_timeout=timeout, health_failures=failures,
                       health_down_seconds=down_seconds, health_cooldown=cooldown,
                       health_raw_days=raw_days, health_history_days=history_days)
        updates = {key:value for key,value in options.items() if value is not None}
        suffix = ""
        if updates:
            policy.update(updates)
            pending = await self._save_settings(mesh, [Setting(revision=str(ctx.interaction.id), guild=1, subject="@monitoring",
                                                              policy=MonitorPolicy(**policy))])
            suffix = f"\nSaved network-wide; replication pending on {pending} peer(s)."
        labels = {"interval": "Check interval (seconds)", "timeout": "Connection timeout (seconds)",
                  "failures": "Failures before confirming an outage", "down_seconds": "Minimum outage duration (seconds)",
                  "cooldown": "Repeat-mention cooldown (seconds)", "raw_days": "Detailed history (days)",
                  "history_days": "Graph history (days)"}
        await ctx.respond(notice('Monitoring settings', "\n".join(f"**{labels.get(key, key.replace('_', ' ').capitalize())}** {value}" for key,value in policy.items()) + suffix),
                          ephemeral=True)

    @servers.command(name="list", description="List this server and its configured peers")
    async def list_servers(self, ctx: discord.ApplicationContext):
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        mesh = getattr(self.bot, "peer_service", None)
        if mesh is None:
            await ctx.respond(notice('Server monitoring', "`local` — this server (private networking disabled)", tone='info'), ephemeral=True)
            return
        heading = ("### Server connections\n"
                   f"Responding server: **{mesh.config.node_id}** · Shared settings owner: **{mesh.config.resolved_state_owner}**\n\n")
        uptime = max(0, int(mesh.local_health()['process_uptime_seconds']))
        days, remainder = divmod(uptime, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"
        lines = [f"**🖥️ {mesh.config.node_id} — This server**\nBot running for {duration}"]
        for node in sorted(mesh.peers):
            state = mesh.online.get(node)
            status = "Reachable" if state is True else "Unreachable" if state is False else "Not checked yet"
            icon = "🟢" if state is True else "🔴" if state is False else "⚪"
            health = mesh.health.get(node)
            detail = ""
            if health:
                detail = (f"\nLast contact <t:{int(health['captured_at'])}:R>"
                          f" · Discord {'connected' if health['discord_connected'] else 'disconnected'} at that contact")
            lines.append(f"**{icon} {node} — {status}**{detail}")
        for start in range(0, len(lines), 6):
            await ctx.respond(heading + "\n\n".join(lines[start:start + 6]), ephemeral=True,
                              allowed_mentions=discord.AllowedMentions.none())
