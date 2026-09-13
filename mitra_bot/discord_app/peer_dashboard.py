"""Reconstructible read-only dashboard controls; no process-local view callbacks."""
from __future__ import annotations
from mitra_bot.discord_app.message_style import embed as styled_embed
from mitra_bot.discord_app.message_style import notice

import asyncio
import hashlib
import time

import discord

from mitra_bot.discord_app.interaction_routing import claim_interaction, response_delay
from mitra_bot.services.peer_graph import metrics, render_history
from mitra_bot.services.role_manager import member_has_role
from mitra_bot.discord_app.access import infrastructure_guild


def tag(node):
    return hashlib.sha256(node.encode()).hexdigest()[:12]


def dashboard_view(mesh, subject, observer, hours, page, advanced=False):
    view = discord.ui.View(timeout=None)
    def custom(window, number):
        return f"mitra-health:{tag(subject) if subject else 'all'}:{tag(observer)}:{window}:{number}" + (":advanced" if advanced else "")
    for window in (1, 6, 24, 168):
        view.add_item(discord.ui.Button(label=f"{window}h" if window < 168 else "7 days",
                                      style=discord.ButtonStyle.primary if window == hours else discord.ButtonStyle.secondary,
                                      custom_id=custom(window, page)))
    view.add_item(discord.ui.Button(label="Refresh", custom_id=custom(hours, page)+":refresh"))
    if subject is None:
        pages = (len(mesh.peers)+1+7)//8
        view.add_item(discord.ui.Button(label="Previous", custom_id=custom(hours, max(0,page-1))+":previous",
                                      disabled=page == 0, row=1))
        view.add_item(discord.ui.Button(label="Next", custom_id=custom(hours, min(pages-1,page+1))+":next",
                                      disabled=page >= pages-1, row=1))
    view.add_item(discord.ui.Button(label="Hide monitoring perspective" if advanced else "Monitoring perspective",
                                   custom_id=custom(hours, page)+":perspective", row=1))
    # Selectors paginate naturally with dashboard pages; slash options accept every node.
    nodes = sorted([mesh.config.node_id, *mesh.peers])[page*8:page*8+8]
    for kind, selected in (("server", subject), ("observer", observer)):
        if kind == "observer" and not advanced:
            continue
        label = "Show" if kind == "server" else "Measured from"
        choices = nodes + ([selected] if selected and selected not in nodes else [])
        options = [discord.SelectOption(label=f"{label}: {node}", value=tag(node), default=node == selected) for node in choices]
        if kind == "server":
            options.insert(0, discord.SelectOption(label="Show: All servers", value="all", default=subject is None))
        view.add_item(discord.ui.Select(placeholder=f"{label} (page {page+1})", options=options,
                                      custom_id=custom(hours,page)+f":{kind}", row=2 if kind == "server" else 3))
    view.stop()
    return view


