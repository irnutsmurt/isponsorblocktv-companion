"""Health, diagnostics bundle, runtime settings, audit log, and the SSE live stream."""

from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import sys
import time
import zipfile
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..auth.deps import actor, current_session, read_session_token, recent_auth
from ..auth.passkeys import origin_allows_passkeys
from ..logging import get_logger, get_logging_state
from ..upstream.config_schema import redact_config
from ..util import iso, now_iso, utcnow, from_epoch


def iso_from_epoch(ts: float) -> str | None:
    return iso(from_epoch(ts))

log = get_logger("api.system")
router = APIRouter(tags=["system"])

RUNTIME_SETTING_KEYS = {"timezone", "public_origin", "youtube_api_key", "theme_default"}


class RuntimeSettingsBody(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)
    public_origin: str | None = Field(default=None, max_length=256)
    youtube_api_key: str | None = Field(default=None, max_length=128)
    clear_youtube_api_key: bool = False
    trust_proxy: str | None = Field(default=None, max_length=64)     # IP or CIDR to add
    untrust_proxy: str | None = Field(default=None, max_length=64)   # runtime entry to remove


@router.get("/healthz")
async def healthz(request: Request):
    """Public, minimal: process liveness only. Collection health is private (see /api/health)."""
    st = request.app.state
    ok = await st.db.ping()
    code = 200 if ok else 503
    return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=code)


async def _full_health(request: Request) -> dict:
    st = request.app.state
    logging_state = get_logging_state()
    docker = None
    if st.docker:
        docker = {"host": st.settings.docker_host, "available": st.docker.available, "last_error": st.docker.last_error, "permissions": st.docker.permissions}
    config_snap = st.config_store.read()
    gaps_open = await st.db.scalar("SELECT COUNT(*) FROM collection_gaps WHERE ended_at IS NULL")
    disk = None
    try:
        import shutil
        u = shutil.disk_usage(st.settings.data_dir)
        disk = {"total": u.total, "free": u.free}
    except OSError:
        pass
    return {
        "version": __version__,
        "started_at": st.started_at,
        "uptime_seconds": int(time.time() - st.started_monotonic_epoch),
        "process": {"python": sys.version.split()[0], "platform": platform.platform(), "uid": os.getuid(), "gid": os.getgid(), "pid": os.getpid()},
        "database": await st.db.stats(),
        "disk": disk,
        "collection": st.collector.health(),
        "open_gaps": gaps_open or 0,
        "enrichment": st.enrichment.health(),
        "availability_poll": {"interval_minutes": st.settings.availability_poll_minutes, "last_run": st.availability.last_run},
        "docker": docker,
        "upstream_config": {"exists": config_snap.exists, "writable": config_snap.writable, "write_problem": config_snap.write_problem, "parse_error": config_snap.parse_error, "etag": config_snap.etag[:12]},
        "logging": logging_state.health(),
        "apply_capability": await st.apply.capability(),
        "sse_clients": st.bus.subscribers,
        "maintenance": st.maintenance.last_run,
        "settings": st.settings.describe(),
        "runtime_settings": {k: ("[set]" if k == "youtube_api_key" and v else v) for k, v in st.runtime_settings.items()},
    }


@router.get("/api/health")
async def health(request: Request, session: dict = Depends(current_session)):
    return await _full_health(request)


