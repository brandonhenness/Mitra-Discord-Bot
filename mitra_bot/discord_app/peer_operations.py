"""Operational diagnostics and a shared, restart-safe Discord dashboard."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import discord

from mitra_bot.discord_app.peer_dashboard import build_dashboard


def dashboard_marker(mesh, guild):
    network = hashlib.sha256(mesh.config.network_id.encode()).hexdigest()[:16]
    return f"Mitra shared dashboard {network}:{guild}"


def verify_channel(channel, guild, *, dashboard=False, pin=False):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise ValueError("Choose a text channel in this guild.")
    permissions = channel.permissions_for(guild.me)
    needed = ["view_channel", "send_messages", "embed_links", "read_message_history"]
    if dashboard:
        needed += ["attach_files"]
    missing = [name.replace("_", " ") for name in needed if not getattr(permissions, name)]
    if pin and not getattr(permissions,"pin_messages", bool(permissions.value & (1 << 51)) or permissions.administrator):
        missing.append("pin messages")
    if missing:
        raise ValueError("The bot needs: " + ", ".join(missing))
    return permissions


async def public_dashboard(mesh, setting):
    payload = await build_dashboard(mesh, hours=setting["dashboard_hours"], page=setting["dashboard_page"])
    payload["view"] = None
    payload["allowed_mentions"] = discord.AllowedMentions.none()
    embed = payload["embed"]
    embed.timestamp = datetime.now(timezone.utc)
    embed.set_footer(text=dashboard_marker(mesh, setting["guild"]) + " · UTC · refreshed by " + mesh.config.node_id)
    return payload


class SharedDashboard:
    def __init__(self, bot, mesh):
        self.bot, self.mesh = bot, mesh

    async def __call__(self):
        if not self.bot.is_ready() or not self.bot.gateway_connected:
            return
        failures = []
        for setting in self.mesh.monitor.store.settings():
            if setting["subject"] != "@dashboard" or not setting["enabled"] or not setting.get("message_id"):
                continue
            try:
                await self.refresh(setting)
            except (discord.HTTPException, OSError, ValueError) as exc:
                failures.append(f"guild {setting['guild']}: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("Dashboard refresh failed: " + ", ".join(failures))

    async def refresh(self, setting):
        channel = self.bot.get_channel(setting["channel"]) or await self.bot.fetch_channel(setting["channel"])
        verify_channel(channel, channel.guild, dashboard=True)
        if channel.guild.id != setting["guild"]:
            raise ValueError("Dashboard guild mismatch")
        message = await channel.fetch_message(setting["message_id"])
        marker = dashboard_marker(self.mesh, setting["guild"])
        if message.author.id != self.bot.user.id or not any((getattr(e.footer, "text", None) or "").startswith(marker) for e in message.embeds):
            raise ValueError("Configured message is not this network's dashboard")
        rank = sorted([self.mesh.config.node_id, *self.mesh.peers]).index(self.mesh.config.node_id)
        threshold = setting["dashboard_interval"] + rank*15
        if time.time() - (message.edited_at or message.created_at).timestamp() < threshold:
            return
        payload = await public_dashboard(self.mesh, setting)
        try:
            # Recheck after graph rendering: another observer may already have refreshed it.
            current = self.mesh.monitor.store.setting(setting["guild"], "@dashboard")
            if not current or current["revision"] != setting["revision"] or not current["enabled"]:
                return
            message = await channel.fetch_message(setting["message_id"])
            if time.time() - (message.edited_at or message.created_at).timestamp() < threshold:
                return
            await message.edit(**payload, attachments=[])
        finally:
            payload["file"].close()


async def find_dashboard(channel, bot, marker):
    # Support both py-cord's older awaitable pins and newer asynchronous iterator.
    pins = channel.pins()
    if inspect.isawaitable(pins):
        messages = await pins
    else:
        messages = [message async for message in pins]
    for message in messages:
        message = getattr(message,"message",message)
        if message.author.id == bot.user.id and any((getattr(e.footer, "text", None) or "").startswith(marker) for e in message.embeds):
            return message
    async for message in channel.history(limit=100):
        if message.author.id == bot.user.id and any((getattr(e.footer, "text", None) or "").startswith(marker) for e in message.embeds):
            return message
    return None


async def doctor(mesh, bot, guild):
    """Read-only checks: no messages, new settings, power operations or key contents."""
    lines = [f"Instance: `{mesh.config.node_id}` · state owner: `{mesh.config.resolved_state_owner}`",
             f"Discord: {'connected' if bot.is_ready() and bot.gateway_connected else 'disconnected'}"]
    tasks = mesh.monitor.tasks
    lines.append(f"Monitoring tasks running: {sum(not task.done() for task in tasks)}/{len(tasks)}")
    for label, filename in (("Node certificate", mesh.config.cert_file), ("CA certificate", mesh.config.ca_file)):
        try:
            # Decode only the public certificate using the bundled OpenSSL binding.
            certificate = await asyncio.to_thread(ssl._ssl._test_decode_cert, str(Path(filename).resolve()))
            expires = ssl.cert_time_to_seconds(certificate["notAfter"])
            days = int((expires-time.time())/86400)
            lines.append(f"{label}: expires <t:{int(expires)}:R>" + (" — renewal needed" if days < 30 else ""))
        except (OSError, ValueError, ssl.SSLError):
            lines.append(f"{label}: could not read expiry; check configured certificate path.")
    semaphore = asyncio.Semaphore(8)
    async def check(target):
        from mitra_bot.services.peer_service import Health, PeerError
        async with semaphore:
            try:
                health = Health.model_validate(await mesh.request(target,"health",{},timeout=3))
                if health.node_id != target:
                    raise ValueError("Identity mismatch")
                local_cursor = mesh.monitor.store.cursor(target)
                page = await mesh.request(target,"history",{"after":local_cursor},timeout=3)
                from mitra_bot.services.peer_monitor import HistoryPage
                page = HistoryPage.model_validate(page)
                sync = "caught up" if page.cursor == local_cursor else "catch-up pending"
                if page.cursor < local_cursor:
                    sync = "source sequence regressed; database recovery required"
                latest = mesh.monitor.store.latest(mesh.config.node_id,target)
                age = f" · observation age {int(time.time()-latest['ts'])}s" if latest else " · no observations yet"
                expires = getattr(mesh,"peer_certificate_expiry",{}).get(target)
                expiry = f" · certificate expires <t:{int(expires)}:R>" if expires else ""
                if expires and expires-time.time() < 30*86400:
                    expiry += " (renew soon)"
                return f"`{target}`: authenticated TLS OK · Discord {'connected' if health.discord_connected else 'disconnected'} · history {sync}{age}{expiry}"
            except (PeerError, ValueError) as exc:
                cause = exc.__cause__ or exc
                return f"`{target}`: check failed ({type(cause).__name__}); check reachability, membership, certificates and synchronized clocks."
    lines.extend(await asyncio.gather(*(check(peer) for peer in sorted(mesh.peers))))
    store = mesh.monitor.store
    pending = store.db.execute("SELECT count(*) FROM health_outbox WHERE delivered IS NULL").fetchone()[0]
    errors = store.db.execute("SELECT error,count(*) FROM health_outbox WHERE delivered IS NULL AND error IS NOT NULL GROUP BY error").fetchall()
    lines.append(f"Queued alerts: {pending}" + (" · " + ", ".join(f"{name}: {count}" for name,count in errors) if errors else ""))
    lines.append("Dashboard worker: " + (mesh.monitor.dashboard_error or "no recorded refresh error"))
    dashboard = store.setting(guild.id,"@dashboard")
    if dashboard and dashboard["enabled"]:
        lines.append(f"Shared dashboard: https://discord.com/channels/{guild.id}/{dashboard['channel']}/{dashboard.get('message_id')} · interval {dashboard['dashboard_interval']}s")
    setting = store.setting(guild.id,"*")
    if not setting or not setting["enabled"]:
        lines.append("Alerts disabled or unconfigured: use /servers alerts.")
    else:
        try:
            channel = bot.get_channel(setting["channel"]) or await bot.fetch_channel(setting["channel"])
            permissions = verify_channel(channel,guild)
            lines.append(f"Alert channel: {channel.mention} · required permissions OK")
            for value in store.settings():
                if value["guild"] != guild.id or not value.get("role"):
                    continue
                role = guild.get_role(value["role"])
                if role is None:
                    state = "role missing; reconfigure /servers alerts"
                elif not role.mentionable and not permissions.mention_everyone:
                    state = "role cannot be mentioned"
                elif not guild.me.guild_permissions.manage_roles or not role < guild.me.top_role:
                    state = "bot cannot manage subscriptions; check Manage Roles and hierarchy"
                elif role.permissions.value or role.name == bot.state.admin_role_name or any(c.overwrites_for(role).pair()[0].value for c in guild.channels):
                    state = "role grants access or is Mitra's admin role; choose a dedicated subscriber role"
                else:
                    state = "role present and mentionable by bot"
                lines.append(f"`{value['subject']}` subscriptions: {state}")
        except (ValueError,discord.HTTPException) as exc:
            lines.append(f"Alert destination: {exc}")
    return lines
