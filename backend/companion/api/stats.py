"""Dashboard, channels and activity endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http

from ..auth.deps import current_session
from ..stats.queries import range_bounds, resolve_tz
from ..util import iso

router = APIRouter(prefix="/api", tags=["stats"])


def _range(request: Request, range: str, start: str | None, end: str | None):
    st = request.app.state
    tz = resolve_tz(st.runtime_settings.get("timezone"))
    s, e, label = range_bounds(range, start, end, tz)
    return s, e, label, tz


async def _collection_summary(request: Request) -> dict:
    st = request.app.state
    status = st.collector.status.as_dict()
    gaps = await st.db.fetchall("SELECT * FROM collection_gaps ORDER BY id DESC LIMIT 20")
    status["recent_gaps"] = [dict(g) for g in gaps]
    status["enrichment"] = st.enrichment.health()
    status["stale"] = status["status"] != "connected"
    return status


@router.get("/stats/overview")
async def overview(request: Request, session: dict = Depends(current_session), range: str = Query("7d"), start: str | None = None, end: str | None = None, device_id: int | None = None):
    st = request.app.state
    s, e, label, tz = _range(request, range, start, end)
    data = await st.stats.overview(s, e, tz, device_id)
    data["categories"] = await st.stats.categories(s, e, device_id)
    data["trend"] = await st.stats.trend(s, e, tz, device_id)
    data["range"]["preset"] = label
    data["timezone"] = str(tz)
    data["collection"] = await _collection_summary(request)
    recent = await st.stats.activity(s=None, e=None, device_id=device_id, kinds=["skip", "ad_skip", "ad_mute"], category=None, channel_key=None, search=None, limit=10, before_id=None, after_id=None)
    data["recent"] = recent["items"]
    return data


@router.get("/channels")
async def channels(request: Request, session: dict = Depends(current_session), range: str = Query("30d"), start: str | None = None, end: str | None = None,
                   device_id: int | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), search: str | None = None):
    st = request.app.state
    s, e, label, tz = _range(request, range, start, end)
    data = await st.stats.channels(s, e, device_id, limit, offset, (search or "").strip()[:100] or None)
    data["range"] = {"start": iso(s), "end": iso(e), "preset": label}
    return data


@router.get("/channels/{channel_key:path}/videos")
async def channel_videos(channel_key: str, request: Request, session: dict = Depends(current_session), range: str = Query("30d"), start: str | None = None, end: str | None = None):
    st = request.app.state
    s, e, label, tz = _range(request, range, start, end)
    return {"videos": await st.stats.channel_videos(channel_key, s, e)}


@router.get("/activity")
async def activity(request: Request, session: dict = Depends(current_session), range: str = Query("all"), start: str | None = None, end: str | None = None,
                   device_id: int | None = None, kind: str | None = None, category: str | None = None, channel_key: str | None = None,
                   search: str | None = None, limit: int = Query(50, ge=1, le=200), before_id: int | None = None, after_id: int | None = None):
    st = request.app.state
    if range == "all" and not start:
        s = e = None
        tz = resolve_tz(st.runtime_settings.get("timezone"))
    else:
        s, e, label, tz = _range(request, range, start, end)
    kinds = [k for k in (kind or "").split(",") if k] or None
    allowed = {"skip", "ad_skip", "ad_mute", "ad_unmute", "connected", "disconnected", "loaded", "error", "warning"}
    if kinds and any(k not in allowed for k in kinds):
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown activity kind.")
    data = await st.stats.activity(s=s, e=e, device_id=device_id, kinds=kinds, category=(category or None), channel_key=(channel_key or None),
                                   search=(search or "").strip()[:100] or None, limit=limit, before_id=before_id, after_id=after_id)
    data["timezone"] = str(tz)
    return data
