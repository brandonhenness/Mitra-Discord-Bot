import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.recover_peer_access import recover
from mitra_bot.services.peer_service import PeerError


def mesh(responses):
    return SimpleNamespace(is_state_owner=True, config=SimpleNamespace(node_id="Mitra", network_id="test"),
                           resolve=lambda target: target, request=AsyncMock(side_effect=responses))


def status(version="0.2.2", boot="old", state=None, discord=True):
    return dict(version=version, boot=boot, discord=discord, job={"state": state} if state else None)


def test_recovery_updates_then_waits_for_restart_before_sync():
    peer = mesh([status(), {"state": "accepted"}, status(state="installing"),
                 status("0.2.3", "new", "complete"), status("0.2.3", "new", "complete"),
                 {"operations": ["infrastructure_access"]}, {"saved": True, "infrastructure_guild_ids": [1]}])
    asyncio.run(recover(peer, (1,), "Anubis", poll_seconds=0))
    calls = peer.request.await_args_list
    installs = [c for c in calls if c.args[1] == "update_install"]
    assert len(installs) == 1
    assert installs[0].args[2]["version"] == "0.2.3"
    job = installs[0].args[2]["job"]
    assert all(c.args[2]["job"] == job for c in calls if c.args[1] == "update_status")
    assert calls[-1].args == ("Anubis", "infrastructure_access", {"infrastructure_guild_ids": [1]})


def test_lost_ack_polls_same_job_without_retrying_install():
    peer = mesh([status(), PeerError("lost ack"), status("0.2.3", "new", "complete"),
                 status("0.2.3", "new", "complete"), {"operations": ["infrastructure_access"]},
                 {"saved": True, "infrastructure_guild_ids": [1]}])
    asyncio.run(recover(peer, (1,), "Anubis", poll_seconds=0))
    assert sum(c.args[1] == "update_install" for c in peer.request.await_args_list) == 1


def test_already_updated_peer_is_not_reinstalled():
    peer = mesh([status("0.2.3"), status("0.2.3"), status("0.2.3"),
                 {"operations": ["infrastructure_access"]}, {"saved": True, "infrastructure_guild_ids": [1]}])
    asyncio.run(recover(peer, (1,), "Anubis", poll_seconds=0))
    assert not any(c.args[1] == "update_install" for c in peer.request.await_args_list)


@pytest.mark.parametrize("initial", [status(state="failed"), status(state="complete")])
def test_failed_or_inconsistent_previous_job_stops_recovery(initial):
    peer = mesh([initial])
    with pytest.raises(PeerError):
        asyncio.run(recover(peer, (1,), "Anubis", poll_seconds=0))
    assert peer.request.await_count == 1


def test_non_owner_cannot_send_any_request():
    peer = mesh([])
    peer.is_state_owner = False
    with pytest.raises(PeerError):
        asyncio.run(recover(peer, (1,), "Anubis", poll_seconds=0))
    peer.request.assert_not_awaited()
