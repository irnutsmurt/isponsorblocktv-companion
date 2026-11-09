"""Upstream configuration: read with etag, validate, save/apply, backups, restore."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http
from pydantic import BaseModel

from ..auth.deps import actor, current_session, recent_auth
from ..logging import get_logger
from ..upstream.config_schema import DEVICE_FIELDS, FIELDS, SKIP_CATEGORIES, changed_fields, effective, redact_config, unknown_keys, validate_config
from ..upstream.config_store import ConfigError

log = get_logger("api.config")
router = APIRouter(prefix="/api/config", tags=["config"])


class SaveBody(BaseModel):
    config: dict[str, Any]
    expected_etag: str | None = None
    apply: bool = True


class ValidateBody(BaseModel):
    config: dict[str, Any]


def _public_view(request: Request) -> dict:
    st = request.app.state
    snap = st.config_store.read()
    data = snap.data
    view = {
        "exists": snap.exists, "etag": snap.etag, "mtime": snap.mtime, "size": snap.size, "path": snap.path,
        "parse_error": snap.parse_error, "writable": snap.writable, "write_problem": snap.write_problem,
        "owner": snap.owner, "mode": snap.mode, "directory": st.config_store.directory_report(),
    }
    if data is not None:
        eff = effective(data)
        if eff.get("apikey"):
            eff["apikey"] = "[REDACTED]"  # never sent to the browser
        eff.pop("devices", None)        # screen IDs never leave the server; see view["devices"]
        view["effective"] = eff
        devices = []
        for i, dev in enumerate(data.get("devices", []) if isinstance(data.get("devices"), list) else []):
            if isinstance(dev, dict):
                from ..util import screen_key, screen_hint
                sid = dev.get("screen_id") if isinstance(dev.get("screen_id"), str) else ""
                known = st.devices.by_key(screen_key(sid)) if sid else None
                devices.append({"index": i, "name": dev.get("name"), "offset": dev.get("offset", 0), "has_screen_id": bool(sid),
                                "screen_hint": screen_hint(sid) if sid else None, "legacy_format": len(sid) == 26,
                                "device_id": known["id"] if known else None,
                                "extra_keys": sorted(k for k in dev if k not in {"screen_id", "name", "offset"})})
            else:
                devices.append({"index": i, "invalid": True})
        view["devices"] = devices
        view["unknown_keys"] = unknown_keys(data)
        view["has_legacy_atvs"] = "atvs" in data
        v = validate_config(data)
        view["validation"] = {"ok": v.ok, "errors": v.errors, "warnings": v.warnings}
        view["redacted_raw"] = redact_config(data)
    return view


@router.get("")
async def get_config(request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    view = _public_view(request)
    view["fields"] = FIELDS
    view["device_fields"] = DEVICE_FIELDS
    view["categories"] = [{"label": l, "value": v} for l, v in SKIP_CATEGORIES]
    view["apply_capability"] = await st.apply.capability()
    view["latest_job"] = await st.apply.latest_job()
    view["apply_busy"] = st.apply.busy()
    return view


@router.post("/validate")
async def validate(body: ValidateBody, request: Request, session: dict = Depends(current_session)):
    v = validate_config(body.config)
    return {"ok": v.ok, "errors": v.errors, "warnings": v.warnings}


@router.put("")
async def save_config(body: SaveBody, request: Request, session: dict = Depends(current_session)):
    """Replace non-device settings (and device names/offsets) while preserving screen IDs and unknown keys.

    The client never sees screen IDs, so the submitted ``devices`` entries carry ``device_id`` (or index)
    plus editable fields; everything else is merged from the file on disk.
    """
    st = request.app.state
    snap = st.config_store.read()
    if snap.exists and snap.parse_error:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "malformed_existing", "message": snap.parse_error})
    if body.expected_etag and snap.etag != body.expected_etag:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": "conflict", "message": "config.json changed on disk since you loaded it. Review the current values and try again.", "current_etag": snap.etag})
    import copy
    from ..upstream.config_schema import DEFAULTS
    base = copy.deepcopy(snap.data) if snap.data is not None else copy.deepcopy(DEFAULTS)
    incoming = body.config
    editable = {f["key"] for f in FIELDS}
    for key in editable:
        if key in incoming:
            if key == "apikey" and incoming[key] == "[REDACTED]":
                continue
            base[key] = incoming[key]
    # devices: match by index within the current file; only name/offset are editable here
    if isinstance(incoming.get("devices"), list) and isinstance(base.get("devices"), list):
        for item in incoming["devices"]:
            if not isinstance(item, dict) or "index" not in item:
                continue
            idx = item["index"]
            if isinstance(idx, int) and 0 <= idx < len(base["devices"]) and isinstance(base["devices"][idx], dict):
                if "name" in item and isinstance(item["name"], str):
                    base["devices"][idx]["name"] = item["name"]
                if "offset" in item and isinstance(item["offset"], (int, float)) and not isinstance(item["offset"], bool):
                    base["devices"][idx]["offset"] = item["offset"]
    if incoming.get("remove_legacy_atvs") is True:
        base.pop("atvs", None)
    changed = changed_fields(snap.data or {}, base)
    v = validate_config(base)
    if not v.ok:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid", "message": "Configuration is invalid.", "errors": v.errors, "warnings": v.warnings})
    try:
        job = await st.apply.start_job(base, snap.etag if snap.exists else None, session["username"], body.apply)
    except ConfigError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc), **exc.details})
    await st.audit.record("config.saved", "started", actor=actor(session), fields=changed, ip=request.state.info.client_ip)
    return {"job": job, "changed": changed, "warnings": v.warnings}


@router.get("/jobs/latest")
async def latest_job(request: Request, session: dict = Depends(current_session)):
    return {"job": await request.app.state.apply.latest_job(), "busy": request.app.state.apply.busy()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request, session: dict = Depends(current_session)):
    try:
        return await request.app.state.apply.get_job(job_id)
    except ConfigError:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Job not found.")


@router.post("/apply")
async def apply_only(request: Request, session: dict = Depends(current_session)):
    """Restart upstream to apply a configuration that was saved earlier without applying."""
    st = request.app.state
    snap = st.config_store.read()
    if not snap.exists or snap.data is None:
        raise HTTPException(http.HTTP_409_CONFLICT, "There is no valid config.json to apply.")
    try:
        job = await st.apply.start_job(snap.data, snap.etag, session["username"], True)
    except ConfigError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    await st.audit.record("config.apply", "started", actor=actor(session), ip=request.state.info.client_ip)
    return {"job": job}


@router.get("/backups")
async def list_backups(request: Request, session: dict = Depends(current_session)):
    return {"backups": await request.app.state.config_store.list_backups()}


@router.get("/backups/{backup_id}")
async def view_backup(backup_id: int, request: Request, session: dict = Depends(current_session)):
    try:
        meta, data = await request.app.state.config_store.read_backup(backup_id)
    except ConfigError as exc:
        raise HTTPException(http.HTTP_404_NOT_FOUND, str(exc))
    v = validate_config(data)
    return {"backup": meta, "redacted": redact_config(data), "effective": effective(data) if isinstance(data, dict) else None,
            "device_count": len(data.get("devices", [])) if isinstance(data, dict) and isinstance(data.get("devices"), list) else 0,
            "validation": {"ok": v.ok, "errors": v.errors, "warnings": v.warnings}}


@router.post("/backups/{backup_id}/restore")
async def restore_backup(backup_id: int, request: Request, session: dict = Depends(recent_auth), apply: bool = True):
    st = request.app.state
    try:
        meta, data = await st.config_store.read_backup(backup_id)
        job = await st.apply.start_job(data, None, session["username"], apply)
    except ConfigError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc), **exc.details})
    await st.audit.record("config.restored", "started", actor=actor(session), target=meta["filename"], ip=request.state.info.client_ip)
    return {"job": job}
