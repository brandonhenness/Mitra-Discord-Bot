import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from mitra_bot.discord_app.bot_factory import MitraBot
from mitra_bot.discord_app.interaction_routing import ClaimedContext, claim_interaction, command_route, response_delay
from mitra_bot.discord_app.peer_power import PowerIntent, decode_intent, handle_power_component, render_intent
from mitra_bot.peer_config import PeerConfig, Peer
from mitra_bot.services.peer_service import PeerError, PeerService


def peer(node="a"):
    cfg = PeerConfig(node_id=node, state_owner="a", network_id="test",
                     peers=[Peer(node_id=other, host="localhost", fingerprint=other * 64) for other in "abc" if other != node])
    result = PeerService(cfg, snapshot=lambda: {}, power=AsyncMock())
    result.configure_discord_identity("one-token")
    return result


class AckServer:
    def __init__(self, lose_reply=False):
        self.ids = set()
        self.lose_reply = lose_reply

    def interaction(self, interaction_id=123, *, data=None, component=False):
        responded = False

        async def defer(**kwargs):
            nonlocal responded
            if interaction_id in self.ids:
                raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad request"),
                                            {"code": 40060, "message": "Already acknowledged"})
            self.ids.add(interaction_id)
            if self.lose_reply:
                raise OSError("ACK succeeded, but response was lost")
            responded = True

        return SimpleNamespace(
            guild=SimpleNamespace(id=3),
            id=interaction_id, created_at=datetime.now(timezone.utc),
            type=discord.InteractionType.component if component else discord.InteractionType.application_command,
            data=data or {"name": "servers", "options": [{"name": "list", "type": 1}]},
            response=SimpleNamespace(defer=defer, is_done=lambda: responded),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        )


def test_only_ack_winner_invokes_command_across_three_instances():
    async def run():
        arbiter = AckServer()
        bots = [MitraBot(intents=discord.Intents.none()) for _ in "abc"]
        for bot in bots:
            bot.state = SimpleNamespace(infrastructure_guild_ids=(3,))
        for node, bot in zip("abc", bots):
            bot.peer_service = peer(node)
        ran = []

        async def callback(bot, interaction, auto_sync=None):
            ran.append(bot.peer_service.config.node_id)

        try:
            with patch("discord.Bot.process_application_commands", new=callback), patch("mitra_bot.discord_app.bot_factory.response_delay", return_value=0):
                await asyncio.gather(*(bot.process_application_commands(arbiter.interaction()) for bot in bots))
            assert len(ran) == 1
        finally:
            for bot in bots:
                await bot.close()
    asyncio.run(run())


def test_ambiguous_ack_never_runs_work_on_any_instance():
    async def run():
        arbiter = AckServer(lose_reply=True)
        results = await asyncio.gather(*(claim_interaction(arbiter.interaction()) for _ in range(3)))
        assert results == [False, False, False]
    asyncio.run(run())


def test_expired_interaction_does_not_attempt_ack():
    async def run():
        event = AckServer().interaction()
        event.created_at -= timedelta(seconds=4)
        event.response.defer = AsyncMock()
        assert not await claim_interaction(event)
        event.response.defer.assert_not_awaited()
    asyncio.run(run())


def test_survivor_handles_monitoring_but_does_not_write_missing_owners_state():
    async def run():
        bot = MitraBot(intents=discord.Intents.none())
        bot.state = SimpleNamespace(infrastructure_guild_ids=(3,))
        bot.peer_service = peer("b")
        parent = AsyncMock()
        arbiter = AckServer()
        try:
            with patch("discord.Bot.process_application_commands", parent), patch("mitra_bot.discord_app.bot_factory.response_delay", return_value=0):
                await bot.process_application_commands(arbiter.interaction())
                parent.assert_awaited_once()
                parent.reset_mock()
                event = arbiter.interaction(124, data={"name": "todo"})
                await bot.process_application_commands(event)
                parent.assert_not_awaited()
                assert "no change" in event.followup.send.call_args.args[0]
        finally:
            await bot.close()
    asyncio.run(run())


