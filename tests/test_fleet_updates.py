import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from mitra_bot.services import fleet_updates as module
from mitra_bot.services.fleet_updates import FleetUpdates
from mitra_bot.services.update_service import InstallResult
from mitra_bot.discord_app.cogs.update_cog import UpdateCog, UpdatePromptView


async def fleet(path=":memory:"):
    db = sqlite3.connect(path)
    mesh = SimpleNamespace(db=db, config=SimpleNamespace(node_id="a"), peers={"b": None, "c": None},
                           is_state_owner=True, boot_id="old-a", request=AsyncMock())
    bot = SimpleNamespace(is_ready=lambda: True, gateway_connected=True, close=AsyncMock())
    value = FleetUpdates(bot, mesh)
    value.running_version = "1.0.0"
    value.spawn = lambda coroutine: coroutine.close()
    await value.start()
    return value


def test_job_acceptance_is_durable_and_idempotent(tmp_path):
    async def run():
        f = await fleet(tmp_path / "updates.db")
        key = "a" * 32
        job = await f.accept(key, "1.1.0")
        assert await f.accept(key, "1.1.0") == job
        assert len(f.rows("update_jobs")) == 1
        with pytest.raises(ValueError, match="reused"):
            await f.accept(key, "1.2.0")
        with pytest.raises(ValueError, match="active"):
            await f.accept("b" * 32, "1.1.0")
        f.db.close()
        restarted = await fleet(tmp_path / "updates.db")
        assert restarted.status(key)["job"]["state"] == "failed"
        restarted.db.close()
    asyncio.run(run())


def test_progress_message_is_reused_after_plan_saves():
    async def run():
        f = await fleet()
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(id=456, get_partial_message=Mock(return_value=message))
        f.bot.get_channel = Mock(return_value=channel)
        f.bot.http = SimpleNamespace(request=AsyncMock(return_value={"id": "789"}))
        plan = dict(id="a"*32, version="1.1.0", state="running", channel=456, nodes=[dict(node="b", state="pending")])
        f.save("update_plans", plan)
        await f.publish(plan["id"])
        plan["nodes"][0]["phase"] = "installing"
        f.save("update_plans", plan)
        await f.publish(plan["id"])
        assert f.rows("update_plans")[0]["message"] == 789
        f.bot.http.request.assert_awaited_once()
        assert "installing" in message.edit.call_args.kwargs["content"]
        f.db.close()
    asyncio.run(run())


def test_update_maintenance_has_deadline_and_preserves_manual_override():
    async def run():
        f = await fleet()
        values = {}
        def append(kind, value):
            assert kind == "setting"
            values[value["subject"]] = value
        f.mesh.monitor = SimpleNamespace(store=SimpleNamespace(
            setting=lambda guild, subject: values.get(subject), append=append))
        plan = dict(id="a"*32)
        entry = dict(node="b", deadline=module.time.time()+1800)
        await f.maintenance(plan, entry)
        assert values["b"]["maintenance_until"] == entry["deadline"]
        assert values["b"]["maintenance_reason"] == "Rolling update " + plan["id"]
        await f.maintenance(plan, entry, finish=True)
        assert values["b"]["maintenance_until"] == 0
        values["b"].update(maintenance_reason="Hardware work", maintenance_until=module.time.time()+3600)
        await f.maintenance(plan, entry)
        await f.maintenance(plan, entry, finish=True)
        assert values["b"]["maintenance_reason"] == "Hardware work"
        assert values["b"]["maintenance_until"] > entry["deadline"]
        f.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("requested", ["0.9.0", "1.0.0"])
def test_reinstall_and_downgrade_are_rejected(requested):
    async def run():
        f = await fleet()
        with pytest.raises(ValueError, match="downgrade"):
            await f.accept("a" * 32, requested)
        assert not f.rows("update_jobs")
        f.db.close()
    asyncio.run(run())


