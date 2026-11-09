"""Optional, cached, rate-limited metadata enrichment. Independent of ingestion: failures retry later."""

from __future__ import annotations

import asyncio
import random
import time

import aiohttp

from ..db import Database, DatabaseUnavailable
from ..events_bus import EventBus
from ..logging import get_logger
from ..util import dumps, loads, now_iso
from .sponsorblock import SponsorBlockClient, match_seek, merge_segments
from .youtube import YouTubeClient

log = get_logger("enrichment")

BACKOFF_SECONDS = [60, 600, 3600, 6 * 3600, 24 * 3600]
MAX_PER_TICK = 15


def _next_attempt(attempts: int) -> float:
    idx = min(attempts, len(BACKOFF_SECONDS) - 1)
    return time.time() + BACKOFF_SECONDS[idx] * (0.8 + random.random() * 0.4)


class EnrichmentWorker:
    def __init__(self, db: Database, bus: EventBus, http: aiohttp.ClientSession, settings, config_provider, runtime_settings: dict) -> None:
        self.db = db
        self.bus = bus
        self.yt = YouTubeClient(http)
        self.http = http
        self.settings = settings
        self.config_provider = config_provider
        self.runtime_settings = runtime_settings
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.status = {"last_tick": None, "metadata_ok": 0, "metadata_failed": 0, "sb_ok": 0, "sb_failed": 0,
                       "sb_rate_limited_until": None, "api_key_rejected": False, "last_error": None}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="enrichment")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while not self._stop.is_set():
            try:
                await self.tick()
            except DatabaseUnavailable:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["last_error"] = str(exc)[:200]
                log.exception("enrichment.tick.failed", "Enrichment tick failed", error=str(exc))
            await asyncio.sleep(15)

    def _api_key(self) -> str | None:
        return self.runtime_settings.get("youtube_api_key") or self.settings.youtube_api_key_env

    async def tick(self) -> dict:
        self.status["last_tick"] = now_iso()
        config = await self.config_provider() if self.config_provider else None
        done = {"sb": 0, "meta": 0}
        done["sb"] = await self._tick_sponsorblock(config)
        if self.settings.enrichment_enabled:
            done["meta"] = await self._tick_metadata()
        return done

    # ---- SponsorBlock attribution ----------------------------------------------
    async def _tick_sponsorblock(self, config: dict | None) -> int:
        cfg = config or {}
        categories = cfg.get("skip_categories")
        if categories is None:
            categories = ["sponsor"]
        if not isinstance(categories, list) or not categories:
            categories = ["sponsor"]
        minimum = cfg.get("minimum_skip_length", 1)
        if not isinstance(minimum, (int, float)):
            minimum = 1
        api_url = cfg.get("sponsorblock_api_url") or "https://sponsor.ajay.app/api/"
        client = SponsorBlockClient(self.http, api_url)
        now = time.time()
        rows = await self.db.fetchall(
            "SELECT v.video_id, v.sb_attempts FROM videos v WHERE v.sb_status = 'pending' AND (v.sb_next_attempt_at IS NULL OR v.sb_next_attempt_at <= ?) "
            "AND EXISTS (SELECT 1 FROM events e WHERE e.video_id = v.video_id AND e.kind = 'skip' AND e.match_confidence = 'pending') "
            "ORDER BY v.last_seen_at DESC LIMIT ?", (now, MAX_PER_TICK))
        count = 0
        for row in rows:
            vid = row["video_id"]
            try:
                segments = await client.fetch_segments(vid, [c for c in categories if isinstance(c, str)])
            except RuntimeError:
                self.status["sb_rate_limited_until"] = now_iso()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.status["sb_failed"] += 1
                await self.db.execute("UPDATE videos SET sb_attempts = sb_attempts + 1, sb_next_attempt_at = ? WHERE video_id = ?", (_next_attempt(row["sb_attempts"]), vid))
                log.warning("enrichment.sb.failed", "SponsorBlock lookup failed; will retry", video_id=vid, error=str(exc)[:200])
                continue
            if segments is None:
                await self.db.execute("UPDATE videos SET sb_status = 'none', sb_fetched_at = ?, sb_segments = NULL WHERE video_id = ?", (now_iso(), vid))
                await self.db.execute("UPDATE events SET match_confidence = 'none', enriched_at = ? WHERE video_id = ? AND kind = 'skip' AND match_confidence = 'pending'", (now_iso(), vid))
                count += 1
                continue
            merged = merge_segments(segments, float(minimum))
            duration = next((s.get("videoDuration") for s in segments if s.get("videoDuration")), None)
            await self.db.execute("UPDATE videos SET sb_status = 'ok', sb_fetched_at = ?, sb_segments = ?, duration = COALESCE(duration, ?) WHERE video_id = ?",
                                  (now_iso(), dumps(segments), duration, vid))
            events = await self.db.fetchall("SELECT id, seek_to FROM events WHERE video_id = ? AND kind = 'skip' AND match_confidence = 'pending'", (vid,))
            for ev in events:
                seg, confidence = match_seek(merged, float(ev["seek_to"] or 0))
                if seg:
                    await self.db.execute(
                        "UPDATE events SET segment_start = ?, segment_end = ?, segment_duration = ?, categories = ?, segment_uuids = ?, match_confidence = ?, enriched_at = ? WHERE id = ?",
                        (seg["start"], seg["end"], seg["end"] - seg["start"], dumps(seg["categories"]), dumps(seg["uuids"]), confidence, now_iso(), ev["id"]))
                else:
                    await self.db.execute("UPDATE events SET match_confidence = 'none', enriched_at = ? WHERE id = ?", (now_iso(), ev["id"]))
            self.status["sb_ok"] += 1
            count += 1
            log.debug("enrichment.sb.ok", "SponsorBlock segments attributed", video_id=vid, merged=len(merged), events=len(events))
        if count:
            self.bus.publish("enriched", {"videos": count})
        return count

    # ---- title / channel metadata ------------------------------------------------
    async def _tick_metadata(self) -> int:
        now = time.time()
        rows = await self.db.fetchall(
            "SELECT video_id, meta_attempts FROM videos WHERE meta_status IN ('pending', 'failed') AND (meta_next_attempt_at IS NULL OR meta_next_attempt_at <= ?) "
            "ORDER BY last_seen_at DESC LIMIT ?", (now, MAX_PER_TICK))
        count = 0
        api_key = self._api_key()
        for row in rows:
            vid = row["video_id"]
            meta = None
            source = None
            try:
                if api_key and not self.status["api_key_rejected"]:
                    try:
                        meta = await self.yt.data_api_video(vid, api_key)
                        source = "youtube_data_api"
                    except PermissionError:
                        self.status["api_key_rejected"] = True
                        log.warning("enrichment.api_key.rejected", "YouTube Data API key rejected or over quota; falling back to oEmbed")
                if meta is None:
                    meta = await self.yt.oembed(vid)
                    source = "oembed"
            except aiohttp.ClientResponseError as exc:
                if exc.status == 429:
                    log.warning("enrichment.meta.rate_limited", "YouTube metadata rate limited; pausing until next tick")
                    break
                meta = None
                self.status["metadata_failed"] += 1
                await self.db.execute("UPDATE videos SET meta_status = 'failed', meta_attempts = meta_attempts + 1, meta_next_attempt_at = ? WHERE video_id = ?", (_next_attempt(row["meta_attempts"]), vid))
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.status["metadata_failed"] += 1
                await self.db.execute("UPDATE videos SET meta_status = 'failed', meta_attempts = meta_attempts + 1, meta_next_attempt_at = ? WHERE video_id = ?", (_next_attempt(row["meta_attempts"]), vid))
                log.warning("enrichment.meta.failed", "Metadata lookup failed; will retry", video_id=vid, error=str(exc)[:200])
                continue
            if meta is None:
                await self.db.execute("UPDATE videos SET meta_status = 'not_found', meta_fetched_at = ?, meta_attempts = meta_attempts + 1 WHERE video_id = ?", (now_iso(), vid))
                count += 1
                continue
            channel_key = meta.get("channel_id") or meta.get("channel_url")
            await self.db.execute(
                "UPDATE videos SET title = ?, channel_key = ?, channel_id = COALESCE(?, channel_id), channel_name = ?, channel_url = ?, meta_status = 'ok', meta_fetched_at = ?, meta_source = ?, meta_attempts = meta_attempts + 1 WHERE video_id = ?",
                (meta.get("title"), channel_key, meta.get("channel_id"), meta.get("channel_name"), meta.get("channel_url"), now_iso(), source, vid))
            self.status["metadata_ok"] += 1
            count += 1
        if count:
            self.bus.publish("enriched", {"videos": count})
        return count

    def health(self) -> dict:
        return dict(self.status, enabled=self.settings.enrichment_enabled, api_key_configured=bool(self._api_key()))
