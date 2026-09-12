"""One permissionless subscription role for all Mitra operational alerts."""
import discord

ALERT_ROLE = "Mitra Alerts"


def safe_role(bot, guild, role):
    return (role is not None and role.id != guild.id and not role.managed
            and role.permissions.value == 0 and role < guild.me.top_role
            and role.name != getattr(bot.state, "admin_role_name", None)
            and not any(c.overwrites_for(role).pair()[0].value for c in guild.channels))


def shared_role(guild):
    roles = [r for r in guild.roles if r.name == ALERT_ROLE]
    if len(roles) > 1:
        raise ValueError("Multiple Mitra Alerts roles exist; reconcile them before continuing.")
    return roles[0] if roles else None


def legacy_roles(bot, guild, roles=None):
    names = {"Mitra IP Subscriber", getattr(bot.state, "ip_subscriber_role_name", "")}
    ids = set()
    mesh = getattr(bot, "peer_service", None)
    if mesh and mesh.monitor:
        ids = {s["role"] for s in mesh.monitor.store.settings()
               if s["guild"] == guild.id and s.get("role")}
    return [r for r in (guild.roles if roles is None else roles) if r.name != ALERT_ROLE and (r.id in ids or r.name in names)]


async def configure_shared_role(bot, guild):
    roles = await guild.fetch_roles()
    matches = [r for r in roles if r.name == ALERT_ROLE]
    if len(matches) > 1:
        raise ValueError("Multiple Mitra Alerts roles exist; reconcile them before continuing.")
    role = matches[0] if matches else await guild.create_role(
        name=ALERT_ROLE, permissions=discord.Permissions.none(), mentionable=True,
        reason="Unified Mitra alert subscriptions")
    if len([r for r in await guild.fetch_roles() if r.name == ALERT_ROLE]) != 1:
        raise ValueError("Concurrent setup created duplicate Mitra Alerts roles; reconcile them first.")
    if not safe_role(bot, guild, role):
        raise ValueError("Mitra Alerts must be below the bot role and have no permissions or channel grants.")
    if not role.mentionable:
        role = await role.edit(mentionable=True, reason="Unified Mitra alert subscriptions")
    old = legacy_roles(bot, guild, roles)
    if any(not safe_role(bot, guild, r) for r in old):
        raise ValueError("An old subscriber role has privileges or cannot be managed; resolve it before migration.")
    if old:
        # Fetch the full membership, not a possibly incomplete gateway cache.
        old_ids = {r.id for r in old}
        async for member in guild.fetch_members(limit=None):
            assigned = [r for r in member.roles if r.id in old_ids]
            if assigned:
                await member.add_roles(role, reason="Preserve existing Mitra alert subscription")
                await member.remove_roles(*assigned, reason="Consolidate Mitra alert subscriptions")
    return role


async def subscription(ctx, subscribe):
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        await ctx.respond("This command can only be used in a server.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)
    try:
        role = shared_role(ctx.guild)
        if role is None:
            raise ValueError("An administrator must configure Mitra Alerts first with /alerts setup.")
        if not safe_role(ctx.bot, ctx.guild, role):
            raise ValueError("Mitra Alerts must have no permissions and be below the bot's role.")
        if subscribe:
            await ctx.author.add_roles(role, reason="Subscribed to all Mitra alerts")
        else:
            old = legacy_roles(ctx.bot, ctx.guild)
            if any(not safe_role(ctx.bot, ctx.guild, r) for r in old):
                raise ValueError("An old subscriber role cannot safely be removed; ask an administrator to finish migration.")
            await ctx.author.remove_roles(role, *old, reason="Unsubscribed from all Mitra alerts")
        await ctx.respond("Subscribed to all Mitra alerts." if subscribe else "Unsubscribed from all Mitra alerts.", ephemeral=True)
    except (ValueError, discord.HTTPException) as exc:
        await ctx.respond(f"Could not change subscription: {exc}", ephemeral=True)