def test_routing_preserves_target_and_keeps_all_fallback_delays_bounded():
    mesh = peer("b")
    data = {"name": "power", "options": [{"name": "restart", "type": 1, "options": [{"name": "server", "value": "c"}]}]}
    assert command_route(data, mesh) == ("c", True)
    assert command_route({"name": "notifications"}, mesh) == ("a", False)
    assert mesh.resolve(None) == "a"
    assert 0.4 <= response_delay(mesh, "c", 123) < 1.1
    assert response_delay(mesh, "b", 123) == 0


def test_claimed_context_defer_is_idempotent():
    async def run():
        event = AckServer().interaction()
        assert await claim_interaction(event)
        ctx = object.__new__(ClaimedContext)
        ctx.interaction = event
        await ctx.defer(ephemeral=True)
    asyncio.run(run())


def intent():
    return PowerIntent(network="test", operation_id="a" * 32, server="b", action="restart", delay_seconds=60,
                       force=False, requester_id=1, channel_id=2, guild_id=3,
                       expires_at=int(datetime.now(timezone.utc).timestamp()) + 600)


def confirmation(mesh, *, signer=None, decision="confirm"):
    embed, view = render_intent(intent(), (signer or mesh).signing_key)
    item = next(item for item in view.children if item.label.lower() == decision)
    event = AckServer().interaction(component=True, data={"custom_id": item.custom_id})
    event.message = SimpleNamespace(author=SimpleNamespace(id=99), embeds=[embed])
    event.channel_id = 2
    event.guild = SimpleNamespace(id=3)
    event.client = SimpleNamespace(user=SimpleNamespace(id=99))
    event.user = Mock(spec=discord.Member)
    event.user.id = 1
    event.user.roles = [SimpleNamespace(name="Mitra Admin")]
    return event


def test_signed_confirmation_survives_origin_loss_and_routes_to_original_target():
    async def run():
        a, c = peer("a"), peer("c")
        c.power = AsyncMock(return_value="scheduled")
        event = confirmation(c, signer=a)
        bot = SimpleNamespace(peer_service=c, state=SimpleNamespace(admin_role_name="Mitra Admin", infrastructure_guild_ids=(3,)))
        with patch("mitra_bot.discord_app.peer_power.response_delay", return_value=0):
            await handle_power_component(bot, event)
        args = c.power.call_args
        assert args.args[0] == "b"
        assert args.kwargs["operation_id"] == intent().operation_id
        assert args.args[1].action == "restart"
        event.edit_original_response.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["signature", "channel", "author", "network", "expired", "target", "decision"])
def test_tampered_or_expired_confirmation_is_rejected(change):
    async def run():
        mesh = peer()
        event = confirmation(mesh)
        if change == "signature":
            event.data["custom_id"] += "x"
        elif change == "channel":
            event.channel_id = 12
        elif change == "author":
            event.message.author.id = 12
        elif change == "network":
            mesh.config.network_id = "other"
        elif change == "decision":
            event.data["custom_id"] = event.data["custom_id"].replace(":confirm:", ":cancel:")
        elif change == "target":
            parts = event.data["custom_id"].split(":")
            parts[2] = ("B" if parts[2][0] != "B" else "C") + parts[2][1:]
            event.data["custom_id"] = ":".join(parts)
        else:
            old = intent().model_copy(update={"expires_at": 1})
            embed, view = render_intent(old, mesh.signing_key)
            event.message.embeds = [embed]
            event.data["custom_id"] = view.children[0].custom_id
        with pytest.raises(PeerError):
            decode_intent(mesh, event)
    asyncio.run(run())


def test_signed_confirmation_admin_denial_has_no_power_effect():
    async def run():
        mesh = peer()
        mesh.power = AsyncMock()
        event = confirmation(mesh)
        event.user.roles = []
        bot = SimpleNamespace(peer_service=mesh, state=SimpleNamespace(admin_role_name="Mitra Admin", infrastructure_guild_ids=(3,)))
        with patch("mitra_bot.discord_app.peer_power.response_delay", return_value=0):
            await handle_power_component(bot, event)
        mesh.power.assert_not_awaited()
        assert "admin role" in event.followup.send.call_args.args[0]
    asyncio.run(run())


def test_confirmation_buttons_fit_discord_limit_and_are_not_local_callbacks():
    async def run():
        _, view = render_intent(intent(), peer().signing_key)
        assert view.is_finished()
        assert all(len(button.custom_id) <= 100 for button in view.children)
    asyncio.run(run())
