"""Explicit, owner-authorized repair of one peer's infrastructure allowlist."""
from mitra_bot.discord_app.access import PUBLIC_COMMANDS
from mitra_bot.services.peer_service import PeerError
from mitra_bot.storage import config_store


def apply_peer_access(bot, payload):
    if set(payload) != {"infrastructure_guild_ids"}:
        raise ValueError("Only infrastructure_guild_ids may be synchronized")
    ids = config_store.BotFileConfigModel(**payload).infrastructure_guild_ids
    if ids is None:
        raise ValueError("An explicit list of Discord server IDs is required")
    ids = sorted(set(ids))
    path = config_store.get_config_path()
    # Validate the existing file, then preserve unrelated and extension settings.
    config_store.read_config_dict()
    raw = config_store.tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw.setdefault("bot", {})["infrastructure_guild_ids"] = ids
    config_store.FileConfigModel.model_validate(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    config_store._atomic_write_text(path, config_store.tomli_w.dumps(raw))
    # Persistence must succeed before granting access in the running process.
    bot.state.infrastructure_guild_ids = tuple(ids)
    for command in [*bot.pending_application_commands, *bot.application_commands]:
        if command.name not in PUBLIC_COMMANDS:
            command.guild_ids = list(ids)
    return {"infrastructure_guild_ids": ids, "saved": True}


async def sync_peer_access(bot, target):
    mesh = bot.peer_service
    if not mesh or not mesh.is_state_owner:
        raise PeerError("Run this operation on the configured shared settings owner")
    target = mesh.resolve(target)
    if target == mesh.config.node_id:
        raise PeerError("Choose a remote peer; the owner's local configuration is the source")
    capabilities = await mesh.request(target, "capabilities", {})
    if "infrastructure_access" not in capabilities.get("operations", []):
        raise PeerError("Update the target to a release supporting access synchronization first")
    ids = sorted(set(bot.state.infrastructure_guild_ids))
    result = await mesh.request(target, "infrastructure_access", {"infrastructure_guild_ids": ids})
    if result.get("saved") is not True or result.get("infrastructure_guild_ids") != ids:
        raise PeerError("Access synchronization was not confirmed; inspect the target before retrying")
    return ids