def test_rpc_rejects_urls_and_extra_parameters():
    async def run():
        f = await fleet()
        with pytest.raises(ValueError, match="only job and version"):
            await f.rpc("update_install", {"job": "a"*32, "version": "1.1.0", "url": "https://untrusted.example"})
        f.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("ok", [True, False])
def test_target_uses_local_release_resolution_and_restarts_only_after_success(monkeypatch, ok):
    async def run():
        f = await fleet()
        release = object()
        resolve = Mock(return_value=release)
        install = Mock(return_value=InstallResult(ok=ok, error=None if ok else "failed"))
        restart = Mock()
        monkeypatch.setattr(module, "resolve_release", resolve)
        monkeypatch.setattr(module.update_service, "install_release", install)
        monkeypatch.setattr(module.update_service, "spawn_replacement_process", restart)
        job = await f.accept("a" * 32, "1.1.0")
        await f.install(job)
        resolve.assert_called_once_with("1.1.0")
        install.assert_called_once_with(release)
        assert restart.call_count == int(ok)
        assert f.status(job["id"])["job"]["state"] == ("restarting" if ok else "failed")
        f.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_rollout_is_sequential_coordinator_last_and_stops_on_failure(monkeypatch, fail):
    async def run():
        f = await fleet()
        started, checked = [], {}
        async def status(node, job_id=None):
            active = node in started
            checked[node] = checked.get(node, 0) + int(active)
            job = {"state": "failed" if fail else "complete", "error": "test failure"} if active else None
            return dict(version="1.1.0" if active else "1.0.0", boot=("new-" if active else "old-")+node,
                        discord=True, capability=1, job=job)
        async def remote(node, operation, payload, **kwargs):
            assert operation == "update_install"
            started.append(node)
            raise OSError("ACK lost after acceptance")
        async def local(operation, payload):
            started.append("a")
        f.node_status = status
        f.mesh.request = remote
        f.rpc = local
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
        plan = await f.begin("all", "1.1.0", 123)
        await f.run_plan(plan)
        assert started == (["b"] if fail else ["b", "c", "a"])
        assert plan["state"] == ("failed" if fail else "complete")
        if not fail:
            assert all(checked[n] >= 2 for n in started)
        f.db.close()
    asyncio.run(run())


def test_missing_peer_blocks_preflight_before_any_jobs():
    async def run():
        f = await fleet()
        f.mesh.request.side_effect = OSError("offline")
        with pytest.raises(ValueError, match="manually first"):
            await f.begin("all", "1.1.0", 123)
        assert not f.rows("update_jobs") and not f.rows("update_plans")
        f.db.close()
    asyncio.run(run())


def test_coordinator_can_finish_its_persisted_final_step_after_restart(tmp_path, monkeypatch):
    async def run():
        f = await fleet(tmp_path / "updates.db")
        job = dict(id="a"*32, version="1.0.0", state="restarting", error=None)
        f.save("update_jobs", job)
        plan = dict(id="b"*32, version="1.0.0", state="running", error=None,
                    nodes=[dict(node="a", job=job["id"], boot="previous-process", state="waiting", deadline=9999999999)])
        f.save("update_plans", plan)
        f.db.close()
        f = await fleet(tmp_path / "updates.db")
        assert f.status(job["id"])["job"]["state"] == "complete"
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
        await f.run_plan(f.rows("update_plans")[0])
        assert f.rows("update_plans")[0]["state"] == "complete"
        f.db.close()
    asyncio.run(run())


