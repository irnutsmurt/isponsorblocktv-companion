"""Devices and pairing. Screen IDs never leave the server; pairing results are held as short-lived tickets."""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http
from pydantic import BaseModel, Field

from ..auth.deps import actor, current_session
from ..logging import get_logger
from ..util import loads, row_to_dict, screen_key
from ..upstream.config_store import ConfigError
from ..upstream.youtube import PairingError, YouTubeClient, validate_pairing_code

log = get_logger("api.devices")
router = APIRouter(prefix="/api", tags=["devices"])

TICKET_TTL = 15 * 60


class PairBody(BaseModel):
    code: str = Field(min_length=4, max_length=32)


class AddDeviceBody(BaseModel):
    ticket: str
    name: str = Field(default="", max_length=80)
    offset: int = Field(default=0, ge=-10000, le=10000)
    apply: bool = True
    expected_etag: str | None = None
    replace_device_id: int | None = None   # re-pair: replace the screen id of an existing device


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    offset: int | None = Field(default=None, ge=-10000, le=10000)
    apply: bool = True
    expected_etag: str | None = None


class RemoveBody(BaseModel):
    apply: bool = True
    expected_etag: str | None = None


def _tickets(request: Request) -> dict:
    return request.app.state.pairing_tickets


def _purge_tickets(tickets: dict) -> None:
    now = time.time()
    for k in [k for k, v in tickets.items() if v["expires"] < now]:
        tickets.pop(k, None)


async def _device_views(request: Request, range_stats: dict | None = None) -> list[dict]:
    st = request.app.state
    rows = await st.db.fetchall(
        "SELECT d.*, s.state, s.state_since, s.state_evidence, s.last_log_at, s.last_connected_at, s.last_disconnected_at, s.last_loaded_at, "
        "s.current_video_id, s.current_video_since, s.yt_availability, s.yt_availability_at, s.yt_availability_error, s.last_error, s.last_error_at, s.upstream_run_id, "
        "v.title AS current_video_title, v.channel_name AS current_channel_name "
        "FROM devices d LEFT JOIN device_state s ON s.device_id = d.id LEFT JOIN videos v ON v.video_id = s.current_video_id ORDER BY d.configured DESC, d.display_name")
    out = []
    current_run = st.collector.status.current_run_id
    for r in rows:
        d = row_to_dict(r)
        d.pop("screen_key", None)
        d["legacy_screen_id"] = d.get("screen_id_length") == 26
        d["loaded_in_current_run"] = bool(current_run and d.get("upstream_run_id") == current_run)
        if range_stats is not None:
            d["stats"] = range_stats.get(d["id"], {"skips": 0, "segment_duration_seconds": 0, "ad_skips": 0, "ad_mutes": 0, "muted_seconds": 0, "last_activity_at": None})
        out.append(d)
    return out


@router.get("/devices")
async def list_devices(request: Request, session: dict = Depends(current_session), range: str = Query("7d"), start: str | None = None, end: str | None = None):
    from ..stats.queries import range_bounds, resolve_tz
    st = request.app.state
    tz = resolve_tz(st.runtime_settings.get("timezone"))
    s, e, label = range_bounds(range, start, end, tz)
    per_device = await st.stats.per_device(s, e)
    return {"devices": await _device_views(request, per_device), "range": {"start": s.isoformat(), "end": e.isoformat(), "preset": label},
            "collection": st.collector.status.as_dict()}


