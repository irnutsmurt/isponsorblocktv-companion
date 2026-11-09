"""Web UI log viewer, level control, temporary debug mode, upstream log viewer."""

from __future__ import annotations

import gzip
import io
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.deps import actor, current_session
from ..logging import get_logger, get_logging_state
from ..logging.core import LEVELS
from ..util import row_to_dict

log = get_logger("api.logs")
router = APIRouter(prefix="/api/logs", tags=["logs"])


class LevelBody(BaseModel):
    component: str = Field(default="", max_length=64)  # "" = global
    level: str
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)


class DebugModeBody(BaseModel):
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)


def _filter(records: list[dict], level: str | None, component: str | None, search: str | None, since: str | None, until: str | None) -> list[dict]:
    min_level = LEVELS.get((level or "DEBUG").upper(), 10)
    out = []
    s = search.lower() if search else None
    for r in records:
        if LEVELS.get(r.get("level", "INFO"), 20) < min_level:
            continue
        if component and r.get("component") != component and not (r.get("component") or "").startswith(component + "."):
            continue
        if since and r.get("ts", "") < since:
            continue
        if until and r.get("ts", "") > until:
            continue
        if s and s not in json.dumps(r, ensure_ascii=False).lower():
            continue
        out.append(r)
    return out


@router.get("/webui")
async def webui_logs(request: Request, session: dict = Depends(current_session), level: str | None = None, component: str | None = None,
                         search: str | None = None, since: str | None = None, until: str | None = None,
                         after_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    state = get_logging_state()
    ring = state.ring
    records = ring.since(after_seq, limit=100000) if after_seq else ring.snapshot()
    filtered = _filter(records, level, component, (search or "")[:100], since, until)
    page = filtered[-limit:] if not after_seq else filtered[:limit]
    components = sorted({r.get("component") for r in ring.snapshot() if r.get("component")})
    return {"records": page, "total_in_buffer": len(ring.buffer), "latest_seq": ring.seq, "components": components, "levels": state.levels.effective() if state.levels else None,
            "file": state.file_handler.status() if state.file_handler else {"enabled": False}}


@router.get("/webui/download")
async def webui_logs_download(request: Request, session: dict = Depends(current_session), level: str | None = None, component: str | None = None, search: str | None = None):
    state = get_logging_state()
    records = _filter(state.ring.snapshot(), level, component, search, None, None)
    buf = io.StringIO()
    for r in records:
        buf.write(json.dumps(r, ensure_ascii=False) + "\n")
    data = buf.getvalue().encode("utf-8")
    log.info("logs.downloaded", "Web UI logs downloaded", records=len(records), actor=session["username"])
    return StreamingResponse(iter([data]), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="webui-logs-{int(time.time())}.ndjson"'})


@router.get("/webui/files")
async def webui_log_files(request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    if not state.file_handler:
        return {"enabled": False, "files": []}
    d = state.file_handler.directory
    files = []
    for p in sorted(d.glob("webui*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        try:
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime, "compressed": p.suffix == ".gz"})
        except OSError:
            continue
    return {"enabled": True, "status": state.file_handler.status(), "files": files}


@router.get("/webui/files/{name}")
async def webui_log_file(name: str, request: Request, session: dict = Depends(current_session), tail_bytes: int = Query(256 * 1024, ge=1024, le=4 * 1024 * 1024)):
    state = get_logging_state()
    if not state.file_handler or "/" in name or ".." in name or not name.startswith("webui"):
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Not found.")
    p = state.file_handler.directory / name
    if not p.exists():
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Not found.")
    try:
        if p.suffix == ".gz":
            with gzip.open(p, "rb") as f:
                data = f.read(8 * 1024 * 1024)
        else:
            data = p.read_bytes()
    except OSError as exc:
        raise HTTPException(http.HTTP_500_INTERNAL_SERVER_ERROR, f"Cannot read file: {exc}")
    tail = data[-tail_bytes:]
    return StreamingResponse(iter([tail]), media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="{name.removesuffix(".gz")}"'})


@router.get("/levels")
async def get_levels(request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    return {"levels": state.levels.effective() if state.levels else None, "default_debug_minutes": state.debug_mode_default_minutes}


@router.put("/levels")
async def set_level(body: LevelBody, request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    if body.level.upper() not in LEVELS:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown level.")
    state.levels.set_override(body.component, body.level, body.minutes)
    log.info("logging.level.changed", "Log level override set", component=body.component or "global", level=body.level.upper(), minutes=body.minutes, actor=session["username"])
    await request.app.state.audit.record("logging.level", "ok", actor=actor(session), fields=[body.component or "global", body.level.upper()], ip=request.state.info.client_ip)
    return {"levels": state.levels.effective()}


@router.delete("/levels/{component}")
async def clear_level(component: str, request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    state.levels.clear_override("" if component == "global" else component)
    log.info("logging.level.cleared", "Log level override cleared", component=component, actor=session["username"])
    return {"levels": state.levels.effective()}


@router.post("/debug-mode")
async def debug_mode(body: DebugModeBody, request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    minutes = body.minutes or state.debug_mode_default_minutes
    state.levels.set_override("", "DEBUG", minutes)
    log.info("logging.debug_mode.enabled", "Temporary DEBUG mode enabled", minutes=minutes, actor=session["username"])
    await request.app.state.audit.record("logging.debug_mode", "ok", actor=actor(session), fields=[f"{minutes}m"], ip=request.state.info.client_ip)
    return {"levels": state.levels.effective()}


@router.delete("/debug-mode")
async def debug_mode_off(request: Request, session: dict = Depends(current_session)):
    state = get_logging_state()
    state.levels.clear_override("")
    log.info("logging.debug_mode.disabled", "Temporary DEBUG mode disabled early", actor=session["username"])
    return {"levels": state.levels.effective()}


@router.get("/upstream")
async def upstream_logs(request: Request, session: dict = Depends(current_session), level: str | None = None, device_id: int | None = None,
                        kind: str | None = None, search: str | None = None, since: str | None = None, until: str | None = None,
                        before_id: int | None = None, after_id: int | None = None, limit: int = Query(200, ge=1, le=1000)):
    st = request.app.state
    where = ["1=1"]
    params: list = []
    if level:
        lv = level.upper()
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if lv in order:
            where.append("(level IN (%s) OR level IS NULL)" % ",".join("?" * len(order[order.index(lv):])))
            params += order[order.index(lv):]
    if device_id:
        where.append("device_id = ?"); params.append(device_id)
    if kind:
        where.append("parsed = ?"); params.append(kind)
    if search:
        where.append("message LIKE ?"); params.append(f"%{search[:100]}%")
    if since:
        where.append("at >= ?"); params.append(since)
    if until:
        where.append("at <= ?"); params.append(until)
    if before_id:
        where.append("id < ?"); params.append(before_id)
    if after_id:
        where.append("id > ?"); params.append(after_id)
    order_dir = "ASC" if after_id else "DESC"
    rows = await st.db.fetchall(f"SELECT * FROM upstream_log WHERE {' AND '.join(where)} ORDER BY id {order_dir} LIMIT ?", params + [limit + 1])
    items = [row_to_dict(r) for r in rows[:limit]]
    if after_id:
        items.reverse()
    kinds = await st.db.fetchall("SELECT parsed, COUNT(*) AS n FROM upstream_log GROUP BY parsed ORDER BY n DESC LIMIT 40")
    return {"records": items, "has_more": len(rows) > limit, "kinds": [dict(k) for k in kinds], "collection": st.collector.status.as_dict()}
