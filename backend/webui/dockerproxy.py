"""Single-container Docker API gate ("isbtv-docker-gate").

Runs as a separate sidecar that owns /var/run/docker.sock and exposes, on an internal network,
only what the webui needs for ONE named container:

    GET  /_ping                                   GET /version
    GET  /containers/<target>/json                (Env filtered to iSPBTV_* keys, Mounts/Network trimmed)
    GET  /containers/<target>/logs?...            (streamed)
    POST /containers/<target>/restart?t=N         (only if GATE_ALLOW_RESTART=true; t clamped to 1..60)

<target> must be the configured container name or its current ID. Everything else is 403.
Optionally a shared token (GATE_TOKEN) must be sent as ``X-Gate-Token``.

Why not a generic socket proxy: the common proxies only allow restarts together with every other
POST (container create, exec), and expose every container's environment. Mounting the Docker
socket read-only does not make the API read-only.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

SOCKET = os.getenv("GATE_DOCKER_SOCKET", "/var/run/docker.sock")
TARGET = os.getenv("GATE_TARGET_CONTAINER") or os.getenv("UPSTREAM_CONTAINER") or "iSponsorBlockTV"
ALLOW_RESTART = (os.getenv("GATE_ALLOW_RESTART", "true").lower() in {"1", "true", "yes", "on"})
TOKEN = os.getenv("GATE_TOKEN") or None
PORT = int(os.getenv("GATE_PORT", "2375"))
PATH_RE = re.compile(r"^(?:/v[\d.]+)?/containers/(?P<ref>[A-Za-z0-9_.-]+)/(?P<action>json|logs|restart)$")
LOG_PARAMS = {"stdout", "stderr", "timestamps", "follow", "since", "until", "tail"}


def log(msg: str, **fields) -> None:
    sys.stdout.write(json.dumps({"component": "docker-gate", "msg": msg, **fields}) + "\n")
    sys.stdout.flush()


class Gate:
    def __init__(self) -> None:
        self.session: ClientSession | None = None

    async def on_startup(self, app: web.Application) -> None:
        self.session = ClientSession(connector=UnixConnector(path=SOCKET), timeout=ClientTimeout(total=None, sock_connect=10))

    async def on_cleanup(self, app: web.Application) -> None:
        if self.session:
            await self.session.close()

    async def target_id(self) -> str | None:
        async with self.session.get(f"http://docker/containers/{quote(TARGET, safe='')}/json") as r:
            if r.status != 200:
                return None
            return (await r.json()).get("Id")

    async def allowed_ref(self, ref: str) -> bool:
        if ref == TARGET:
            return True
        tid = await self.target_id()
        return bool(tid) and len(ref) >= 12 and tid.startswith(ref)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if TOKEN and not hmac.compare_digest(request.headers.get("X-Gate-Token", ""), TOKEN):
            return web.json_response({"message": "gate token required"}, status=403)
        path = request.path
        if request.method == "GET" and re.match(r"^(?:/v[\d.]+)?/_ping$", path):
            return await self.forward(request, "/_ping")
        if request.method == "GET" and re.match(r"^(?:/v[\d.]+)?/version$", path):
            return await self.forward(request, "/version")
        m = PATH_RE.match(path)
        if not m or not await self.allowed_ref(m.group("ref")):
            log("denied", method=request.method, path=path)
            return web.json_response({"message": "forbidden by isbtv docker gate"}, status=403)
        ref, action = m.group("ref"), m.group("action")
        if action == "json" and request.method == "GET":
            return await self.inspect(ref)
        if action == "logs" and request.method == "GET":
            params = {k: v for k, v in request.query.items() if k in LOG_PARAMS}
            return await self.forward(request, f"/containers/{quote(ref, safe='')}/logs", params=params, stream=True)
        if action == "restart" and request.method == "POST":
            if not ALLOW_RESTART:
                return web.json_response({"message": "restart disabled (GATE_ALLOW_RESTART=false)"}, status=403)
            try:
                t = max(1, min(60, int(request.query.get("t", "15"))))
            except ValueError:
                t = 15
            log("restart", container=TARGET)
            return await self.forward(request, f"/containers/{quote(ref, safe='')}/restart", params={"t": str(t)}, method="POST")
        return web.json_response({"message": "forbidden by isbtv docker gate"}, status=403)

    async def inspect(self, ref: str) -> web.Response:
        async with self.session.get(f"http://docker/containers/{quote(ref, safe='')}/json") as r:
            body = await r.read()
            if r.status != 200:
                return web.Response(status=r.status, body=body, content_type="application/json")
        data = json.loads(body)
        cfg = data.get("Config") or {}
        cfg["Env"] = [e for e in (cfg.get("Env") or []) if e.startswith("iSPBTV_")]
        data["Config"] = cfg
        data.pop("NetworkSettings", None)
        data["Mounts"] = [{"Destination": m.get("Destination"), "RW": m.get("RW")} for m in data.get("Mounts", [])]
        hc = data.get("HostConfig") or {}
        data["HostConfig"] = {"LogConfig": {"Type": (hc.get("LogConfig") or {}).get("Type")}, "RestartPolicy": hc.get("RestartPolicy")}
        return web.json_response(data)

    async def forward(self, request: web.Request, path: str, params=None, stream=False, method="GET") -> web.StreamResponse:
        async with self.session.request(method, f"http://docker{path}", params=params) as r:
            if not stream:
                return web.Response(status=r.status, body=await r.read(), content_type=r.content_type or "application/json")
            resp = web.StreamResponse(status=r.status, headers={"Content-Type": r.headers.get("Content-Type", "application/octet-stream")})
            await resp.prepare(request)
            try:
                async for chunk in r.content.iter_any():
                    await resp.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return resp


def main() -> None:
    gate = Gate()
    app = web.Application(client_max_size=1024)
    app.router.add_route("*", "/{tail:.*}", gate.handle)
    app.on_startup.append(gate.on_startup)
    app.on_cleanup.append(gate.on_cleanup)
    log("starting", target=TARGET, restart=ALLOW_RESTART, token=bool(TOKEN), port=PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
