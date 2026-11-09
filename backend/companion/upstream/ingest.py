"""Collector: streams upstream logs, interprets them into durable events and device state.

Guarantees:
* every stored record carries a deterministic ``dedup_key`` (run, timestamp, line hash) so a
  replayed Docker stream or a resumed checkpoint never double-counts, while a genuine repeat skip
  (different timestamp) is kept;
* the checkpoint is committed together with the records it covers;
* source outages open/close ``collection_gaps`` and never zero out existing statistics.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..db import Database, DatabaseUnavailable
from ..events_bus import EventBus
from ..logging import get_logger
from ..util import dumps, iso, now_iso, parse_iso, utcnow
from .devices import DeviceRegistry
from .docker_client import ContainerInfo, DockerClient, DockerError
from .parser import ParsedLine, UpstreamLogParser
from .sources import DockerLogSource, FileLogSource, RawLine, SourceUnavailable, parse_docker_ts

log = get_logger("ingest")

UNMUTE_PAIR_WINDOW = timedelta(minutes=10)
PLAYBACK_CONTINUITY = timedelta(hours=6)
STORED_EVENT_KINDS = {"skip", "ad_skip", "ad_mute", "ad_unmute", "connected", "disconnected", "loaded", "error", "warning"}
ERROR_KINDS = {"connection_denied", "connected_as_screen", "unknown_reply", "auth_failed", "subscribe_failed",
               "connect_failed", "command_failed", "error", "sb_error"}
FATAL_STARTUP_KINDS = {
    "startup_no_config": "iSponsorBlockTV found no config.json and is exiting.",
    "startup_no_devices": "iSponsorBlockTV found no devices in config.json and is exiting.",
    "legacy_atvs": "iSponsorBlockTV refuses to start while the legacy 'atvs' key is present.",
}


@dataclass
class CollectionStatus:
    source_kind: str = "none"               # docker|file|none
    status: str = "starting"                # starting|connected|disconnected|unavailable|disabled
    detail: str | None = None
    error_code: str | None = None
    since: str | None = None
    last_record_at: str | None = None       # timestamp of newest upstream record processed
    last_successful_update: str | None = None  # wall clock of last successful DB commit
    coverage_start: str | None = None
    lines_processed: int = 0
    records_stored: int = 0
    duplicates_skipped: int = 0
    unparsed_lines: int = 0
    reconnects: int = 0
    container: dict | None = None
    current_run_id: str | None = None
    upstream_debug: bool | None = None
    parser_version: str = "2.x-r1"
    open_gap_id: int | None = None
    pending_unmute_noise: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class _DeviceRuntime:
    device_id: int
    state: str = "unknown"
    current_video_id: str | None = None
    current_playback_id: int | None = None
    open_mute_event_id: int | None = None
    open_mute_at: datetime | None = None
    last_playing_ts: datetime | None = None
    last_line_ts: datetime | None = None
    run_id: str | None = None


class Collector:
    def __init__(self, db: Database, bus: EventBus, devices: DeviceRegistry, parser: UpstreamLogParser,
                 docker: DockerClient | None, settings) -> None:
        self.db = db
        self.bus = bus
        self.devices = devices
        self.parser = parser
        self.docker = docker
        self.settings = settings
        self.status = CollectionStatus(parser_version=parser.__class__.__module__ and "2.x-r1")
        self._runtime: dict[int, _DeviceRuntime] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._source_key: str = ""
        self._cursor: str | None = None
        self._last_line_hash: str | None = None
        self._pending_cursor: str | None = None
        self._last_commit = 0.0
        self._last_ts: datetime | None = None
        self._run_id: str | None = None
        self._run_started: datetime | None = None
        self._dedup_scope: str = ""
        self._run_fatal: str | None = None
        self.apply_watchers: list[asyncio.Queue] = []
        self.config_provider = None  # async callable returning current config dict (set by app)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.status.coverage_start = await self.db.get_state("coverage_start")
        self.status.last_successful_update = await self.db.get_state("last_successful_update")
        for dev in self.devices.all():
            st = await self.db.fetchone("SELECT * FROM device_state WHERE device_id = ?", (dev["id"],))
            rt = _DeviceRuntime(dev["id"])
            if st:
                rt.state = st["state"]
                rt.current_video_id = st["current_video_id"]
                rt.run_id = st["upstream_run_id"]
            self._runtime[dev["id"]] = rt
        self._task = asyncio.create_task(self._run(), name="collector")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._commit_checkpoint(force=True)

    def _select_source(self) -> str:
        mode = self.settings.log_source
        if mode == "auto":
            if self.settings.upstream_log_file and self.settings.upstream_log_file.exists():
                return "file"
            return "docker" if self.docker else "none"
        return mode

    async def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            kind = self._select_source()
            self.status.source_kind = kind
            if kind == "none":
                await self._set_status("disabled", "No log source configured (LOG_SOURCE=none). Device status and statistics cannot be collected.", "disabled")
                await asyncio.sleep(30)
                continue
            try:
                if kind == "docker":
                    await self._run_docker()
                else:
                    await self._run_file()
                backoff = 5.0
            except SourceUnavailable as exc:
                await self._set_status("unavailable", str(exc), exc.code)
                delay = min(exc.retry_in, 300.0) * (0.8 + random.random() * 0.4)
                await asyncio.sleep(delay)
            except DatabaseUnavailable as exc:
                await self._set_status("unavailable", f"Companion database unavailable: {exc}", "database")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("ingest.loop.crashed", "Collector loop failed; restarting with backoff", error=str(exc))
                await self._set_status("unavailable", f"Collector error: {exc.__class__.__name__}", "internal")
                await asyncio.sleep(backoff * (0.8 + random.random() * 0.4))
                backoff = min(backoff * 2, 300.0)

    async def _set_status(self, status: str, detail: str | None, code: str | None = None) -> None:
        changed = status != self.status.status or detail != self.status.detail
        self.status.status = status
        self.status.detail = detail
        self.status.error_code = code
        if changed:
            self.status.since = now_iso()
            if status in {"unavailable", "disconnected", "disabled"}:
                await self._open_gap(code or status, detail or status)
                log.warning("ingest.source.down", "Log source unavailable", status=status, code=code, detail=detail)
            else:
                await self._close_gap()
                log.info("ingest.source.up", "Log source connected", status=status, detail=detail)
            self.bus.publish("collection", self.status.as_dict())

    async def _open_gap(self, reason: str, detail: str) -> None:
        if self.status.open_gap_id or not self._source_key:
            return
        gid = await self.db.execute_returning_id(
            "INSERT INTO collection_gaps (source, started_at, reason) VALUES (?, ?, ?)",
            (self._source_key, now_iso(), f"{reason}: {detail}"[:500]))
        self.status.open_gap_id = gid

    async def _close_gap(self) -> None:
        if self.status.open_gap_id:
            await self.db.execute("UPDATE collection_gaps SET ended_at = ? WHERE id = ?", (now_iso(), self.status.open_gap_id))
            self.status.open_gap_id = None

    # ------------------------------------------------------------------ docker
    async def _run_docker(self) -> None:
        assert self.docker is not None
        source = DockerLogSource(self.docker, self.settings.upstream_container)
        info = await source.resolve()
        self._source_key = f"docker:{info.name}"
        self.status.container = self._container_summary(info)
        self.status.upstream_debug = info.env_has_debug
        await self._load_checkpoint()
        await self._begin_run(info)
        await self._set_status("connected", f"Following container '{info.name}' ({info.state})")
        if info.state != "running":
            # Still read whatever history exists, then wait for it to come back.
            pass
        first = True
        async for raw in source.stream(self._cursor):
            if first:
                first = False
                await self._detect_rotation_gap(raw)
            await self._handle_raw(raw)
            if self._stop.is_set():
                break
        # Stream ended: container stopped or restarted.
        await self._commit_checkpoint(force=True)
        try:
            info2 = await self.docker.inspect(info.id)
            self.status.container = self._container_summary(info2)
            if info2.state == "running":
                raise SourceUnavailable("Log stream closed by Docker; reconnecting.", "stream_closed", retry_in=2.0)
            await self._end_run(f"container {info2.state} (exit code {info2.exit_code})")
            raise SourceUnavailable(f"iSponsorBlockTV container is {info2.state}; waiting for it to start.", "container_stopped", retry_in=10.0)
        except DockerError as exc:
            await self._end_run("container disappeared")
            raise SourceUnavailable(str(exc), exc.code, retry_in=10.0) from exc

    @staticmethod
    def _container_summary(info: ContainerInfo) -> dict:
        return {"id": info.id[:12], "name": info.name, "image": info.image, "state": info.state,
                "started_at": info.started_at, "restart_count": info.restart_count, "log_driver": info.log_driver,
                "version": info.version_label, "exit_code": info.exit_code, "debug": info.env_has_debug}

    async def _detect_rotation_gap(self, raw: RawLine) -> None:
        if not self._cursor or not raw.docker_ts:
            return
        prev = parse_docker_ts(self._cursor)
        first = parse_docker_ts(raw.docker_ts)
        if prev and first and (first - prev) > timedelta(minutes=2):
            # Lines between the checkpoint and the first available line were dropped by Docker's own rotation.
            await self.db.execute("INSERT INTO collection_gaps (source, started_at, ended_at, reason) VALUES (?, ?, ?, ?)",
                                  (self._source_key, iso(prev), iso(first), "docker_log_rotation: lines between checkpoint and oldest available log were discarded by Docker's log driver"))
            log.warning("ingest.gap.rotation", "Docker discarded log lines between the checkpoint and the oldest available line",
                        checkpoint=iso(prev), first_available=iso(first))

    async def _begin_run(self, info: ContainerInfo) -> None:
        run_id = f"{info.id[:12]}@{info.started_at}"
        if run_id == self._run_id:
            return
        self._run_id = run_id
        self._run_started = parse_docker_ts(info.started_at) if info.started_at else None
        self._dedup_scope = f"docker:{info.id[:12]}"
        self._run_fatal = None
        self.status.current_run_id = run_id
        await self.db.execute(
            "INSERT OR IGNORE INTO upstream_runs (run_id, started_at, container_id, image, version) VALUES (?, ?, ?, ?, ?)",
            (run_id, info.started_at or now_iso(), info.id[:12], info.image, info.version_label))
        log.info("ingest.run.begin", "Observing upstream run", run_id=run_id, image=info.image, version=info.version_label)
        self.bus.publish("run", {"run_id": run_id, "started_at": info.started_at})
        for q in list(self.apply_watchers):
            q.put_nowait({"type": "run_started", "run_id": run_id, "started_at": info.started_at})

    async def _end_run(self, reason: str) -> None:
        if self._run_id:
            await self.db.execute("UPDATE upstream_runs SET ended_at = COALESCE(ended_at, ?), exit_reason = ? WHERE run_id = ?",
                                  (now_iso(), reason[:200], self._run_id))
            log.info("ingest.run.end", "Upstream run ended", run_id=self._run_id, reason=reason)
            for rt in self._runtime.values():
                if rt.state in {"connected", "playing", "loaded"}:
                    await self._transition(rt, "unknown", utcnow(), f"upstream run ended: {reason}")
            for q in list(self.apply_watchers):
                q.put_nowait({"type": "run_ended", "run_id": self._run_id, "reason": reason})

    # ------------------------------------------------------------------ file
    async def _run_file(self) -> None:
        path = self.settings.upstream_log_file
        if path is None:
            raise SourceUnavailable("UPSTREAM_LOG_FILE is not set.", "config", retry_in=60.0)
        source = FileLogSource(path)
        self._source_key = f"file:{path}"
        self.status.container = None
        await self._load_checkpoint()
        self._dedup_scope = f"file:{path}"
        if not self._run_id:
            self._run_id = f"file@{now_iso()}"
            self.status.current_run_id = self._run_id
            await self.db.execute("INSERT OR IGNORE INTO upstream_runs (run_id, started_at) VALUES (?, ?)", (self._run_id, now_iso()))
        await self._set_status("connected", f"Tailing {path}")
        async for raw in source.stream(self._cursor):
            await self._handle_raw(raw)
            if self._stop.is_set():
                break

    # ------------------------------------------------------------------ checkpoint
    async def _load_checkpoint(self) -> None:
        row = await self.db.fetchone("SELECT * FROM ingest_checkpoint WHERE source = ?", (self._source_key,))
        if row:
            self._cursor = row["cursor"]
            self._last_line_hash = row["last_line_hash"]
            self.status.lines_processed = row["lines_processed"]
            log.info("ingest.checkpoint.loaded", "Resuming from checkpoint", source=self._source_key, cursor=self._cursor)
        else:
            self._cursor = None
            log.info("ingest.checkpoint.none", "No checkpoint; reading all available history", source=self._source_key)

    async def _commit_checkpoint(self, force: bool = False) -> None:
        if self._pending_cursor is None:
            return
        now = time.monotonic()
        if not force and now - self._last_commit < 1.0:
            return
        self._last_commit = now
        self._cursor = self._pending_cursor
        await self.db.execute(
            "INSERT INTO ingest_checkpoint (source, cursor, last_line_hash, lines_processed, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor, last_line_hash = excluded.last_line_hash, "
            "lines_processed = excluded.lines_processed, updated_at = excluded.updated_at",
            (self._source_key, self._cursor, self._last_line_hash, self.status.lines_processed, now_iso()))
        self.status.last_successful_update = now_iso()
        await self.db.set_state("last_successful_update", self.status.last_successful_update)
        log.debug("ingest.checkpoint.commit", "Checkpoint committed", cursor=self._cursor, lines=self.status.lines_processed)

    # ------------------------------------------------------------------ processing
    async def _handle_raw(self, raw: RawLine) -> None:
        parsed = self.parser.parse(raw.text)
        ts = parse_docker_ts(raw.docker_ts) if raw.docker_ts else None
        if ts is None:
            ts = parsed.ts or self._last_ts or utcnow()
        self._last_ts = ts
        self.status.lines_processed += 1
        if parsed.kind == "other" or parsed.kind == "continuation":
            self.status.unparsed_lines += 1
        line_hash = hashlib.sha1(parsed.redacted_line.encode("utf-8")).hexdigest()[:12]
        # Identity of a log line = source + its own (nanosecond) timestamp + content. Independent of
        # which upstream run we were observing when it was read, so replays never double-count while
        # a genuinely repeated message (different timestamp) is kept.
        stamp = raw.docker_ts or f"{iso(ts)}@{raw.cursor}"
        dedup = f"{self._dedup_scope or self._source_key}|{stamp}|{line_hash}"
        # Lines older than the current run's start belong to an earlier run (Docker keeps them).
        self._line_is_current_run = not (self._run_started and ts < self._run_started)
        try:
            async with self.db.transaction() as conn:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO upstream_log (at, at_epoch, level, logger, device_id, message, parsed, stream, run_id, dedup_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (iso(ts), ts.timestamp(), parsed.level, parsed.logger, None, parsed.message[:4000], parsed.kind, raw.stream, self._run_id, dedup))
                if cur.rowcount == 0:
                    self.status.duplicates_skipped += 1
                    self._pending_cursor = raw.cursor
                    return
                await self._interpret(conn, parsed, ts, dedup, cur.lastrowid)
            self.status.records_stored += 1
            self.status.last_record_at = iso(ts)
            self._last_line_hash = line_hash
            self._pending_cursor = raw.cursor
            if self.status.coverage_start is None:
                self.status.coverage_start = iso(ts)
                await self.db.set_state("coverage_start", self.status.coverage_start)
                log.info("ingest.coverage.start", "First upstream record collected; coverage starts here", at=self.status.coverage_start)
            await self._commit_checkpoint()
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            log.exception("ingest.line.failed", "Failed to process an upstream log line", kind=parsed.kind, error=str(exc))

    async def _interpret(self, conn, p: ParsedLine, ts: datetime, dedup: str, log_row_id: int) -> None:
        if getattr(self, "_file_run_ended", False) and p.kind not in {"shutdown", "exited", "continuation"}:
            # File mode has no container metadata; upstream's "Exited" line marks a restart boundary.
            self._file_run_ended = False
            self._run_id = f"file@{iso(ts)}"
            self.status.current_run_id = self._run_id
            await conn.execute("INSERT OR IGNORE INTO upstream_runs (run_id, started_at) VALUES (?, ?)", (self._run_id, iso(ts)))
            log.info("ingest.run.begin", "Observing upstream run (file mode)", run_id=self._run_id)
            for q in list(self.apply_watchers):
                q.put_nowait({"type": "run_started", "run_id": self._run_id, "started_at": iso(ts)})
        device = None
        rt = None
        if p.screen_key:
            device = self.devices.by_key(p.screen_key) or await self.devices.from_log(p.screen_key, p.screen_id)
            rt = self._runtime.setdefault(device["id"], _DeviceRuntime(device["id"]))
            rt.last_line_ts = ts
            await conn.execute("UPDATE upstream_log SET device_id = ? WHERE id = ?", (device["id"], log_row_id))
            await conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)", (iso(ts), device["id"], iso(ts)))
            await conn.execute("UPDATE device_state SET last_log_at = ? WHERE device_id = ?", (iso(ts), device["id"]))
            device["last_seen_at"] = iso(ts)
        kind = p.kind
        f = p.fields
        if kind in FATAL_STARTUP_KINDS and getattr(self, "_line_is_current_run", True):
            self._run_fatal = FATAL_STARTUP_KINDS[kind]
            log.error("upstream.fatal_startup", "Upstream reports a fatal startup condition", reason=self._run_fatal)
            self.bus.publish("upstream_fatal", {"reason": self._run_fatal, "at": iso(ts)})
            for q in list(self.apply_watchers):
                q.put_nowait({"type": "fatal", "reason": self._run_fatal})
            return
        if kind in {"shutdown", "exited"} and getattr(self, "_line_is_current_run", True):
            for q in list(self.apply_watchers):
                q.put_nowait({"type": "shutdown"})
            if self.status.source_kind == "file":
                self._file_run_ended = True  # the next upstream line starts a new run
            return
        if rt is None or device is None:
            if kind == "sb_error":
                await self._store_event(conn, "error", None, ts, dedup, video_id=f.get("video_id"),
                                        details={"message": p.message[:300], "code": "sponsorblock_error", "status": f.get("status")})
            return
        did = device["id"]
        current = getattr(self, "_line_is_current_run", True)
        if kind == "auth_refreshed":
            if not current:
                await self._store_event(conn, "loaded", did, ts, dedup, details={"run": "previous"})
                return
            rt.run_id = self._run_id
            await conn.execute("UPDATE device_state SET last_loaded_at = ?, upstream_run_id = ?, last_error = NULL, last_error_at = NULL WHERE device_id = ?", (iso(ts), self._run_id, did))
            if rt.state in {"unknown", "configured", "disconnected"}:
                await self._transition(rt, "loaded", ts, "iSponsorBlockTV refreshed this device's YouTube session", conn)
            await self._store_event(conn, "loaded", did, ts, dedup, details={"run_id": self._run_id})
            for q in list(self.apply_watchers):
                q.put_nowait({"type": "device_loaded", "device_id": did, "run_id": self._run_id})
        elif kind == "connected":
            if f.get("screen_name"):
                await self.devices.set_screen_name(did, f["screen_name"])
            await conn.execute("UPDATE device_state SET last_connected_at = ? WHERE device_id = ?", (iso(ts), did))
            await self._transition(rt, "connected", ts, "connected to the TV's YouTube session", conn)
            await self._store_event(conn, "connected", did, ts, dedup, details={"screen_name": f.get("screen_name")})
        elif kind == "playing":
            vid = f["video_id"]
            await self._ensure_video(conn, vid, ts)
            new_playback = rt.current_video_id != vid or rt.last_playing_ts is None or (ts - rt.last_playing_ts) > PLAYBACK_CONTINUITY
            if new_playback:
                cur = await conn.execute(
                    "INSERT INTO playbacks (device_id, video_id, started_at, last_seen_at, segments_detected) VALUES (?, ?, ?, ?, ?)",
                    (did, vid, iso(ts), iso(ts), f.get("segments")))
                rt.current_playback_id = cur.lastrowid
                rt.current_video_id = vid
                await conn.execute("UPDATE device_state SET current_video_id = ?, current_video_since = ? WHERE device_id = ?", (vid, iso(ts), did))
                self.bus.publish("playback", {"device_id": did, "video_id": vid, "at": iso(ts), "segments": f.get("segments")})
            else:
                await conn.execute("UPDATE playbacks SET last_seen_at = ?, state_changes = state_changes + 1, segments_detected = COALESCE(?, segments_detected) WHERE id = ?",
                                   (iso(ts), f.get("segments"), rt.current_playback_id))
            rt.last_playing_ts = ts
            await self._transition(rt, "playing", ts, "playback reported", conn)
        elif kind == "skip":
            vid = rt.current_video_id
            await self._store_event(conn, "skip", did, ts, dedup, video_id=vid, playback_id=rt.current_playback_id,
                                    seek_to=f["seek_to"], match_confidence="pending" if vid else "unavailable",
                                    details=None if vid else {"note": "no current video known for this device when the skip was logged"})
            if vid:
                await conn.execute("UPDATE videos SET sb_status = CASE WHEN sb_status = 'none' THEN 'pending' ELSE sb_status END WHERE video_id = ?", (vid,))
        elif kind == "ad_mute":
            eid = await self._store_event(conn, "ad_mute", did, ts, dedup, video_id=rt.current_video_id, playback_id=rt.current_playback_id)
            if eid:
                rt.open_mute_event_id = eid
                rt.open_mute_at = ts
        elif kind == "ad_unmute":
            if rt.open_mute_event_id and rt.open_mute_at and ts - rt.open_mute_at <= UNMUTE_PAIR_WINDOW:
                duration = (ts - rt.open_mute_at).total_seconds()
                await conn.execute("UPDATE events SET duration = ?, paired_event_id = NULL WHERE id = ?", (duration, rt.open_mute_event_id))
                await self._store_event(conn, "ad_unmute", did, ts, dedup, video_id=rt.current_video_id, playback_id=rt.current_playback_id,
                                        paired_event_id=rt.open_mute_event_id, duration=duration)
                rt.open_mute_event_id = None
                rt.open_mute_at = None
            else:
                self.status.pending_unmute_noise += 1  # upstream logs this on every playing state change
        elif kind == "ad_skip":
            await self._store_event(conn, "ad_skip", did, ts, dedup, video_id=rt.current_video_id, playback_id=rt.current_playback_id)
        elif kind == "subscribe_ended":
            await conn.execute("UPDATE device_state SET last_disconnected_at = ?, current_video_id = NULL, current_video_since = NULL WHERE device_id = ?", (iso(ts), did))
            rt.current_video_id = None
            rt.current_playback_id = None
            await self._transition(rt, "disconnected", ts, f"TV session ended (status {f.get('status')})", conn)
            await self._store_event(conn, "disconnected", did, ts, dedup, details={"status": f.get("status")})
        elif kind in ERROR_KINDS or kind == "warning":
            level_kind = "warning" if (kind == "warning" or kind in {"connection_denied", "unknown_reply"}) else "error"
            hint = {
                "auth_failed": "YouTube rejected this device's screen ID. Re-pair the TV from the Devices page.",
                "connected_as_screen": "Upstream connected as the screen itself; force-close the YouTube app on the TV.",
                "connection_denied": "YouTube denied the connection; upstream is retrying with a workaround.",
            }.get(kind)
            await conn.execute("UPDATE device_state SET last_error = ?, last_error_at = ? WHERE device_id = ?", (p.message[:300], iso(ts), did))
            await self._store_event(conn, level_kind, did, ts, dedup, video_id=rt.current_video_id,
                                    details={"message": p.message[:300], "code": kind, "hint": hint})
        elif kind == "legacy_screen_id":
            await conn.execute("UPDATE device_state SET last_error = ?, last_error_at = ? WHERE device_id = ?",
                               ("Legacy 26-character screen ID; re-pair this TV.", iso(ts), did))
        # other kinds (prefetch, subscribing, watchdog, debug_event, continuation) are stored as raw log only

    async def _ensure_video(self, conn, video_id: str, ts: datetime) -> None:
        await conn.execute(
            "INSERT INTO videos (video_id, first_seen_at, last_seen_at, meta_status, sb_status) VALUES (?, ?, ?, ?, 'pending') "
            "ON CONFLICT(video_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (video_id, iso(ts), iso(ts), "pending" if self.settings.enrichment_enabled else "disabled"))

    async def _store_event(self, conn, kind: str, device_id: int | None, ts: datetime, dedup: str, *, video_id: str | None = None,
                           playback_id: int | None = None, seek_to: float | None = None, match_confidence: str | None = None,
                           paired_event_id: int | None = None, duration: float | None = None, details: dict | None = None) -> int | None:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO events (kind, device_id, at, at_epoch, video_id, playback_id, seek_to, match_confidence, outcome, paired_event_id, duration, details, dedup_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?)",
            (kind, device_id, iso(ts), ts.timestamp(), video_id, playback_id, seek_to, match_confidence, paired_event_id, duration,
             dumps(details) if details else None, f"ev|{dedup}", now_iso()))
        if cur.rowcount == 0:
            return None
        eid = cur.lastrowid
        self.bus.publish("event", {"id": eid, "kind": kind, "device_id": device_id, "at": iso(ts), "video_id": video_id})
        return eid

    async def _transition(self, rt: _DeviceRuntime, state: str, ts: datetime, evidence: str, conn=None) -> None:
        if rt.state == state:
            return
        prev = rt.state
        rt.state = state
        sql = "UPDATE device_state SET state = ?, state_since = ?, state_evidence = ? WHERE device_id = ?"
        params = (state, iso(ts), evidence, rt.device_id)
        if conn is not None:
            await conn.execute(sql, params)
        else:
            await self.db.execute(sql, params)
        log.info("device.state", "Device state changed", device_id=rt.device_id, from_state=prev, to_state=state, evidence=evidence)
        self.bus.publish("device_state", {"device_id": rt.device_id, "state": state, "since": iso(ts), "evidence": evidence})

    # ------------------------------------------------------------------ external hooks
    async def on_config_changed(self, config: dict | None) -> None:
        summary = await self.devices.sync_from_config(config)
        for dev in self.devices.all():
            self._runtime.setdefault(dev["id"], _DeviceRuntime(dev["id"]))
        if config:
            for dev in config.get("devices", []) if isinstance(config.get("devices"), list) else []:
                if isinstance(dev, dict) and isinstance(dev.get("screen_id"), str):
                    self.parser.register_screen_id(dev["screen_id"])
        self.bus.publish("devices", summary)

    def watch_apply(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.apply_watchers.append(q)
        return q

    def unwatch_apply(self, q: asyncio.Queue) -> None:
        if q in self.apply_watchers:
            self.apply_watchers.remove(q)

    def run_fatal(self) -> str | None:
        return self._run_fatal

    def health(self) -> dict:
        d = self.status.as_dict()
        d["parser_stats"] = dict(self.parser.stats)
        d["current_cursor"] = self._cursor
        d["process_health"] = "ok"
        return d
