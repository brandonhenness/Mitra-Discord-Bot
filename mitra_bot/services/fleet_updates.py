"""Durable, owner-coordinated rolling updates over authenticated peer RPCs."""
import asyncio
import json
import logging
import re
import time
import uuid
from urllib.parse import quote

import requests

from packaging.version import Version

from mitra_bot import __version__
from mitra_bot.services import update_service


def resolve_release(version):
    """Resolve a confirmed version through this node's own trusted repository."""
    repo = update_service.resolve_github_repo()
    if not repo:
        raise ValueError("No GitHub repository configured")
    tag = "v" + str(Version(version))
    response = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    release = update_service._release_info_from_payload(payload, repo)
    if payload.get("draft") or not release or Version(release.version) != Version(version):
        raise ValueError("Requested published release was not found")
    if not (release.sha256 or release.checksum_url):
        raise ValueError("Rolling updates require a release with a verified deployment artifact")
    return release


class FleetUpdates:
    def __init__(self, bot, mesh):
        self.bot, self.mesh, self.db = bot, mesh, mesh.db
        self.tasks = set()
        self.running_version = __version__

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def save(self, table, value):
        with self.db:
            self.db.execute(f"INSERT OR REPLACE INTO {table}(id,value) VALUES (?,?)",
                            (value["id"], json.dumps(value)))

    def rows(self, table):
        return [json.loads(r[0]) for r in self.db.execute(f"SELECT value FROM {table} ORDER BY rowid DESC")]

    async def start(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS update_jobs (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS update_plans (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for job in self.rows("update_jobs"):
            if job["state"] in {"accepted", "installing", "restarting"}:
                job["state"] = "complete" if job["state"] == "restarting" and Version(self.running_version) == Version(job["version"]) else "failed"
                job["error"] = None if job["state"] == "complete" else "Interrupted update; inspect local recovery files before retrying."
                self.save("update_jobs", job)
        if self.mesh.is_state_owner:
            for plan in self.rows("update_plans"):
                if plan["state"] == "running":
                    self.spawn(self.run_plan(plan))

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def status(self, job_id=None):
        job = next((j for j in self.rows("update_jobs") if j["id"] == job_id), None) if job_id else None
        return {"version": self.running_version, "boot": self.mesh.boot_id,
                "discord": bool(self.bot.is_ready() and self.bot.gateway_connected), "job": job,
                "capability": 1}

    async def rpc(self, operation, payload):
        if operation == "update_status":
            if set(payload) - {"job"} or (payload.get("job") is not None and not re.fullmatch(r"[a-f0-9]{32}", str(payload["job"]))):
                raise ValueError("Invalid update status request")
            return self.status(payload.get("job"))
        if set(payload) != {"job", "version"}:
            raise ValueError("An update request must contain only job and version")
        return await self.accept(payload["job"], payload["version"])

    async def accept(self, job_id, version):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id) or not isinstance(version, str) or len(version) > 64:
            raise ValueError("Invalid update request")
        requested = Version(version)
        jobs = self.rows("update_jobs")
        previous = next((j for j in jobs if j["id"] == job_id), None)
        if previous:
            if Version(previous["version"]) != requested:
                raise ValueError("Update job ID reused for another release")
            return previous
        if any(j["state"] in {"accepted", "installing", "restarting"} for j in jobs):
            raise ValueError("Another update is active on this node")
        if requested <= Version(self.running_version):
            raise ValueError("Refusing reinstall or downgrade")
        job = dict(id=job_id, version=version, state="accepted", error=None)
        self.save("update_jobs", job)  # Commit before scheduling; retries reuse this ID.
        self.spawn(self.install(job))
        return job

    async def install(self, job):
        try:
            # The administrator confirmed this exact version, including a beta.
            # Discovery policy remains on the coordinator; no arbitrary URLs cross RPC.
            release = await asyncio.to_thread(resolve_release, job["version"])
            job["state"] = "installing"
            self.save("update_jobs", job)
            result = await asyncio.to_thread(update_service.install_release, release)
            if not result.ok:
                raise ValueError(result.error or "Installation failed")
            job["state"] = "restarting"
            self.save("update_jobs", job)
            update_service.spawn_replacement_process()
            self.bot._mitra_restart_requested = True
            await self.bot.close()
        except Exception as exc:
            logging.exception("Peer update job %s failed", job["id"])
            job.update(state="failed", error=str(exc)[:500])
            self.save("update_jobs", job)

    def targets(self, server):
        members = {self.mesh.config.node_id, *self.mesh.peers}
        if server != "all" and server not in members:
            raise ValueError("Unknown server; choose a node ID or all")
        chosen = members if server == "all" else {server}
        # The coordinator survives until every remote node has rejoined.
        return sorted(chosen - {self.mesh.config.node_id}) + ([self.mesh.config.node_id] if self.mesh.config.node_id in chosen else [])

    async def node_status(self, node, job_id=None):
        if node == self.mesh.config.node_id:
            return self.status(job_id)
        return await self.mesh.request(node, "update_status", {"job": job_id} if job_id else {}, timeout=5)

    async def preview(self, server):
        states = {}
        for node in self.targets(server):
            try:
                status = await self.node_status(node)
                if status.get("capability") != 1:
                    raise ValueError("Missing rolling update capability")
                if not status.get("discord"):
                    raise ValueError("Node is not connected to Discord")
                states[node] = status
            except Exception:
                raise ValueError(f"{node} is unreachable, disconnected from Discord, or lacks rolling-update support. Update older peers manually first.") from None
        return states

    async def begin(self, server, version, actor):
        if not self.mesh.is_state_owner:
            raise ValueError("Only the application-state owner can coordinate updates")
        if any(p["state"] == "running" for p in self.rows("update_plans")):
            raise ValueError("A rolling update is already running; use /update status")
        states = await self.preview(server)
        # Another confirmation may have arrived while checking peers.
        if any(p["state"] == "running" for p in self.rows("update_plans")):
            raise ValueError("A rolling update is already running")
        plan = dict(id=uuid.uuid4().hex, version=version, actor=actor, state="running", error=None,
                    nodes=[dict(node=n, job=uuid.uuid4().hex, boot=s["boot"], state="pending", deadline=None)
                           for n, s in states.items()])
        self.save("update_plans", plan)
        self.spawn(self.run_plan(plan))
        return plan

    def cancel(self, actor):
        for plan in self.rows("update_plans"):
            if plan["state"] == "running":
                plan.update(state="cancelled", cancelled_by=actor)
                self.save("update_plans", plan)
                return plan["id"]
        raise ValueError("No rolling update is active")

    def cancelled(self, plan):
        return any(p["id"] == plan["id"] and p["state"] == "cancelled" for p in self.rows("update_plans"))

    async def run_plan(self, plan):
        try:
            for entry in plan["nodes"]:
                if self.cancelled(plan):
                    return
                if entry["state"] in {"complete", "skipped"}:
                    continue
                node = entry["node"]
                if entry["state"] == "pending":
                    status = await self.node_status(node)
                    if Version(status["version"]) >= Version(plan["version"]):
                        if not status["discord"]:
                            raise ValueError(f"{node} has no Discord connection")
                        entry["state"] = "skipped"
                        self.save("update_plans", plan)
                        continue
                    entry.update(state="waiting", boot=status["boot"], deadline=time.time()+1800)
                    self.save("update_plans", plan)
                # Persisted job ID makes an ambiguous ACK safe to retry.
                stable = 0
                while time.time() < entry["deadline"]:
                    if self.cancelled(plan):
                        return
                    try:
                        status = await self.node_status(node, entry["job"])
                    except Exception:
                        status = None
                    if status is not None:
                        if self.cancelled(plan):
                            return
                        job = status.get("job")
                        if job and job["state"] == "failed":
                            raise ValueError(f"{node}: {job['error']}")
                        if job and job["state"] == "complete" and Version(status["version"]) != Version(plan["version"]):
                            raise ValueError(f"{node} returned on an unexpected version; rollout stopped")
                        if not job:
                            payload = {"job": entry["job"], "version": plan["version"]}
                            try:
                                if node == self.mesh.config.node_id:
                                    await self.rpc("update_install", payload)
                                else:
                                    await self.mesh.request(node, "update_install", payload, timeout=5)
                            except Exception as exc:
                                # Poll before retrying: the target may already have committed it.
                                from mitra_bot.services.peer_service import PeerError
                                if isinstance(exc, PeerError) and exc.__cause__ is None:
                                    raise ValueError(f"{node} rejected the update request; inspect that node before retrying") from exc
                        ready = (job and job["state"] == "complete" and status["boot"] != entry["boot"]
                                 and Version(status["version"]) == Version(plan["version"]) and status["discord"])
                        stable = stable + 1 if ready else 0
                        if stable >= 2:
                            entry["state"] = "complete"
                            self.save("update_plans", plan)
                            break
                    else:
                        stable = 0
                    await asyncio.sleep(5)
                else:
                    raise ValueError(f"{node} did not confirm a healthy restart within 30 minutes; rollout stopped")
            plan["state"] = "complete"
            self.save("update_plans", plan)
        except Exception as exc:
            logging.exception("Rolling update %s stopped", plan["id"])
            if self.cancelled(plan):
                return
            plan.update(state="failed", error=str(exc)[:500])
            self.save("update_plans", plan)
