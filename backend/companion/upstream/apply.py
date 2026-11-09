"""Save-and-apply: write config.json, restart upstream (if allowed), verify it loaded the devices."""

from __future__ import annotations

import asyncio
import time

from ..db import Database
from ..events_bus import EventBus
from ..logging import get_logger
from ..util import dumps, loads, now_iso, row_to_dict, screen_key
from .config_store import ConfigError, ConfigStore
from .docker_client import DockerClient, DockerError
from .ingest import Collector

log = get_logger("apply")

RESTART_TIMEOUT = 75      # seconds to see the container running again
LOAD_TIMEOUT = 120        # seconds to see every configured device loaded by the new run


class ApplyService:
    def __init__(self, db: Database, bus: EventBus, store: ConfigStore, collector: Collector, docker: DockerClient | None, settings) -> None:
        self.db = db
        self.bus = bus
        self.store = store
        self.collector = collector
        self.docker = docker
        self.settings = settings
        self._lock = asyncio.Lock()
        self._current: asyncio.Task | None = None

    def busy(self) -> bool:
        return self._lock.locked()

    async def capability(self) -> dict:
        """What 'apply' can do in this deployment."""
        if not self.docker:
            return {"mode": "manual", "reason": "No Docker API configured; restart iSponsorBlockTV from your NAS after saving."}
        if not self.settings.allow_restart:
            return {"mode": "manual", "reason": "ALLOW_RESTART is disabled; restart iSponsorBlockTV from your NAS after saving."}
        try:
            info = await self.docker.find_container(self.settings.upstream_container)
        except DockerError as exc:
            return {"mode": "manual", "reason": f"Docker API unavailable ({exc.code}); restart iSponsorBlockTV from your NAS after saving."}
        if info is None:
            return {"mode": "manual", "reason": f"Container '{self.settings.upstream_container}' not found; restart iSponsorBlockTV from your NAS after saving."}
        if self.docker.permissions.get("restart") is False:
            return {"mode": "manual", "reason": "The Docker socket proxy does not allow restarts (ALLOW_RESTARTS=0)."}
        return {"mode": "restart", "container": info.name, "state": info.state}

    async def start_job(self, new_config: dict, expected_etag: str | None, actor: str | None, apply: bool) -> dict:
        if self._lock.locked():
            raise ConfigError("Another save is in progress; wait for it to finish.", "busy")
        job_id = await self.db.execute_returning_id(
            "INSERT INTO apply_jobs (created_at, status, steps, actor) VALUES (?, 'pending', '[]', ?)", (now_iso(), actor))
        self._current = asyncio.create_task(self._run(job_id, new_config, expected_etag, actor, apply))
        return await self.get_job(job_id)

    async def get_job(self, job_id: int) -> dict:
        row = await self.db.fetchone("SELECT * FROM apply_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise ConfigError("Job not found.", "not_found")
        d = row_to_dict(row)
        d["steps"] = loads(d.get("steps"), [])
        return d

    async def latest_job(self) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM apply_jobs ORDER BY id DESC LIMIT 1")
        if row is None:
            return None
        d = row_to_dict(row)
        d["steps"] = loads(d.get("steps"), [])
        return d

    async def _update(self, job_id: int, status: str, steps: list[dict], error: str | None = None, finished: bool = False, sha: str | None = None) -> None:
        await self.db.execute(
            "UPDATE apply_jobs SET status = ?, steps = ?, error = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END, config_sha256 = COALESCE(?, config_sha256) WHERE id = ?",
            (status, dumps(steps), error, 1 if finished else 0, now_iso(), sha, job_id))
        self.bus.publish("apply_job", {"id": job_id, "status": status, "steps": steps, "error": error})

    @staticmethod
    def _step(steps: list[dict], name: str, status: str, detail: str | None = None) -> None:
        for s in steps:
            if s["name"] == name:
                s.update({"status": status, "detail": detail, "at": now_iso()})
                return
        steps.append({"name": name, "status": status, "detail": detail, "at": now_iso()})

    async def _run(self, job_id: int, new_config: dict, expected_etag: str | None, actor: str | None, apply: bool) -> None:
        async with self._lock:
            steps: list[dict] = []
            try:
                self._step(steps, "save", "running", "Validating and writing config.json")
                await self._update(job_id, "pending", steps)
                snap = await self.store.write(new_config, expected_etag=expected_etag, reason="apply", actor=actor)
                self._step(steps, "save", "ok", f"config.json written ({snap.size} bytes)")
                await self.collector.on_config_changed(snap.data)
                await self._update(job_id, "saved", steps, sha=snap.etag)
            except ConfigError as exc:
                self._step(steps, "save", "failed", str(exc))
                await self._update(job_id, "failed", steps, error=dumps({"code": exc.code, "message": str(exc), **exc.details}), finished=True)
                log.warning("apply.save.failed", "Config save failed", code=exc.code, error=str(exc))
                return
            except Exception as exc:
                self._step(steps, "save", "failed", str(exc))
                await self._update(job_id, "failed", steps, error=dumps({"code": "internal", "message": str(exc)}), finished=True)
                log.exception("apply.save.crashed", "Config save crashed")
                return

            if not apply:
                self._step(steps, "apply", "skipped", "Saved only. iSponsorBlockTV keeps running with its previous settings until it is restarted.")
                await self._update(job_id, "saved", steps, finished=True)
                return

            cap = await self.capability()
            expected_keys = {screen_key(d["screen_id"]) for d in new_config.get("devices", []) if isinstance(d, dict) and isinstance(d.get("screen_id"), str)}
            if cap["mode"] != "restart":
                self._step(steps, "restart", "manual", cap["reason"])
                await self._update(job_id, "manual_restart_required", steps)
                # Passive verification: wait (bounded) for a new run and device loads.
                ok, detail = await self._verify_new_run(expected_keys, timeout=15 * 60, require_restart_after=time.time())
                if ok:
                    self._step(steps, "verify", "ok", detail)
                    await self._update(job_id, "applied", steps, finished=True)
                else:
                    self._step(steps, "verify", "pending", detail)
                    await self._update(job_id, "manual_restart_required", steps, finished=True)
                return

            try:
                self._step(steps, "restart", "running", f"Restarting container '{cap['container']}' (playback skipping pauses for ~10–30 s)")
                await self._update(job_id, "restarting", steps)
                assert self.docker is not None
                before = await self.docker.inspect(self.settings.upstream_container)
                await self.docker.restart(before.id, timeout_s=15)
                started = await self._wait_running(before.started_at)
                if not started:
                    self._step(steps, "restart", "failed", "The container did not come back within the timeout.")
                    await self._update(job_id, "failed", steps, error=dumps({"code": "restart_timeout", "message": "Saved, but iSponsorBlockTV did not restart in time."}), finished=True)
                    return
                self._step(steps, "restart", "ok", f"Container running again (started {started.started_at})")
                await self._update(job_id, "verifying", steps)
            except DockerError as exc:
                self._step(steps, "restart", "failed", str(exc))
                await self._update(job_id, "failed", steps, error=dumps({"code": exc.code, "message": f"Saved, but the restart failed: {exc}"}), finished=True)
                log.error("apply.restart.failed", "Restart failed", code=exc.code, error=str(exc))
                return

            self._step(steps, "verify", "running", "Waiting for iSponsorBlockTV to load every configured device")
            await self._update(job_id, "verifying", steps)
            ok, detail = await self._verify_new_run(expected_keys, timeout=LOAD_TIMEOUT, require_restart_after=None)
            if ok:
                self._step(steps, "verify", "ok", detail)
                await self._update(job_id, "applied", steps, finished=True)
                log.info("apply.success", "Configuration applied and verified", job_id=job_id)
            else:
                self._step(steps, "verify", "failed", detail)
                await self._update(job_id, "failed", steps, error=dumps({"code": "verify_failed", "message": detail}), finished=True)
                log.error("apply.verify.failed", "Configuration saved but verification failed", job_id=job_id, detail=detail)

    async def _wait_running(self, previous_started_at: str | None):
        assert self.docker is not None
        deadline = time.monotonic() + RESTART_TIMEOUT
        while time.monotonic() < deadline:
            try:
                info = await self.docker.inspect(self.settings.upstream_container)
                if info.state == "running" and info.started_at != previous_started_at:
                    return info
            except DockerError:
                pass
            await asyncio.sleep(2)
        return None

    async def _verify_new_run(self, expected_keys: set[str], timeout: float, require_restart_after: float | None) -> tuple[bool, str]:
        """Watch collector events for a new run during which every expected device gets loaded."""
        q = self.collector.watch_apply()
        loaded: set[int] = set()
        expected_ids = {d["id"] for d in self.collector.devices.all() if d["screen_key"] in expected_keys}
        run_seen = require_restart_after is None
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=min(5.0, max(0.1, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    if run_seen and expected_ids and expected_ids <= loaded:
                        break
                    continue
                if msg["type"] == "run_started":
                    run_seen = True
                    loaded.clear()
                elif msg["type"] == "fatal":
                    return False, f"iSponsorBlockTV reported a fatal startup problem: {msg['reason']}"
                elif msg["type"] == "device_loaded" and run_seen:
                    loaded.add(msg["device_id"])
                    if expected_ids and expected_ids <= loaded:
                        break
            if self.collector.run_fatal():
                return False, f"iSponsorBlockTV reported a fatal startup problem: {self.collector.run_fatal()}"
            if not run_seen:
                return False, "No restart of iSponsorBlockTV was observed yet. Restart it from your NAS; this page updates automatically."
            if expected_ids and expected_ids <= loaded:
                return True, f"iSponsorBlockTV loaded {len(loaded)} device(s) after the restart."
            missing = len(expected_ids - loaded)
            if self.collector.status.status != "connected":
                return False, f"Saved, but the companion lost the log stream ({self.collector.status.detail}); could not confirm devices loaded."
            return False, (f"iSponsorBlockTV restarted but {missing} of {len(expected_ids)} device(s) did not report a YouTube session refresh within "
                           f"{int(timeout)} s. Check the device's error state; a revoked screen ID needs re-pairing.")
        finally:
            self.collector.unwatch_apply(q)