@router.get("/devices/{device_id}")
async def device_detail(device_id: int, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    views = [d for d in await _device_views(request) if d["id"] == device_id]
    if not views:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Device not found.")
    d = views[0]
    d["stats"] = await st.stats.device_detail_stats(device_id)
    recent = await st.stats.activity(s=None, e=None, device_id=device_id, kinds=None, category=None, channel_key=None, search=None, limit=20, before_id=None, after_id=None)
    d["recent_activity"] = recent["items"]
    return d


@router.post("/pairing/verify")
async def pairing_verify(body: PairBody, request: Request, session: dict = Depends(current_session)):
    """Exchange a TV code with YouTube. Returns a ticket; the screen ID stays server-side."""
    st = request.app.state
    err = validate_pairing_code(body.code)
    if err:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, err)
    yt: YouTubeClient = st.youtube
    try:
        screen = await yt.pair_with_code(body.code)
    except PairingError as exc:
        log.info("pairing.failed", "TV code pairing failed", code=exc.code)
        raise HTTPException(http.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": str(exc)})
    key = screen_key(screen.screen_id)
    existing = st.devices.by_key(key)
    tickets = _tickets(request)
    _purge_tickets(tickets)
    ticket = secrets.token_urlsafe(16)
    tickets[ticket] = {"screen_id": screen.screen_id, "name": screen.name, "expires": time.time() + TICKET_TTL, "user_id": session["user_id"]}
    # Verification that does not open a lounge session: YouTube's own availability status.
    availability = None
    try:
        availability = await yt.screen_availability(screen.screen_id)
    except Exception as exc:
        log.debug("pairing.availability_failed", "Availability check after pairing failed", error=str(exc)[:120])
    await st.audit.record("pairing.verified", "ok", actor=actor(session), fields=["screen_id"], ip=request.state.info.client_ip)
    return {
        "ticket": ticket, "screen_name": screen.name, "legacy_format": len(screen.screen_id) == 26,
        "availability": availability, "expires_in": TICKET_TTL,
        "duplicate_of": {"id": existing["id"], "name": existing["display_name"], "configured": bool(existing["configured"])} if existing else None,
    }


async def _mutate_config(request: Request, session: dict, mutate, apply: bool, expected_etag: str | None, action: str, fields: list[str]) -> dict:
    st = request.app.state
    snap = st.config_store.read()
    if snap.exists and snap.parse_error:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "malformed_existing", "message": snap.parse_error})
    if expected_etag and snap.etag != expected_etag:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "conflict", "message": "config.json changed since you loaded it. Reload and try again.", "current_etag": snap.etag})
    import copy
    new_cfg = copy.deepcopy(snap.data) if snap.data else {}
    if not snap.exists:
        from ..upstream.config_schema import DEFAULTS
        new_cfg = copy.deepcopy(DEFAULTS)
    new_cfg.setdefault("devices", [])
    if not isinstance(new_cfg["devices"], list):
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "invalid", "message": "'devices' in config.json is not a list; fix it on the Configuration page first."})
    mutate(new_cfg)
    try:
        job = await st.apply.start_job(new_cfg, snap.etag if snap.exists else None, session["username"], apply)
    except ConfigError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc), **exc.details})
    await st.audit.record(action, "started", actor=actor(session), fields=fields, ip=request.state.info.client_ip)
    return job


@router.post("/devices")
async def add_device(body: AddDeviceBody, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    tickets = _tickets(request)
    _purge_tickets(tickets)
    t = tickets.get(body.ticket)
    if not t or t["user_id"] != session["user_id"]:
        raise HTTPException(http.HTTP_410_GONE, detail={"code": "ticket_expired", "message": "The pairing result expired. Enter the TV code again."})
    name = body.name.strip() or (t["name"] or "YouTube on TV")
    key = screen_key(t["screen_id"])

    def mutate(cfg: dict) -> None:
        devices = cfg["devices"]
        if body.replace_device_id is not None:
            old = st.devices.by_id(body.replace_device_id)
            if not old:
                raise HTTPException(http.HTTP_404_NOT_FOUND, "Device to re-pair not found.")
            replaced = False
            for dev in devices:
                if isinstance(dev, dict) and isinstance(dev.get("screen_id"), str) and screen_key(dev["screen_id"]) == old["screen_key"]:
                    dev["screen_id"] = t["screen_id"]
                    dev["name"] = name
                    replaced = True
            if not replaced:
                devices.append({"screen_id": t["screen_id"], "name": name, "offset": body.offset})
            return
        for dev in devices:
            if isinstance(dev, dict) and isinstance(dev.get("screen_id"), str) and screen_key(dev["screen_id"]) == key:
                raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "duplicate", "message": f"This TV is already configured as “{dev.get('name') or 'unnamed'}”."})
        devices.append({"screen_id": t["screen_id"], "name": name, "offset": body.offset})

    job = await _mutate_config(request, session, mutate, body.apply, body.expected_etag, "device.added", ["devices"])
    tickets.pop(body.ticket, None)
    return {"job": job, "device_name": name}