def test_member_picker_schema_and_admin_confirmed_target():
    async def run():
        bot = discord.Bot()
        bot.add_cog(UpdateCog(bot))
        group = bot.get_cog("UpdateCog").update
        for command in group.subcommands:
            if command.name in {"check", "install"}:
                option = next(o for o in command.options if o.name == "server")
                assert option.input_type == discord.SlashCommandOptionType.string
                assert option.default == "all"
        bot.state = SimpleNamespace(admin_role_name="Admin")
        bot.fleet_updates = SimpleNamespace(begin=AsyncMock(return_value={"id": "plan"}))
        view = UpdatePromptView(bot.get_cog("UpdateCog"), SimpleNamespace(version="1.1.0"), source="fleet", server="b")
        user = Mock(spec=discord.Member)
        user.roles, user.id = [SimpleNamespace(name="Admin")], 55
        interaction = SimpleNamespace(client=bot, guild=object(), user=user, channel_id=123,
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        await view.children[0].callback(interaction)
        bot.fleet_updates.begin.assert_awaited_once_with("b", "1.1.0", 55, channel_id=123)
        await bot.close()
    asyncio.run(run())


def test_cancelled_rollout_does_not_start_more_nodes(monkeypatch):
    async def run():
        f = await fleet()
        async def status(node, job_id=None):
            return dict(version="1.0.0", boot="old", discord=True, capability=1, job=None)
        f.node_status = status
        plan = await f.begin("all", "1.1.0", 123)
        f.cancel(123)
        await f.run_plan(plan)
        f.mesh.request.assert_not_awaited()
        assert f.rows("update_plans")[0]["state"] == "cancelled"
        f.db.close()
    asyncio.run(run())


def test_restart_timeout_stops_remaining_nodes():
    async def run():
        f = await fleet()
        plan = dict(id="b"*32, version="1.1.0", state="running", error=None,
                    nodes=[dict(node="b", job="a"*32, boot="old", state="waiting", deadline=0),
                           dict(node="a", job="c"*32, boot="old", state="pending", deadline=None)])
        f.save("update_plans", plan)
        await f.run_plan(plan)
        assert plan["state"] == "failed"
        assert plan["nodes"][1]["state"] == "pending"
        f.mesh.request.assert_not_awaited()
        f.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", [None, "draft", "version", "checksum"])
def test_release_resolution_is_pinned_to_local_repository(monkeypatch, bad):
    monkeypatch.setattr(module.update_service, "resolve_github_repo", lambda: "trusted/bot")
    payload = dict(tag_name="v1.1.0", draft=bad == "draft", zipball_url="https://github.com/source.zip",
                   assets=[dict(name="mitra-discord-bot-1.1.0.zip", browser_download_url="https://github.com/trusted/bot/releases/download/v1.1.0/bot.zip", digest="sha256:"+"a"*64)])
    if bad == "version":
        payload["tag_name"] = "v1.2.0"
    if bad == "checksum":
        payload["assets"] = []
    response = Mock()
    response.json.return_value = payload
    get = Mock(return_value=response)
    monkeypatch.setattr(module.requests, "get", get)
    if bad:
        with pytest.raises(ValueError):
            module.resolve_release("1.1.0")
    else:
        assert module.resolve_release("1.1.0").version == "v1.1.0"
    get.assert_called_once_with("https://api.github.com/repos/trusted/bot/releases/tags/v1.1.0", timeout=30)


def test_up_to_date_owner_still_offers_update_for_older_peer(monkeypatch):
    from mitra_bot.discord_app.cogs import update_cog
    async def run():
        bot = SimpleNamespace(state=SimpleNamespace(admin_role_name="Admin"))
        bot.fleet_updates = SimpleNamespace(preview=AsyncMock(return_value={
            "b": dict(version="1.0.0"), "a": dict(version="1.1.0")}))
        actor = Mock(spec=discord.Member)
        actor.roles = [SimpleNamespace(name="Admin")]
        ctx = SimpleNamespace(guild=object(), author=actor, bot=bot, defer=AsyncMock(), respond=AsyncMock())
        release = SimpleNamespace(version="1.1.0", html_url="https://github.com/trusted/bot/releases/tag/v1.1.0")
        monkeypatch.setattr(update_cog, "check_latest_release", lambda: SimpleNamespace(error=None, release=release, available=False))
        cog = UpdateCog(bot)
        await cog.fleet_prompt(ctx, "all")
        view = ctx.respond.call_args.kwargs["view"]
        assert isinstance(view, UpdatePromptView) and view.server == "all"
        view.stop()
    asyncio.run(run())
