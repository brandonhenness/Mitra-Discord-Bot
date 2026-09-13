"""Discord delivery adapter. Never uses Discord as a distributed action lock."""
from __future__ import annotations
from mitra_bot.discord_app.message_style import embed as styled_embed

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import discord
from discord.http import Route

ALERT_DETAILS_URL = "https://github.com/brandonhenness/Mitra-Discord-Bot/blob/main/docs/operations-recovery.md"


class PeerAlertDelivery:
    def __init__(self, bot, mesh):
        self.bot, self.mesh = bot, mesh

    async def __call__(self, key, kind, incident, setting, role_setting):
        if not self.bot.is_ready() or not self.bot.gateway_connected:
            raise RuntimeError("Discord is disconnected")
        channel = self.bot.get_channel(setting["channel"]) or await self.bot.fetch_channel(setting["channel"])
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != setting["guild"]:
            raise ValueError("Alert destination must be a text channel in the configured guild")
        role_setting = setting if setting.get("role") else role_setting
        subject = incident["subject"]
        network = hashlib.sha256(self.mesh.config.network_id.encode()).hexdigest()[:16]
        prefix = f"mitra-health:{network}:{subject}:{incident['kind']}:"
        summary = kind == "outage" and incident["recovered"] is not None
        phase = "summary" if summary else kind
        exact = hashlib.sha256(key.encode()).hexdigest()[:24]
        # Cross-observer reports identify an overlapping episode, not a global verdict.
        # Read failure defers delivery; never blindly retry an ambiguous send.
        async for message in channel.history(limit=100):
            if message.author.id != self.bot.user.id:
                continue
            for embed in message.embeds:
                footer = getattr(embed.footer, "text", None) or ""
                # New alerts carry delivery metadata in a URL fragment, not visible
                # footer text. Continue recognizing alerts sent by older releases.
                if not footer.startswith(prefix) and (embed.url or "").startswith(ALERT_DETAILS_URL + "#"):
                    footer = unquote(embed.url.split("#", 1)[1])
                if not footer.startswith(prefix):
                    continue
                try:
                    saved = json.loads(footer[len(prefix):])
                except (ValueError, TypeError):
                    continue
                if saved.get("key") == exact:
                    return message.id
                # Only suppress matching phases whose observed intervals overlap.
                end = incident["recovered"] or time.time()
                old_end = saved.get("end") or message.created_at.timestamp()
                overlap = saved.get("start", end+1) <= end and old_end >= incident["start"]
                if saved.get("phase") == phase and overlap:
                    return message.id
                if saved.get("phase") == phase and time.time()-message.created_at.timestamp() < self.mesh.config.health_cooldown:
                    # Preserve subsequent episodes in history but rate-limit mentions during flapping.
                    role_setting = None
        role_id = role_setting.get("role") if role_setting and role_setting["enabled"] else None
        role = channel.guild.get_role(role_id) if role_id else None
        if role_id and role is None:
            raise ValueError("Subscriber role was deleted; configure a replacement with /servers alerts")
        if role and role.id == channel.guild.id:
            raise ValueError("Everyone cannot be a subscriber role")
        observer = self.mesh.config.node_id
        peer = incident["kind"] == "peer"
        recovered = incident["recovered"] is not None
        if summary:
            title = (f"🟢 Connection to {subject} restored (delayed report)" if peer
                     else f"🟢 {subject} reconnected to Discord (delayed report)")
        elif kind == "recovery":
            title = f"🟢 Connection to {subject} restored" if peer else f"🟢 {subject} reconnected to Discord"
        else:
            title = f"🔴 Lost contact with {subject}" if peer else f"🟠 {subject} lost its Discord connection"
        if recovered:
            description = "Peer contact has been restored." if peer else "The bot has reconnected to Discord."
        else:
            description = ("The observing server cannot reach this peer. The machine, bot process, or network may be unavailable."
                           if peer else "The peer reported that its bot connection to Discord was lost.")
        embed = styled_embed(title=title, description=description,
                              color=discord.Color.green() if recovered else discord.Color.red() if peer else discord.Color.orange(),
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Monitoring", value="Peer connection" if peer else "Discord connection", inline=True)
        embed.add_field(name="Observed by", value=observer, inline=True)
        if recovered:
            seconds = max(0, int(incident["recovered"] - incident["start"]))
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            embed.add_field(name="Observed interruption", value=duration, inline=True)
        timeline = (f"First failure · <t:{int(incident['start'])}:f>\n"
                    f"Outage confirmed · <t:{int(incident['detected'])}:f>")
        if recovered:
            timeline += f"\nRecovery confirmed · <t:{int(incident['recovered'])}:f>"
        embed.add_field(name="Timeline", value=timeline, inline=False)
        if incident["last_success"] is not None:
            embed.add_field(name="Last successful contact before interruption",
                            value=f"<t:{int(incident['last_success'])}:f>", inline=False)
        embed.set_footer(text="Mitra • Peer and Discord connectivity are monitored separately")
        marker = prefix + json.dumps(dict(key=exact, phase=phase, start=incident["start"],
                                          end=incident["recovered"]), separators=(",", ":"))
        embed.url = ALERT_DETAILS_URL + "#" + quote(marker, safe="")
        # py-cord's high-level send does not expose enforce_nonce in all supported versions.
        # Its authenticated HTTP client preserves Discord rate-limit handling.
        payload = dict(content=role.mention if role else "", embeds=[embed.to_dict()], nonce=exact,
                       enforce_nonce=True, allowed_mentions={"parse": [], "roles": [str(role.id)] if role else [], "users": []})
        data = await self.bot.http.request(Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id), json=payload)
        return data["id"]
