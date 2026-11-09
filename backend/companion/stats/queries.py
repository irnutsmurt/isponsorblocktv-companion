"""Metric definitions (see docs/metrics.md) implemented as SQL over events + daily_totals.

Every aggregate distinguishes: measured count, *segment duration* (SponsorBlock-reported, not
measured), *ad mute duration* (time muted, not time saved), and unknown/unavailable buckets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..db import Database
from ..util import iso, loads, row_to_dict


def resolve_tz(name: str | None) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:
        return timezone.utc


def range_bounds(preset: str | None, start: str | None, end: str | None, tz) -> tuple[datetime, datetime, str]:
    """Return (start_utc, end_utc, label). Day boundaries follow the selected timezone."""
    now_local = datetime.now(tz)
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset == "today":
        s, e = today, today + timedelta(days=1)
    elif preset == "7d":
        s, e = today - timedelta(days=6), today + timedelta(days=1)
    elif preset == "30d":
        s, e = today - timedelta(days=29), today + timedelta(days=1)
    elif preset == "all":
        s, e = datetime(2000, 1, 1, tzinfo=tz), today + timedelta(days=1)
    elif preset == "custom" and start:
        try:
            s = datetime.fromisoformat(start).replace(tzinfo=tz) if len(start) == 10 else datetime.fromisoformat(start)
            e_raw = datetime.fromisoformat(end).replace(tzinfo=tz) if end and len(end) == 10 else (datetime.fromisoformat(end) if end else None)
            e = (e_raw + timedelta(days=1)) if e_raw and len(end or "") == 10 else (e_raw or today + timedelta(days=1))
        except ValueError:
            s, e = today - timedelta(days=6), today + timedelta(days=1)
    else:
        s, e = today - timedelta(days=6), today + timedelta(days=1)
        preset = "7d"
    return s.astimezone(timezone.utc), e.astimezone(timezone.utc), preset or "7d"


class StatsQueries:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def overview(self, s: datetime, e: datetime, tz, device_id: int | None = None) -> dict:
        where = "at_epoch >= ? AND at_epoch < ?"
        params: list = [s.timestamp(), e.timestamp()]
        if device_id:
            where += " AND device_id = ?"
            params.append(device_id)
        rows = await self.db.fetchall(
            f"SELECT kind, match_confidence, COUNT(*) AS n, SUM(COALESCE(segment_duration, 0)) AS seg_dur, SUM(COALESCE(duration, 0)) AS dur "
            f"FROM events WHERE {where} GROUP BY kind, match_confidence", params)
        skips = {"count": 0, "attributed": 0, "unattributed": 0, "pending": 0, "segment_duration_seconds": 0.0}
        ad_skips = 0
        ad_mutes = {"count": 0, "muted_seconds": 0.0, "unpaired": 0}
        errors = 0
        for r in rows:
            if r["kind"] == "skip":
                skips["count"] += r["n"]
                if r["match_confidence"] in ("exact", "nearest"):
                    skips["attributed"] += r["n"]
                    skips["segment_duration_seconds"] += r["seg_dur"] or 0
                elif r["match_confidence"] == "pending":
                    skips["pending"] += r["n"]
                else:
                    skips["unattributed"] += r["n"]
            elif r["kind"] == "ad_skip":
                ad_skips += r["n"]
            elif r["kind"] == "ad_mute":
                ad_mutes["count"] += r["n"]
                ad_mutes["muted_seconds"] += r["dur"] or 0
            elif r["kind"] in ("error", "warning"):
                errors += r["n"]
        unpaired = await self.db.scalar(f"SELECT COUNT(*) FROM events WHERE {where} AND kind = 'ad_mute' AND duration IS NULL", params)
        ad_mutes["unpaired"] = unpaired or 0
        # Rolled-up totals (older than detailed retention) overlapping the range
        day_s, day_e = s.astimezone(timezone.utc).strftime("%Y-%m-%d"), e.astimezone(timezone.utc).strftime("%Y-%m-%d")
        dparams: list = [day_s, day_e]
        dwhere = "day >= ? AND day < ?"
        if device_id:
            dwhere += " AND device_id = ?"
            dparams.append(device_id)
        rolled = await self.db.fetchall(f"SELECT kind, SUM(count) AS n, SUM(duration) AS d FROM daily_totals WHERE {dwhere} GROUP BY kind", dparams)
        rolled_d = {r["kind"]: {"count": r["n"], "duration": r["d"]} for r in rolled}
        playbacks = await self.db.scalar(
            "SELECT COUNT(*) FROM playbacks WHERE started_at >= ? AND started_at < ?" + (" AND device_id = ?" if device_id else ""),
            [iso(s), iso(e)] + ([device_id] if device_id else []))
        videos = await self.db.scalar(
            "SELECT COUNT(DISTINCT video_id) FROM playbacks WHERE started_at >= ? AND started_at < ?" + (" AND device_id = ?" if device_id else ""),
            [iso(s), iso(e)] + ([device_id] if device_id else []))
        return {
            "range": {"start": iso(s), "end": iso(e)},
            "skips": skips,
            "ad_skips": {"count": ad_skips},
            "ad_mutes": ad_mutes,
            "errors": errors,
            "playbacks": playbacks or 0,
            "distinct_videos": videos or 0,
            "rolled_up": rolled_d,  # included in totals? No: shown separately as 'archived' because categories/devices are coarser
        }

    async def categories(self, s: datetime, e: datetime, device_id: int | None = None) -> list[dict]:
        where = "kind = 'skip' AND at_epoch >= ? AND at_epoch < ?"
        params: list = [s.timestamp(), e.timestamp()]
        if device_id:
            where += " AND device_id = ?"
            params.append(device_id)
        rows = await self.db.fetchall(f"SELECT categories, match_confidence, segment_duration FROM events WHERE {where}", params)
        agg: dict[str, dict] = {}
        for r in rows:
            cats = loads(r["categories"], None)
            if not cats:
                key = "pending" if r["match_confidence"] == "pending" else "unknown"
                cats = [key]
            share = (r["segment_duration"] or 0) / len(cats)
            for c in cats:
                a = agg.setdefault(c, {"category": c, "count": 0, "segment_duration_seconds": 0.0})
                a["count"] += 1
                a["segment_duration_seconds"] += share
        return sorted(agg.values(), key=lambda a: -a["count"])

    async def trend(self, s: datetime, e: datetime, tz, device_id: int | None = None, bucket: str = "auto") -> list[dict]:
        where = "at_epoch >= ? AND at_epoch < ? AND kind IN ('skip','ad_skip','ad_mute')"
        params: list = [s.timestamp(), e.timestamp()]
        if device_id:
            where += " AND device_id = ?"
            params.append(device_id)
        rows = await self.db.fetchall(f"SELECT kind, at_epoch, segment_duration, duration FROM events WHERE {where}", params)
        buckets: dict[str, dict] = {}
        span_hours = (e - s).total_seconds() / 3600
        use_hour = bucket == "hour" or (bucket == "auto" and span_hours <= 48)
        for r in rows:
            dt = datetime.fromtimestamp(r["at_epoch"], timezone.utc).astimezone(tz)
            key = dt.strftime("%Y-%m-%dT%H:00") if use_hour else dt.strftime("%Y-%m-%d")
            b = buckets.setdefault(key, {"bucket": key, "skips": 0, "ad_skips": 0, "ad_mutes": 0, "segment_duration_seconds": 0.0, "muted_seconds": 0.0})
            if r["kind"] == "skip":
                b["skips"] += 1
                b["segment_duration_seconds"] += r["segment_duration"] or 0
            elif r["kind"] == "ad_skip":
                b["ad_skips"] += 1
            else:
                b["ad_mutes"] += 1
                b["muted_seconds"] += r["duration"] or 0
        # fill empty buckets so charts show zeros vs gaps correctly
        out = []
        cur = s.astimezone(tz)
        step = timedelta(hours=1) if use_hour else timedelta(days=1)
        cur = cur.replace(minute=0, second=0, microsecond=0) if use_hour else cur.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = e.astimezone(tz)
        guard = 0
        while cur < end_local and guard < 2000:
            key = cur.strftime("%Y-%m-%dT%H:00") if use_hour else cur.strftime("%Y-%m-%d")
            out.append(buckets.get(key, {"bucket": key, "skips": 0, "ad_skips": 0, "ad_mutes": 0, "segment_duration_seconds": 0.0, "muted_seconds": 0.0}))
            cur += step
            guard += 1
        return out

    async def per_device(self, s: datetime, e: datetime) -> dict[int, dict]:
        rows = await self.db.fetchall(
            "SELECT device_id, kind, COUNT(*) AS n, SUM(COALESCE(segment_duration,0)) AS seg, SUM(COALESCE(duration,0)) AS dur, MAX(at) AS last_at "
            "FROM events WHERE at_epoch >= ? AND at_epoch < ? AND kind IN ('skip','ad_skip','ad_mute') GROUP BY device_id, kind",
            (s.timestamp(), e.timestamp()))
        out: dict[int, dict] = {}
        for r in rows:
            d = out.setdefault(r["device_id"], {"skips": 0, "segment_duration_seconds": 0.0, "ad_skips": 0, "ad_mutes": 0, "muted_seconds": 0.0, "last_activity_at": None})
            if r["kind"] == "skip":
                d["skips"] = r["n"]
                d["segment_duration_seconds"] = r["seg"] or 0
            elif r["kind"] == "ad_skip":
                d["ad_skips"] = r["n"]
            else:
                d["ad_mutes"] = r["n"]
                d["muted_seconds"] = r["dur"] or 0
            if r["last_at"] and (d["last_activity_at"] is None or r["last_at"] > d["last_activity_at"]):
                d["last_activity_at"] = r["last_at"]
        return out

    async def channels(self, s: datetime, e: datetime, device_id: int | None, limit: int, offset: int, search: str | None) -> dict:
        where = "e.kind = 'skip' AND e.at_epoch >= ? AND e.at_epoch < ?"
        params: list = [s.timestamp(), e.timestamp()]
        if device_id:
            where += " AND e.device_id = ?"
            params.append(device_id)
        if search:
            where += " AND (v.channel_name LIKE ? OR v.title LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        sql = (
            "SELECT COALESCE(v.channel_key, '__unknown__') AS channel_key, MAX(v.channel_id) AS channel_id, MAX(v.channel_name) AS channel_name, MAX(v.channel_url) AS channel_url, "
            "COUNT(*) AS skips, SUM(CASE WHEN e.match_confidence IN ('exact','nearest') THEN 1 ELSE 0 END) AS attributed, "
            "SUM(COALESCE(e.segment_duration, 0)) AS segment_duration_seconds, COUNT(DISTINCT e.video_id) AS videos, MAX(e.at) AS last_at, "
            "SUM(CASE WHEN v.meta_status = 'pending' THEN 1 ELSE 0 END) AS pending_meta "
            f"FROM events e LEFT JOIN videos v ON v.video_id = e.video_id WHERE {where} GROUP BY COALESCE(v.channel_key, '__unknown__') ORDER BY skips DESC LIMIT ? OFFSET ?")
        rows = await self.db.fetchall(sql, params + [limit, offset])
        total = await self.db.scalar(f"SELECT COUNT(DISTINCT COALESCE(v.channel_key, '__unknown__')) FROM events e LEFT JOIN videos v ON v.video_id = e.video_id WHERE {where}", params)
        items = []
        for r in rows:
            d = row_to_dict(r)
            d["unknown"] = d["channel_key"] == "__unknown__"
            items.append(d)
        return {"items": items, "total": total or 0}

    async def channel_videos(self, channel_key: str, s: datetime, e: datetime, limit: int = 50) -> list[dict]:
        if channel_key == "__unknown__":
            where = "v.channel_key IS NULL"
            params: list = []
        else:
            where = "v.channel_key = ?"
            params = [channel_key]
        rows = await self.db.fetchall(
            "SELECT e.video_id, MAX(v.title) AS title, COUNT(*) AS skips, SUM(COALESCE(e.segment_duration,0)) AS segment_duration_seconds, MAX(e.at) AS last_at "
            f"FROM events e LEFT JOIN videos v ON v.video_id = e.video_id WHERE e.kind = 'skip' AND e.at_epoch >= ? AND e.at_epoch < ? AND {where} "
            "GROUP BY e.video_id ORDER BY last_at DESC LIMIT ?", [s.timestamp(), e.timestamp()] + params + [limit])
        return [row_to_dict(r) for r in rows]

    async def activity(self, *, s: datetime | None, e: datetime | None, device_id: int | None, kinds: list[str] | None,
                       category: str | None, channel_key: str | None, search: str | None, limit: int, before_id: int | None, after_id: int | None) -> dict:
        where = ["1=1"]
        params: list = []
        if s:
            where.append("e.at_epoch >= ?"); params.append(s.timestamp())
        if e:
            where.append("e.at_epoch < ?"); params.append(e.timestamp())
        if device_id:
            where.append("e.device_id = ?"); params.append(device_id)
        if kinds:
            where.append("e.kind IN (%s)" % ",".join("?" * len(kinds))); params += kinds
        if category:
            if category == "unknown":
                where.append("e.kind = 'skip' AND (e.categories IS NULL OR e.categories = '[]')")
            else:
                where.append("e.categories LIKE ?"); params.append(f'%"{category}"%')
        if channel_key:
            if channel_key == "__unknown__":
                where.append("v.channel_key IS NULL")
            else:
                where.append("v.channel_key = ?"); params.append(channel_key)
        if search:
            where.append("(v.title LIKE ? OR v.channel_name LIKE ? OR e.video_id LIKE ? OR e.details LIKE ?)")
            params += [f"%{search}%"] * 4
        if before_id:
            where.append("e.id < ?"); params.append(before_id)
        order = "ASC" if after_id else "DESC"
        if after_id:
            where.append("e.id > ?"); params.append(after_id)
        sql = (
            "SELECT e.*, v.title, v.channel_name, v.channel_key, v.channel_id, v.channel_url, v.meta_status, v.duration AS video_duration, d.display_name AS device_name "
            "FROM events e LEFT JOIN videos v ON v.video_id = e.video_id LEFT JOIN devices d ON d.id = e.device_id "
            f"WHERE {' AND '.join(where)} ORDER BY e.id {order} LIMIT ?")
        rows = await self.db.fetchall(sql, params + [limit + 1])
        items = [row_to_dict(r) for r in rows[:limit]]
        for it in items:
            it["categories"] = loads(it.get("categories"), None)
            it["segment_uuids"] = loads(it.get("segment_uuids"), None)
            it["details"] = loads(it.get("details"), None)
        if after_id:
            items.reverse()
        return {"items": items, "has_more": len(rows) > limit}

    async def device_detail_stats(self, device_id: int) -> dict:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS skips, SUM(COALESCE(segment_duration,0)) AS seg, MIN(at) AS first_at, MAX(at) AS last_at FROM events WHERE device_id = ? AND kind = 'skip'", (device_id,))
        ads = await self.db.fetchone(
            "SELECT SUM(CASE WHEN kind='ad_skip' THEN 1 ELSE 0 END) AS ad_skips, SUM(CASE WHEN kind='ad_mute' THEN 1 ELSE 0 END) AS ad_mutes, SUM(CASE WHEN kind='ad_mute' THEN COALESCE(duration,0) ELSE 0 END) AS muted FROM events WHERE device_id = ?", (device_id,))
        rolled = await self.db.fetchall("SELECT kind, SUM(count) AS n, SUM(duration) AS d FROM daily_totals WHERE device_id = ? GROUP BY kind", (device_id,))
        return {"lifetime": {"skips": row["skips"] or 0, "segment_duration_seconds": row["seg"] or 0, "first_at": row["first_at"], "last_at": row["last_at"],
                             "ad_skips": ads["ad_skips"] or 0, "ad_mutes": ads["ad_mutes"] or 0, "muted_seconds": ads["muted"] or 0},
                "archived": {r["kind"]: {"count": r["n"], "duration": r["d"]} for r in rolled}}