@router.get("/api/capabilities")
async def capabilities(request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    col = st.collector.status
    source_ok = col.status == "connected"
    return {
        "parser_version": col.parser_version,
        "supported_upstream": ">=2.5,<3 (verified against 2.11.0)",
        "detected_upstream_version": (col.container or {}).get("version"),
        "source": {"kind": col.source_kind, "status": col.status, "detail": col.detail},
        "capabilities": [
            {"name": "Configured devices", "status": "observed", "available": st.config_store.read().exists, "source": "config.json"},
            {"name": "Device loaded by upstream", "status": "observed", "available": source_ok, "source": "log: Refreshed auth"},
            {"name": "Connection events", "status": "observed (edges)", "available": source_ok, "source": "log: Connected to device / Subscribe completed"},
            {"name": "Current video", "status": "observed", "available": source_ok, "source": "log: Playing video"},
            {"name": "Segment skip (requested)", "status": "observed", "available": source_ok, "source": "log: Skipping segment"},
            {"name": "Segment category & duration", "status": "inferred", "available": source_ok, "source": "SponsorBlock API match on seek target"},
            {"name": "Skip confirmed on TV", "status": "unavailable", "available": False, "source": "upstream never logs the seek result"},
            {"name": "Ad skip / mute (requested)", "status": "observed", "available": source_ok, "source": "log: Ad can be skipped / Ad has started"},
            {"name": "Video title / channel", "status": "inferred", "available": st.settings.enrichment_enabled, "source": "oEmbed / YouTube Data API"},
            {"name": "TV online/offline (YouTube)", "status": "observed (polled)", "available": st.settings.availability_poll_minutes > 0, "source": "pairing/get_screen_availability"},
            {"name": "Upstream process state", "status": "observed", "available": bool(st.docker and st.docker.available), "source": "Docker API inspect"},
            {"name": "Apply via restart", "status": "supported", "available": (await st.apply.capability())["mode"] == "restart", "source": "Docker API restart"},
            {"name": "Hot reload", "status": "unavailable", "available": False, "source": "upstream reads config.json only at startup"},
        ],
    }


@router.get("/api/diagnostics/bundle")
async def diagnostics_bundle(request: Request, session: dict = Depends(recent_auth), include_upstream_logs: bool = True, include_config: bool = False):
    """Sanitized zip: health, capabilities, redacted config (optional), recent webui + upstream logs. Bounded size."""
    st = request.app.state
    health = await _full_health(request)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "iSponsorBlockTV Web UI diagnostic bundle\nGenerated: %s\nSecrets, screen IDs, cookies and the database are excluded. Device names and video IDs are private operational data.\n" % now_iso())
        z.writestr("health.json", json.dumps(health, indent=2, default=str))
        z.writestr("capabilities.json", json.dumps(await capabilities(request, session), indent=2, default=str))
        if include_config:
            snap = st.config_store.read()
            z.writestr("config.redacted.json", json.dumps(redact_config(snap.data) if snap.data else {"exists": snap.exists, "parse_error": snap.parse_error}, indent=2))
        ring = get_logging_state().ring.snapshot()[-3000:]
        z.writestr("webui-logs.ndjson", "\n".join(json.dumps(r, ensure_ascii=False) for r in ring))
        if include_upstream_logs:
            rows = await st.db.fetchall("SELECT at, level, logger, parsed, message FROM upstream_log ORDER BY id DESC LIMIT 3000")
            z.writestr("upstream-logs.ndjson", "\n".join(json.dumps(dict(r), ensure_ascii=False) for r in reversed(rows)))
        gaps = await st.db.fetchall("SELECT * FROM collection_gaps ORDER BY id DESC LIMIT 200")
        z.writestr("collection-gaps.json", json.dumps([dict(g) for g in gaps], indent=2))
        jobs = await st.db.fetchall("SELECT * FROM apply_jobs ORDER BY id DESC LIMIT 20")
        z.writestr("apply-jobs.json", json.dumps([dict(j) for j in jobs], indent=2, default=str))
        audit = await st.audit.recent(200)
        z.writestr("audit.json", json.dumps(audit, indent=2, default=str))
    data = buf.getvalue()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(http.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Diagnostic bundle exceeded the 8 MiB limit.")
    await st.audit.record("diagnostics.exported", "ok", actor=actor(session), fields=["include_config" if include_config else "no_config"], ip=request.state.info.client_ip)
    return StreamingResponse(iter([data]), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="webui-diagnostics-{int(time.time())}.zip"'})


@router.get("/api/audit")
async def audit_log(request: Request, session: dict = Depends(current_session), limit: int = 100):
    return {"items": await request.app.state.audit.recent(limit)}


@router.get("/api/settings")
async def get_runtime_settings(request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    rs = st.runtime_settings
    origin = rs.get("public_origin") or st.settings.public_origin
    info = request.state.info
    hint = getattr(st, "proxy_hint", None)
    detected = None
    if hint and time.time() - hint["at"] < 24 * 3600 and not info.via_trusted_proxy:
        detected = {"address": hint["peer"], "seen_at": iso_from_epoch(hint["at"]), "forwarded_proto": hint.get("forwarded_proto")}
    return {
        "proxy": {
            "connected_from": info.peer,
            "via_trusted_proxy": info.via_trusted_proxy,
            "client_ip": info.client_ip,
            "https_detected_by": info.scheme_source,
            "trusted_env": [str(n) for n in st.settings.trusted_proxies],
            "trusted_runtime": [str(n) for n in getattr(st, "runtime_trusted_proxies", [])],
            "detected_untrusted": detected,
        },
        "timezone": rs.get("timezone") or "UTC",
        "public_origin": origin,
        "public_origin_source": "runtime" if rs.get("public_origin") else ("env" if st.settings.public_origin else None),
        "current_origin": request.state.info.origin,
        "passkey_blocker": origin_allows_passkeys(origin) if origin else "Public origin not configured.",
        "youtube_api_key_set": bool(rs.get("youtube_api_key") or st.settings.youtube_api_key_env),
        "youtube_api_key_source": "runtime" if rs.get("youtube_api_key") else ("env" if st.settings.youtube_api_key_env else None),
        "enrichment_enabled": st.settings.enrichment_enabled,
        "insecure_transport": not request.state.info.is_secure,
    }


@router.put("/api/settings")
async def put_runtime_settings(body: RuntimeSettingsBody, request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    changed = []
    if body.timezone is not None:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(body.timezone)
        except Exception:
            raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown timezone.")
        await st.db.set_state("timezone", body.timezone)
        st.runtime_settings["timezone"] = body.timezone
        changed.append("timezone")
    if body.public_origin is not None:
        origin = body.public_origin.strip().rstrip("/")
        if origin:
            from urllib.parse import urlsplit
            p = urlsplit(origin)
            if p.scheme not in {"http", "https"} or not p.hostname or p.path not in {"", "/"}:
                raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Public origin must look like https://host[:port] with no path.")
        await st.db.set_state("public_origin", origin or None)
        if origin:
            st.runtime_settings["public_origin"] = origin
        else:
            st.runtime_settings.pop("public_origin", None)
        changed.append("public_origin")
    if body.clear_youtube_api_key:
        await st.db.set_state("youtube_api_key", None)
        st.runtime_settings.pop("youtube_api_key", None)
        changed.append("youtube_api_key")
    elif body.youtube_api_key:
        await st.db.set_state("youtube_api_key", body.youtube_api_key.strip())
        st.runtime_settings["youtube_api_key"] = body.youtube_api_key.strip()
        st.enrichment.status["api_key_rejected"] = False
        changed.append("youtube_api_key")
    if body.trust_proxy or body.untrust_proxy:
        import ipaddress
        from ..settings import parse_trusted
        current = [str(n) for n in getattr(st, "runtime_trusted_proxies", [])]
        if body.trust_proxy:
            try:
                net = ipaddress.ip_network(body.trust_proxy.strip(), strict=False)
            except ValueError:
                raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Enter an IP address or a network like 172.31.0.0/16.")
            if net.prefixlen == 0:
                raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Trusting every address is not allowed.")
            if str(net) not in current:
                current.append(str(net))
        if body.untrust_proxy:
            current = [c for c in current if c != body.untrust_proxy.strip()]
        await st.db.set_state("trusted_proxies", ",".join(current) or None)
        st.runtime_trusted_proxies = parse_trusted(",".join(current))
        st.proxy_hint = None
        changed.append("trusted_proxies")
        from ..context import request_info
        request.state.info = request_info(request)  # reflect the new trust in the response
    await st.audit.record("settings.changed", "ok", actor=actor(session), fields=changed, ip=request.state.info.client_ip)
    return await get_runtime_settings(request, session)


@router.get("/api/stream")
async def stream(request: Request, session: dict = Depends(current_session)):
    """Server-sent events. The session is re-validated every 60 s; revoked/expired sessions are cut off."""
    st = request.app.state
    q = st.bus.subscribe()
    token = read_session_token(request)
    session_id = session["id"]

    async def gen():
        last_check = time.monotonic()
        try:
            yield f"event: hello\ndata: {json.dumps({'seq': st.bus.seq, 'collection': st.collector.status.as_dict()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {msg['seq']}\nevent: {msg['type']}\ndata: {json.dumps(msg['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                if time.monotonic() - last_check > 60:
                    last_check = time.monotonic()
                    try:
                        current = await st.sessions.lookup(token)
                    except Exception:
                        current = None
                    if current is None or current["id"] != session_id:
                        log.info("sse.session_ended", "Closing live stream: session expired or revoked")
                        yield "event: session_ended\ndata: {}\n\n"
                        break
        finally:
            st.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