@router.patch("/devices/{device_id}")
async def rename_device(device_id: int, body: RenameBody, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    dev = st.devices.by_id(device_id)
    if not dev:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Device not found.")

    def mutate(cfg: dict) -> None:
        found = False
        for d in cfg["devices"]:
            if isinstance(d, dict) and isinstance(d.get("screen_id"), str) and screen_key(d["screen_id"]) == dev["screen_key"]:
                d["name"] = body.name.strip()
                if body.offset is not None:
                    d["offset"] = body.offset
                found = True
        if not found:
            raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "not_configured", "message": "This device is not in config.json any more."})

    job = await _mutate_config(request, session, mutate, body.apply, body.expected_etag, "device.renamed", ["devices"])
    return {"job": job}


@router.post("/devices/{device_id}/remove")
async def remove_device(device_id: int, body: RemoveBody, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    dev = st.devices.by_id(device_id)
    if not dev:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Device not found.")

    def mutate(cfg: dict) -> None:
        before = len(cfg["devices"])
        cfg["devices"] = [d for d in cfg["devices"] if not (isinstance(d, dict) and isinstance(d.get("screen_id"), str) and screen_key(d["screen_id"]) == dev["screen_key"])]
        if len(cfg["devices"]) == before:
            raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "not_configured", "message": "This device is not in config.json any more."})

    job = await _mutate_config(request, session, mutate, body.apply, body.expected_etag, "device.removed", ["devices"])
    return {"job": job}


@router.post("/devices/{device_id}/check-availability")
async def check_availability(device_id: int, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    dev = st.devices.by_id(device_id)
    if not dev:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Device not found.")
    snap = st.config_store.read()
    sid = None
    for d in (snap.data or {}).get("devices", []) if isinstance((snap.data or {}).get("devices"), list) else []:
        if isinstance(d, dict) and isinstance(d.get("screen_id"), str) and screen_key(d["screen_id"]) == dev["screen_key"]:
            sid = d["screen_id"]
    if not sid:
        raise HTTPException(http.HTTP_409_CONFLICT, "Device is not in config.json; availability cannot be checked.")
    return await st.availability.poll_screen(sid)


@router.delete("/devices/{device_id}/history")
async def delete_device_history(device_id: int, request: Request, session: dict = Depends(current_session)):
    """Explicit, separate from removal: forget a device that is no longer configured."""
    st = request.app.state
    dev = st.devices.by_id(device_id)
    if not dev:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Device not found.")
    if dev.get("configured"):
        raise HTTPException(http.HTTP_409_CONFLICT, "Remove the device from the configuration first.")
    async with st.db.transaction() as conn:
        await conn.execute("DELETE FROM events WHERE device_id = ?", (device_id,))
        await conn.execute("DELETE FROM playbacks WHERE device_id = ?", (device_id,))
        await conn.execute("DELETE FROM daily_totals WHERE device_id = ?", (device_id,))
        await conn.execute("DELETE FROM upstream_log WHERE device_id = ?", (device_id,))
        await conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    await st.devices.load()
    await st.audit.record("device.history_deleted", "ok", actor=actor(session), target=str(device_id), ip=request.state.info.client_ip)
    return {"ok": True}