async def build_dashboard(mesh, subject=None, observer=None, hours=24, page=0, advanced=False):
    observer = mesh.resolve(observer or mesh.config.node_id)
    if subject:
        mesh.resolve(subject)
    nodes = [subject] if subject else sorted([mesh.config.node_id, *mesh.peers])[page*8:page*8+8]
    if not nodes or not 1 <= hours <= 2160:
        raise ValueError("Invalid dashboard page or window")
    end = time.time()
    start = end-hours*3600
    store = mesh.monitor.store
    series = {node: store.series(observer, node, start, end) for node in nodes}
    embed = styled_embed(title=f"Server {'status' if subject else 'dashboard'} · {hours}h", color=discord.Color.blue())
    embed.description = f"Observed by **{observer}** · responding instance **{mesh.config.node_id}**\nAvailability is based on sampled peer reachability."
    gap = store.db.execute("SELECT gap FROM health_cursors WHERE observer=?", (observer,)).fetchone()
    if gap and gap[0]:
        embed.description += "\nSome source records expired before replication; missing coverage stays unknown."
    for node, rows in series.items():
        latest = store.latest(observer, node)
        last_good = store.last_success(observer, node)
        fresh = latest and 0 <= end-latest["ts"] <= mesh.config.health_interval*2.5
        state = latest["state"] if fresh else "unknown (observer data stale or missing)"
        stats = metrics(rows, start, end)
        availability = f"{stats['availability']:.2f}%" if stats["availability"] is not None else "Unknown"
        value = f"**{state}**\nAvailability {availability} · coverage {stats['coverage']:.1f}%"
        maintenance = mesh.monitor.maintenance(node)
        if maintenance:
            value += f"\nMaintenance until <t:{int(maintenance['maintenance_until'])}:R>: {maintenance['maintenance_reason']}"
        if latest:
            gateway = ("connected" if latest["gateway"] else "disconnected") if fresh and latest["gateway"] is not None else "unknown / stale"
            value += f"\nLast observation <t:{int(latest['ts'])}:R> · Discord {gateway}"
            if last_good:
                value += f"\nLast peer response <t:{int(last_good['ts'])}:R>"
                if last_good["uptime"] is not None:
                    value += f"\nProcess uptime (last report): {last_good['uptime']//3600}h {last_good['uptime']//60%60}m"
            if latest["skew"]:
                value += "\nRemote clock differs by over 60s."
        embed.add_field(name=node, value=value, inline=False)
    restarts = []
    if subject:
        last_boot = None
        for boot, ts in store.db.execute("""SELECT json_extract(value,'$.boot'), MIN(ts) FROM health_events
            WHERE observer=? AND kind='sample' AND json_extract(value,'$.subject')=? AND ts>=? AND ts<=?
            GROUP BY json_extract(value,'$.boot') ORDER BY MIN(ts)""", (observer, subject, start, end)):
            if not boot:
                continue
            if last_boot and boot != last_boot:
                restarts.append(ts)
            last_boot = boot
    graph = await asyncio.to_thread(render_history, series, observer, start, end, detail=bool(subject), restarts=restarts)
    embed.set_image(url="attachment://peer-health.png")
    embed.set_footer(text=f"UTC · 5-minute stored buckets (long graphs aggregate) · page {page+1}")
    return dict(embed=embed, file=discord.File(graph, filename="peer-health.png"),
                view=dashboard_view(mesh, subject, observer, hours, page, advanced))


async def handle_dashboard_component(bot, interaction):
    mesh = bot.peer_service
    if not await claim_interaction(interaction, delay=response_delay(mesh, mesh.config.resolved_state_owner, interaction.id)):
        return
    if (not infrastructure_guild(bot, interaction.guild) or not isinstance(interaction.user, discord.Member)
            or not member_has_role(interaction.user, bot.state.admin_role_name)):
        await interaction.followup.send(notice('Permission required', "You do not have permission to view this dashboard.", tone='warning'), ephemeral=True)
        return
    try:
        if interaction.message.author.id != bot.user.id:
            raise ValueError("Invalid message")
        parts = interaction.data["custom_id"].split(":")
        _, subject_tag, observer_tag, window, page_text, *action = parts
        advanced = "advanced" in action
        action = [part for part in action if part != "advanced"]
        if action == ["perspective"]:
            advanced = not advanced
        if action and action[0] in ("server", "observer"):
            selected = interaction.data["values"][0]
            if action[0] == "server":
                subject_tag = selected
            else:
                observer_tag = selected
        nodes = {tag(n): n for n in [mesh.config.node_id, *mesh.peers]}
        subject = None if subject_tag == "all" else nodes[subject_tag]
        observer = nodes[observer_tag]
        hours, page = int(window), int(page_text)
        if not 0 <= page <= len(mesh.peers)//8:
            raise ValueError("Invalid page")
        # A fresh ephemeral response works even after the original interaction token expires.
        payload = await build_dashboard(mesh, subject, observer, hours, page, advanced=advanced)
        await interaction.followup.send(**payload, ephemeral=True)
    except (ValueError, KeyError, IndexError):
        await interaction.followup.send(notice('Server dashboard', "This dashboard control is invalid or membership changed. Run /servers dashboard again.", tone='info'), ephemeral=True)
