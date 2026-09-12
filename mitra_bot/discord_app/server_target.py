from mitra_bot.services.peer_service import PeerError


def resolve_server(bot, server):
    mesh = getattr(bot, "peer_service", None)
    if mesh is not None:
        return mesh.resolve(server)
    if server not in (None, "local"):
        raise PeerError("Private networking is disabled; only 'local' is available.")
    return "local"
