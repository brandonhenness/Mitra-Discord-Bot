"""Discord delivery adapter. Never uses Discord as a distributed action lock."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import discord
from discord.http import Route


class PeerAlertDelivery:
    def __init__(self, bot, mesh):
        self.bot, self.mesh = bot, mesh

    async def __call__(self, key, kind, incident, setting, role_setting):
        if not self.bot.is_ready() or not self.bot.gateway_connected:
            raise RuntimeError("Discord is disconnected")
        channel = self.bot.get_channel(setting["channel"]) or await self.bot.fetch_channel(setting["channel"])
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != setting["guild"]:
            raise ValueError("Alert destination must be a text channel in the configured guild")
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
        if summary:
            title = f"{subject}: recovered outage (delayed report)"
        elif kind == "recovery":
            title = f"{subject}: connection recovered"
        else:
            title = f"{subject}: {'unreachable' if incident['kind'] == 'peer' else 'Discord disconnected'}"
        description = (f"Observed by **{observer}**.\n"
                       f"First failure: <t:{int(incident['start'])}:F>\n"
                       f"Confirmed: <t:{int(incident['detected'])}:F>\n")
        if incident["last_success"] is not None:
            description += f"Last successful contact: <t:{int(incident['last_success'])}:F>\n"
        if incident["recovered"] is not None:
            description += f"Recovery confirmed: <t:{int(incident['recovered'])}:F>\n"
        description += ("The machine, Mitra process, or network connection may be unavailable."
                        if incident["kind"] == "peer" and incident["recovered"] is None
                        else "Peer reachability and Discord connectivity are monitored separately.")
        embed = discord.Embed(title=title, description=description,
                              color=discord.Color.green() if incident["recovered"] else discord.Color.orange(),
                              timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=prefix + json.dumps(dict(key=exact, phase=phase, start=incident["start"],
                                                      end=incident["recovered"]), separators=(",", ":")))
        # py-cord's high-level send does not expose enforce_nonce in all supported versions.
        # Its authenticated HTTP client preserves Discord rate-limit handling.
        payload = dict(content=role.mention if role else "", embeds=[embed.to_dict()], nonce=exact,
                       enforce_nonce=True, allowed_mentions={"parse": [], "roles": [str(role.id)] if role else [], "users": []})
        data = await self.bot.http.request(Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id), json=payload)
        return data["id"]
