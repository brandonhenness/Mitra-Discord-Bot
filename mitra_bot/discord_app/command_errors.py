"""Report command failures without exposing exception details to Discord."""
from mitra_bot.discord_app.message_style import notice
import logging

import discord


async def report_command_error(ctx, error):
    original = getattr(error, "original", error)
    logging.error(
        "Command failed: /%s interaction_id=%s",
        getattr(getattr(ctx, "command", None), "qualified_name", "unknown"),
        ctx.interaction.id,
        exc_info=(type(original), original, original.__traceback__),
    )
    message = (
        "This command failed before it could finish. Check the bot log for "
        f"reference `{ctx.interaction.id}`. A partial change may have been made; "
        "check the current settings before retrying."
    )
    try:
        if ctx.interaction.response.is_done():
            await ctx.interaction.edit_original_response(
                content=notice('Command could not finish', message, tone='error'), embeds=[], view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.respond(notice('Command could not finish', message, tone='error'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except (discord.HTTPException, OSError):
        logging.warning("Could not deliver command error for interaction %s", ctx.interaction.id, exc_info=True)
