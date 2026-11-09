"""Application factory and process entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
from pathlib import Path

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .api import config as config_api
from .api import devices as devices_api
from .api import logs as logs_api
from .api import stats as stats_api
from .api import system as system_api
from .auth import router as auth_router
from .auth.audit import AuditService
from .auth.bootstrap import BootstrapService
from .auth.passkeys import PasskeyService
from .auth.passwords import PasswordService
from .auth.ratelimit import RateLimiter
from .auth.recovery import RecoveryService
from .auth.sessions import SessionService
from .background import Maintenance
from .context import request_info
from .db import Database, DatabaseUnavailable
from .events_bus import EventBus
from .logging import get_logger, request_id_var, setup_logging
from .logging.core import shutdown_logging
from .settings import Settings, get_settings
from .stats.queries import StatsQueries
from .upstream.apply import ApplyService
from .upstream.availability import AvailabilityPoller
from .upstream.config_store import ConfigStore
from .upstream.devices import DeviceRegistry
from .upstream.docker_client import DockerClient
from .upstream.enrichment import EnrichmentWorker
from .upstream.ingest import Collector
from .upstream.parser import UpstreamLogParser
from .upstream.youtube import YouTubeClient
from .util import now_iso

log = get_logger("app")
STATIC_DIR = Path(__file__).parent / "static"


class SecurityMiddleware(BaseHTTPMiddleware):
    """Request IDs, body-size limit, security headers, access logging."""

    def __init__(self, app, max_body: int) -> None:
        super().__init__(app)
        self.max_body = max_body

    async def dispatch(self, request: Request, call_next):
        rid = secrets.token_hex(6)
        token = request_id_var.set(rid)
        start = time.monotonic()
        try:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > self.max_body:
                return JSONResponse({"detail": "Request body too large."}, status_code=413)
            response = await call_next(request)
        except DatabaseUnavailable as exc:
            response = JSONResponse({"detail": "Storage unavailable."}, status_code=503)
            log.critical("db.request_failed_closed", "Request failed closed: database unavailable", error=str(exc))
        finally:
            request_id_var.reset(token)
        request_id_var.set(rid)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com https://img.youtube.com; "
                "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=15552000"
        duration = (time.monotonic() - start) * 1000
        if request.url.path.startswith("/api/") and request.url.path != "/api/stream":
            lg = log.debug if response.status_code < 400 else log.info
            lg("http.request", f"{request.method} {request.url.path} → {response.status_code}", method=request.method,
               path=request.url.path, status=response.status_code, duration_ms=round(duration, 1))
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await _startup(app, settings)
        try:
            yield
        finally:
            await _shutdown(app)

    app = FastAPI(title="iSponsorBlockTV Companion", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.runtime_settings = {}
    app.state.pairing_tickets = {}
    app.state.started_at = now_iso()
    app.state.started_monotonic_epoch = time.time()

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Invalid request.", "errors": exc.errors()[:10]}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("http.unhandled", "Unhandled error while serving a request", path=request.url.path)
        return JSONResponse({"detail": "Internal error. Check the companion logs (request id in header)."}, status_code=500)

    app.add_middleware(SecurityMiddleware, max_body=settings.max_request_bytes)

    app.include_router(auth_router.router)
    app.include_router(devices_api.router)
    app.include_router(config_api.router)
    app.include_router(stats_api.router)
    app.include_router(logs_api.router)
    app.include_router(system_api.router)

    # ---- static SPA ----
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        if STATIC_DIR.exists():
            candidate = (STATIC_DIR / path).resolve()
            if path and candidate.is_file() and str(candidate).startswith(str(STATIC_DIR.resolve())):
                headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in str(candidate) else {"Cache-Control": "no-cache"}
                return FileResponse(candidate, headers=headers)
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return Response("Frontend not built. Run `npm run build` in frontend/.", media_type="text/plain", status_code=503)

    return app


async def _startup(app: FastAPI, settings: Settings) -> None:
    st = app.state
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.data_dir, 0o700)
    except OSError:
        pass
    log.info("app.starting", f"iSponsorBlockTV Companion {__version__} starting", version=__version__, settings=settings.describe())

    db = Database(settings.db_path)
    try:
        await db.open()
    except Exception as exc:
        log.critical("db.open_failed", "Cannot open the companion database; authentication will fail closed", error=str(exc), exc_info=True)
        db.healthy = False
    st.db = db

    if db.healthy:
        for key in ("timezone", "public_origin", "youtube_api_key"):
            v = await db.get_state(key)
            if v:
                st.runtime_settings[key] = v
        if st.runtime_settings.get("youtube_api_key"):
            from .logging.redaction import register_secret
            register_secret(st.runtime_settings["youtube_api_key"], "YOUTUBE_API_KEY")

    st.passwords = PasswordService(settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism)
    st.sessions = SessionService(db, settings.session_idle_hours, settings.session_absolute_days, settings.remember_days, settings.reauth_minutes)
    st.limiter = RateLimiter(db)
    st.bootstrap = BootstrapService(db, settings.setup_token_env, settings.setup_token_ttl_minutes)
    st.recovery = RecoveryService(db, st.passwords)
    st.passkeys = PasskeyService(db)
    st.audit = AuditService(db)
    st.bus = EventBus()

    st.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300), timeout=aiohttp.ClientTimeout(total=30))
    st.youtube = YouTubeClient(st.http)

    st.docker = None
    if settings.log_source in {"auto", "docker"}:
        docker = DockerClient(settings.docker_host)
        try:
            await docker.start()
            st.docker = docker
            if await docker.ping():
                log.info("docker.connected", "Docker API reachable", host=settings.docker_host)
            else:
                log.warning("docker.unreachable", "Docker API not reachable at startup; will keep retrying", host=settings.docker_host, error=docker.last_error)
        except Exception as exc:
            log.warning("docker.init_failed", "Docker client could not be initialised", error=str(exc))
            st.docker = None

    st.config_store = ConfigStore(settings.upstream_config_file, settings.backup_dir, db, settings.config_backups_keep)
    st.devices = DeviceRegistry(db)
    st.parser = UpstreamLogParser(settings.upstream_tz)
    st.stats = StatsQueries(db)

    async def config_provider() -> dict | None:
        snap = st.config_store.read()
        return snap.data

    st.collector = Collector(db, st.bus, st.devices, st.parser, st.docker, settings)
    st.collector.config_provider = config_provider
    st.enrichment = EnrichmentWorker(db, st.bus, st.http, settings, config_provider, st.runtime_settings)
    st.availability = AvailabilityPoller(db, st.bus, st.http, st.devices, config_provider, settings.availability_poll_minutes)
    st.apply = ApplyService(db, st.bus, st.config_store, st.collector, st.docker, settings)
    st.maintenance = Maintenance(st)

    if db.healthy:
        await st.devices.load()
        snap = st.config_store.read()
        if snap.exists:
            if snap.parse_error:
                log.error("config.malformed", "Upstream config.json is malformed; it will not be modified", error=snap.parse_error, path=snap.path)
            else:
                await st.collector.on_config_changed(snap.data)
                log.info("config.loaded", "Upstream config.json loaded", path=snap.path, devices=len((snap.data or {}).get("devices", []) or []), etag=snap.etag[:12])
        else:
            log.warning("config.missing", "No upstream config.json yet; setup wizard will create one", path=snap.path, writable=snap.writable)
        await st.collector.start()
        await st.enrichment.start()
        await st.availability.start()
        await st.maintenance.start()
        st.config_watch = asyncio.create_task(_watch_config(st), name="config-watch")
        await st.bootstrap.announce_if_needed()
    log.info("app.started", "Companion ready", port=settings.port, auth_required=True)


async def _watch_config(st) -> None:
    """Detect external edits of config.json and refresh devices (never auto-overwrites anything)."""
    last_etag = st.config_store.read().etag
    while True:
        await asyncio.sleep(10)
        try:
            snap = st.config_store.read()
            if snap.etag != last_etag:
                own = snap.etag == st.config_store.last_written_etag
                if not own:
                    log.info("config.changed_externally", "config.json changed outside the companion", previous=last_etag[:12], current=snap.etag[:12], parse_error=snap.parse_error)
                last_etag = snap.etag
                if own:
                    continue
                if snap.data is not None:
                    await st.collector.on_config_changed(snap.data)
                st.bus.publish("config_changed", {"etag": snap.etag, "parse_error": snap.parse_error})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("config.watch.failed", "Config watcher error", error=str(exc))


async def _shutdown(app: FastAPI) -> None:
    st = app.state
    log.info("app.stopping", "Companion shutting down")
    for name in ("config_watch",):
        t = getattr(st, name, None)
        if t:
            t.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await t
    for svc in ("maintenance", "availability", "enrichment", "collector"):
        s = getattr(st, svc, None)
        if s:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await s.stop()
    if getattr(st, "docker", None):
        await st.docker.close()
    if getattr(st, "http", None):
        await st.http.close()
    if getattr(st, "db", None):
        await st.db.close()
    log.info("app.stopped", "Companion stopped")
    shutdown_logging()


def configure_logging(settings: Settings) -> None:
    setup_logging(
        level=settings.log_level, component_levels=settings.component_levels(), fmt=settings.log_format,
        file_enabled=settings.log_file_enabled, log_dir=settings.log_dir, file_max_bytes=settings.log_file_max_bytes,
        rotate_daily=settings.log_file_rotate_daily, retention_days=settings.log_retention_days,
        budget_bytes=settings.log_dir_budget_bytes, ring_size=settings.log_ring_size,
        debug_mode_default_minutes=settings.debug_mode_default_minutes,
    )


def _ensure_tls(settings: Settings) -> tuple[str, str] | None:
    if settings.tls_cert_file and settings.tls_key_file:
        return str(settings.tls_cert_file), str(settings.tls_key_file)
    if settings.tls_self_signed:
        from .tls import ensure_self_signed
        cert, key = ensure_self_signed(settings.data_dir / "tls", settings.public_origin)
        return str(cert), str(key)
    return None


def run() -> None:
    import uvicorn
    settings = get_settings()
    configure_logging(settings)
    tls = _ensure_tls(settings)
    app = create_app(settings)
    kwargs = {}
    if tls:
        kwargs = {"ssl_certfile": tls[0], "ssl_keyfile": tls[1]}
        log.info("tls.enabled", "Serving HTTPS", cert=tls[0])
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False, proxy_headers=False,
                timeout_graceful_shutdown=10, **kwargs)


if __name__ == "__main__":
    run()
