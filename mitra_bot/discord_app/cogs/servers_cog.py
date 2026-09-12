from __future__ import annotations

import discord
import asyncio
import time
import inspect
from discord.ext import commands

from mitra_bot.discord_app.checks import ensure_admin
from mitra_bot.discord_app.peer_dashboard import build_dashboard
from mitra_bot.services.peer_monitor import Setting, MonitorPolicy
from mitra_bot.services.peer_service import PeerError


class ServersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._dashboard_locks = {}

    servers = discord.SlashCommandGroup("servers", "Private network servers")

    @servers.command(name="doctor", description="Check connectivity, certificates, replication and alert permissions")
    async def doctor_command(self, ctx: discord.ApplicationContext):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        from mitra_bot.discord_app.peer_operations import doctor
        lines = await doctor(mesh,self.bot,ctx.guild)
        # Bound each message even with maximum-length node IDs and a large mesh.
        for start in range(0,len(lines),6):
            await ctx.respond("\n".join(lines[start:start+6]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

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
            role_setting = mesh.monitor.store.setting(ctx.guild.id,server)
            role = ctx.guild.get_role(role_setting["role"]) if role_setting and role_setting["role"] else None
            if mention and (role is None or not self._safe_role(role,ctx.guild)):
                raise ValueError("Configure a valid subscriber role before testing mentions.")
            if mention and not role.mentionable and not permissions.mention_everyone:
                raise ValueError("The subscriber role cannot be mentioned in this channel.")
            embed = discord.Embed(title=f"TEST ONLY — {server} monitoring alert",
                description=f"Requested by <@{ctx.author.id}> through `{mesh.config.node_id}`.\n"
                            "This is a delivery test, not an outage. No health history or incident was changed.",
                color=discord.Color.blue())
            from discord.http import Route
            payload = dict(content=role.mention if mention else "", embeds=[embed.to_dict()],
                           nonce=str(ctx.interaction.id),enforce_nonce=True,
                           allowed_mentions={"parse":[],"users":[],"roles":[str(role.id)] if mention else []})
            result = await self.bot.http.request(Route("POST","/channels/{channel_id}/messages",channel_id=channel.id),json=payload)
            await ctx.respond(f"Test sent: https://discord.com/channels/{ctx.guild.id}/{channel.id}/{result['id']}\n"
                              + ("Subscriber role mentioned." if mention else "No subscribers were pinged. Use mention:true to test the role mention."),ephemeral=True)
        except (ValueError,PeerError,discord.HTTPException,OSError) as exc:
            await ctx.respond(f"Test delivery was not confirmed: {exc}. Check the destination before retrying.",ephemeral=True)

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
            await ctx.respond(message+f" Replication pending on {pending} peer(s).",ephemeral=True)
        except (PeerError,ValueError) as exc:
            await ctx.respond(str(exc),ephemeral=True)

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
            await ctx.respond(f"Shared dashboard: {message.jump_url}\nRefresh every {interval}s, with peer takeover after a stale update. "
                              f"Replication pending on {pending} peer(s). Anyone with channel access can view it.",ephemeral=True)
        except (ValueError,PeerError,discord.HTTPException,OSError) as exc:
            await ctx.respond(f"Dashboard setup was not fully confirmed: {exc}. Inspect the channel and /servers doctor before retrying.",ephemeral=True)
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
            await ctx.respond("No shared dashboard is configured.",ephemeral=True)
            return
        current.update(revision=str(ctx.interaction.id),enabled=False)
        pending = await self._save_settings(mesh,[Setting.model_validate(current)])
        await ctx.respond(f"Automatic updates stopped. The existing message remains as a timestamped snapshot. Replication pending on {pending} peer(s).",ephemeral=True)

    async def _mesh(self, ctx, admin=True):
        if admin:
            guard = ensure_admin(ctx)
            if guard:
                await guard
                return None
        elif ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond("Use this command in a Discord server.", ephemeral=True)
            return None
        mesh = getattr(self.bot, "peer_service", None)
        if mesh is None or mesh.monitor is None:
            await ctx.respond("Private peer monitoring is not enabled on this instance.", ephemeral=True)
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
            await ctx.respond(str(exc), ephemeral=True)

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
            await ctx.respond(str(exc), ephemeral=True)

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
            await ctx.respond(str(exc), ephemeral=True)
            return
        rows = mesh.monitor.store.incidents(observer, server, limit=8, offset=(page-1)*8)
        embed = discord.Embed(title=f"{server}: incidents observed by {observer}", color=discord.Color.blue())
        for item in rows:
            end = f"<t:{int(item['recovered'])}:F>" if item["recovered"] else "Not yet observed recovered"
            embed.add_field(name=f"{item['kind']} connection · {item['reason']}",
                            value=f"First failure <t:{int(item['start'])}:F>\nConfirmed <t:{int(item['detected'])}:F>\nRecovery: {end}", inline=False)
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

    @servers.command(name="alerts", description="Configure an opt-in alert channel and a server subscriber role")
    async def alerts(self, ctx: discord.ApplicationContext,
                     channel: discord.Option(discord.TextChannel, "Outage/recovery destination"),
                     server: discord.Option(str, "Server whose subscription role to configure"),
                     role: discord.Option(discord.Role, "Existing role with no permissions (otherwise create one)") = None,
                     enabled: discord.Option(bool, "Enable alerts for this guild") = True):
        mesh = await self._mesh(ctx)
        if not mesh:
            return
        await ctx.defer(ephemeral=True)
        try:
            mesh.resolve(server)
            if channel.guild.id != ctx.guild.id:
                raise ValueError("Choose a channel in this guild.")
            permissions = channel.permissions_for(ctx.guild.me)
            if enabled and not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.read_message_history)):
                raise ValueError("The bot needs View Channel, Send Messages, Embed Links and Read Message History in that channel.")
            existing = mesh.monitor.store.setting(ctx.guild.id, server)
            if role is None and existing and existing["role"]:
                role = ctx.guild.get_role(existing["role"])
            name = f"Mitra {server} alerts"
            if role is None:
                roles = await ctx.guild.fetch_roles()
                matches = [r for r in roles if r.name == name]
                if len(matches) > 1:
                    raise ValueError("Multiple matching subscriber roles exist. Choose one explicitly and reconcile memberships before removing duplicates.")
                role = matches[0] if matches else await ctx.guild.create_role(name=name, permissions=discord.Permissions.none(), mentionable=True,
                                                                            reason="Mitra peer alert subscriptions")
                if not matches:
                    roles = await ctx.guild.fetch_roles()
                    if len([r for r in roles if r.name == name]) > 1:
                        raise ValueError("Concurrent setup created duplicate roles. Choose one explicitly with /servers alerts; no subscription mapping was saved.")
            if not self._safe_role(role, ctx.guild):
                raise ValueError("Choose an unmanaged role below the bot's highest role, with no permissions or channel grants. Mitra's administrator role cannot be used.")
            if not role.mentionable and not permissions.mention_everyone:
                raise ValueError("The subscriber role must be mentionable, or the bot must have Mention Everyone in the alert channel.")
            revision = str(ctx.interaction.id)
            pending = await self._save_settings(mesh, [Setting(revision=revision, guild=ctx.guild.id, subject="*", channel=channel.id, enabled=enabled),
                                                       Setting(revision=revision, guild=ctx.guild.id, subject=server, role=role.id)])
            await ctx.respond(f"Alerts {'enabled' if enabled else 'disabled'} in {channel.mention}. `{server}` subscriptions use {role.mention}. "
                              f"Saved locally; replication pending on {pending} peer(s). Members can use `/servers subscribe server:{server}`.",
                              allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)
        except (PeerError, ValueError, discord.HTTPException) as exc:
            await ctx.respond(f"Could not configure alerts: {exc}", ephemeral=True)

    async def _subscription(self, ctx, server, subscribe):
        mesh = await self._mesh(ctx, admin=False)
        if not mesh:
            return
        try:
            mesh.resolve(server)
            setting = mesh.monitor.store.setting(ctx.guild.id, server)
            role = ctx.guild.get_role(setting["role"]) if setting and setting["role"] else None
            if role is None:
                raise ValueError("An administrator must configure this server's role with /servers alerts first.")
            if subscribe and not self._safe_role(role, ctx.guild):
                raise ValueError("Subscriber role permissions changed; ask an administrator to configure a role with no permissions.")
            if subscribe:
                await ctx.author.add_roles(role, reason="Self-service Mitra peer alert subscription")
            else:
                await ctx.author.remove_roles(role, reason="Self-service Mitra peer alert unsubscription")
            await ctx.respond(f"{'Subscribed to' if subscribe else 'Unsubscribed from'} `{server}` alerts.", ephemeral=True)
        except (PeerError, ValueError, discord.HTTPException) as exc:
            await ctx.respond(str(exc), ephemeral=True)

    @servers.command(name="subscribe", description="Subscribe yourself to a server's outage and recovery alerts")
    async def subscribe(self, ctx: discord.ApplicationContext, server: discord.Option(str, "Server ID")):
        await self._subscription(ctx, server, True)

    @servers.command(name="unsubscribe", description="Unsubscribe yourself from a server's alerts")
    async def unsubscribe(self, ctx: discord.ApplicationContext, server: discord.Option(str, "Server ID")):
        await self._subscription(ctx, server, False)

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
        await ctx.respond("Monitoring policy:\n" + "\n".join(f"`{key}`: {value}" for key,value in policy.items()) + suffix,
                          ephemeral=True)

    @servers.command(name="list", description="List this server and its configured peers")
    async def list_servers(self, ctx: discord.ApplicationContext):
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        mesh = getattr(self.bot, "peer_service", None)
        if mesh is None:
            await ctx.respond("`local` — this server (private networking disabled)", ephemeral=True)
            return
        lines = [f"Responding instance: `{mesh.config.node_id}`",
                 f"Application-state owner: `{mesh.config.resolved_state_owner}`",
                 f"`{mesh.config.node_id}` — local, process uptime {mesh.local_health()['process_uptime_seconds']}s"]
        for node in sorted(mesh.peers):
            state = mesh.online.get(node)
            status = "reachable" if state is True else "unreachable" if state is False else "not checked yet"
            health = mesh.health.get(node)
            if health:
                status += f" | last seen <t:{health['captured_at']}:R> | Discord={'connected' if health['discord_connected'] else 'disconnected'}"
            lines.append(f"`{node}` — {status}")
        # Stay below Discord's message limit for the maximum mesh size.
        for start in range(0, len(lines), 20):
            await ctx.respond("\n".join(lines[start:start + 20]), ephemeral=True)
