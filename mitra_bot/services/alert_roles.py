"""One permissionless subscription role for all Mitra operational alerts."""
from mitra_bot.discord_app.message_style import notice
import discord
from mitra_bot.discord_app.access import infrastructure_guild

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
    if not infrastructure_guild(bot, guild):
        raise ValueError("Infrastructure alerts are not available in this Discord server")
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


async def subscription(ctx, subscribe, user=None):
    if not infrastructure_guild(ctx.bot, ctx.guild):
        await ctx.respond(notice('Command unavailable', 'Infrastructure alerts are not available in this Discord server.', tone='warning'), ephemeral=True)
        return
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        await ctx.respond(notice('Use this command in Discord', "This command can only be used in a server.", tone='warning'), ephemeral=True)
        return
    if user is not None:
        from mitra_bot.discord_app.checks import ensure_admin
        guard = ensure_admin(ctx)
        if guard:
            await guard
            return
        if not isinstance(user, discord.Member) or user.guild.id != ctx.guild.id:
            await ctx.respond(notice('Alert subscriptions', "Choose a member of this Discord server.", tone='info'), ephemeral=True)
            return
    target = ctx.author if user is None else user
    reason = "Subscribed to all Mitra alerts" if subscribe else "Unsubscribed from all Mitra alerts"
    if user is not None:
        reason += f" by administrator {ctx.author.id}"
    await ctx.defer(ephemeral=True)
    try:
        role = shared_role(ctx.guild)
        if role is None:
            raise ValueError("An administrator must configure Mitra Alerts first with /alerts setup.")
        if not safe_role(ctx.bot, ctx.guild, role):
            raise ValueError("Mitra Alerts must have no permissions and be below the bot's role.")
        if subscribe:
            await target.add_roles(role, reason=reason)
        else:
            old = legacy_roles(ctx.bot, ctx.guild)
            if any(not safe_role(ctx.bot, ctx.guild, r) for r in old):
                raise ValueError("An old subscriber role cannot safely be removed; ask an administrator to finish migration.")
            await target.remove_roles(role, *old, reason=reason)
        message = "Subscribed to all Mitra alerts." if subscribe else "Unsubscribed from all Mitra alerts."
        if user is not None:
            message = f"{target.mention}: {message}"
        await ctx.respond(notice('Subscription updated', message, tone='success'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except (ValueError, discord.HTTPException) as exc:
        await ctx.respond(notice('Action could not finish', f"Could not change subscription: {exc}", tone='error'), ephemeral=True)
