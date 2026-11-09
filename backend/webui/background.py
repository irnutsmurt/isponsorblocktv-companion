"""Housekeeping: retention roll-ups, log pruning, temporary debug expiry, session purge, WAL checkpoint."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

from .db import Database, DatabaseUnavailable
from .logging import get_logger, get_logging_state
from .util import iso, loads, utcnow

log = get_logger("maintenance")


class Maintenance:
    def __init__(self, app_state) -> None:
        self.st = app_state
        self._task: asyncio.Task | None = None
        self.last_run: dict = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        tick = 0
        while True:
            try:
                self._expire_debug()
                if tick % 120 == 0:        # hourly (30 s tick)
                    await self.hourly()
                if tick % 2880 == 0 and tick > 0:   # daily
                    await self.daily()
            except asyncio.CancelledError:
                raise
            except DatabaseUnavailable:
                pass
            except Exception as exc:
                log.exception("maintenance.tick.failed", "Maintenance tick failed", error=str(exc))
            tick += 1
            await asyncio.sleep(30 * (0.9 + random.random() * 0.2))

    def _expire_debug(self) -> None:
        state = get_logging_state()
        if state.levels:
            expired = state.levels.expire()
            for name in expired:
                log.info("logging.debug_mode.expired", "Temporary log level override expired; previous level restored",
                         component=name or "global", levels=state.levels.effective())

    async def hourly(self) -> None:
        db: Database = self.st.db
        s = self.st.settings
        await self.st.sessions.purge_expired()
        await db.execute("DELETE FROM webauthn_challenges WHERE expires_at < ?", (iso(utcnow() - timedelta(hours=1)),))
        # upstream raw log retention: by age and by row count
        cutoff = (utcnow() - timedelta(days=s.raw_log_retention_days)).timestamp()
        removed = await db.execute("DELETE FROM upstream_log WHERE at_epoch < ?", (cutoff,))
        count = await db.scalar("SELECT COUNT(*) FROM upstream_log")
        if count and count > s.raw_log_max_rows:
            excess = count - s.raw_log_max_rows
            removed += await db.execute("DELETE FROM upstream_log WHERE id IN (SELECT id FROM upstream_log ORDER BY id LIMIT ?)", (excess,))
        if removed:
            log.info("retention.upstream_log", "Pruned raw upstream log rows", removed=removed, remaining=await db.scalar("SELECT COUNT(*) FROM upstream_log"))
        try:
            await db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        state = get_logging_state()
        if state.file_handler:
            summary = await asyncio.to_thread(state.file_handler.prune)
            if summary.get("removed_for_age") or summary.get("removed_for_budget"):
                log.info("logging.prune", "Log archives pruned", **summary)
        self.last_run["hourly"] = iso(utcnow())

    async def daily(self) -> None:
        await self.rollup_and_prune_events()
        self.last_run["daily"] = iso(utcnow())

    async def rollup_and_prune_events(self) -> dict:
        """Roll detailed events older than the retention window into daily_totals, then delete them."""
        db: Database = self.st.db
        s = self.st.settings
        cutoff_dt = utcnow() - timedelta(days=s.event_retention_days)
        cutoff = cutoff_dt.timestamp()
        rows = await db.fetchall(
            "SELECT id, kind, device_id, at, categories, segment_duration, duration FROM events WHERE at_epoch < ? AND kind IN ('skip','ad_skip','ad_mute') ORDER BY at_epoch LIMIT 20000",
            (cutoff,))
        if not rows:
            return {"rolled": 0}
        async with db.transaction() as conn:
            for r in rows:
                day = r["at"][:10]
                if r["kind"] == "skip":
                    cats = loads(r["categories"], []) or ["unknown"]
                    for c in cats:
                        await conn.execute(
                            "INSERT INTO daily_totals (day, device_id, kind, category, count, duration) VALUES (?, ?, 'skip', ?, 1, ?) "
                            "ON CONFLICT(day, device_id, kind, category) DO UPDATE SET count = count + 1, duration = duration + excluded.duration",
                            (day, r["device_id"], c, (r["segment_duration"] or 0) / max(1, len(cats))))
                elif r["kind"] == "ad_skip":
                    await conn.execute(
                        "INSERT INTO daily_totals (day, device_id, kind, category, count, duration) VALUES (?, ?, 'ad_skip', '', 1, 0) "
                        "ON CONFLICT(day, device_id, kind, category) DO UPDATE SET count = count + 1", (day, r["device_id"]))
                elif r["kind"] == "ad_mute":
                    await conn.execute(
                        "INSERT INTO daily_totals (day, device_id, kind, category, count, duration) VALUES (?, ?, 'ad_mute', '', 1, ?) "
                        "ON CONFLICT(day, device_id, kind, category) DO UPDATE SET count = count + 1, duration = duration + excluded.duration",
                        (day, r["device_id"], r["duration"] or 0))
            ids = [r["id"] for r in rows]
            await conn.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in ids])
            await conn.execute("DELETE FROM events WHERE at_epoch < ? AND kind NOT IN ('skip','ad_skip','ad_mute')", (cutoff,))
            await conn.execute("DELETE FROM playbacks WHERE started_at < ?", (iso(cutoff_dt),))
        log.info("retention.events", "Rolled up and pruned detailed events", rolled=len(rows), cutoff=iso(cutoff_dt))
        return {"rolled": len(rows)}
