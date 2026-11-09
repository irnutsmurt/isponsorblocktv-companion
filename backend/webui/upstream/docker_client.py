"""Minimal Docker Engine API client (aiohttp) for inspect, logs and a scoped restart.

Works against a unix socket or a TCP endpoint such as a docker-socket-proxy. Only the endpoints
this webui needs are implemented; nothing here can create containers, exec, or mount volumes.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import quote

import aiohttp

from ..logging import get_logger

log = get_logger("upstream.docker")


class DockerError(Exception):
    def __init__(self, message: str, code: str = "docker_error", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    state: str              # running|exited|restarting|paused|created|dead
    status_text: str
    started_at: str | None
    finished_at: str | None
    restart_count: int
    log_driver: str | None
    labels: dict
    tty: bool
    exit_code: int | None
    env_has_debug: bool
    version_label: str | None
    mounts: list[dict]


class DockerClient:
    def __init__(self, docker_host: str) -> None:
        self.docker_host = docker_host
        self._session: aiohttp.ClientSession | None = None
        self.base_url = "http://docker"
        self.available: bool | None = None
        self.last_error: str | None = None
        self.permissions: dict[str, bool | None] = {"inspect": None, "logs": None, "restart": None}

    async def start(self) -> None:
        import os
        self._headers = {"X-Gate-Token": os.environ["GATE_TOKEN"]} if os.getenv("GATE_TOKEN") else {}
        if self.docker_host.startswith("unix://"):
            path = self.docker_host[len("unix://"):]
            connector = aiohttp.UnixConnector(path=path)
        elif self.docker_host.startswith(("tcp://", "http://")):
            host = self.docker_host.split("://", 1)[1]
            self.base_url = f"http://{host}"
            connector = aiohttp.TCPConnector(limit=8)
        else:
            raise DockerError(f"Unsupported DOCKER_HOST '{self.docker_host}'", "config")
        self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise DockerError("Docker client not started", "not_started")
        return self._session

    async def _request(self, method: str, path: str, **kwargs) -> aiohttp.ClientResponse:
        try:
            headers = {**getattr(self, "_headers", {}), **kwargs.pop("headers", {})}
            resp = await self.session.request(method, self.base_url + path, headers=headers, **kwargs)
        except (aiohttp.ClientConnectorError, FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            self.available = False
            self.last_error = f"Cannot reach the Docker API at {self.docker_host}: {exc}"
            raise DockerError(self.last_error, "unreachable") from exc
        except asyncio.TimeoutError as exc:
            self.last_error = "Docker API timed out"
            raise DockerError(self.last_error, "timeout") from exc
        self.available = True
        if resp.status == 403:
            body = await resp.text()
            resp.release()
            if path.endswith("/restart"):
                self.permissions["restart"] = False
            raise DockerError(f"The Docker API gate denied this request: {body[:160]}", "forbidden", 403)
        if resp.status == 404:
            resp.release()
            raise DockerError("Container not found.", "not_found", 404)
        if resp.status >= 400:
            body = await resp.text()
            resp.release()
            raise DockerError(f"Docker API error {resp.status}: {body[:200]}", "api_error", resp.status)
        return resp

    async def ping(self) -> bool:
        try:
            resp = await self._request("GET", "/_ping")
            await resp.text()
            resp.release()
            return True
        except DockerError:
            return False

    async def version(self) -> dict:
        resp = await self._request("GET", "/version")
        try:
            return await resp.json()
        finally:
            resp.release()

    async def find_container(self, name_or_id: str) -> ContainerInfo | None:
        try:
            info = await self.inspect(name_or_id)
            self.permissions["inspect"] = True
            return info
        except DockerError as exc:
            if exc.code == "not_found":
                self.permissions["inspect"] = True
                return None
            if exc.code == "forbidden":
                self.permissions["inspect"] = False
            raise

    async def inspect(self, name_or_id: str) -> ContainerInfo:
        resp = await self._request("GET", f"/containers/{quote(name_or_id, safe='')}/json")
        try:
            data = await resp.json()
        finally:
            resp.release()
        state = data.get("State", {})
        cfg = data.get("Config", {})
        labels = cfg.get("Labels") or {}
        env = cfg.get("Env") or []
        cmd = cfg.get("Cmd") or []
        return ContainerInfo(
            id=data.get("Id", ""),
            name=(data.get("Name") or "").lstrip("/"),
            image=cfg.get("Image", ""),
            state=state.get("Status", "unknown"),
            status_text=state.get("Status", "unknown"),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
            restart_count=int(data.get("RestartCount") or 0),
            log_driver=(data.get("HostConfig", {}).get("LogConfig") or {}).get("Type"),
            labels=labels,
            tty=bool(cfg.get("Tty")),
            exit_code=state.get("ExitCode"),
            env_has_debug=("--debug" in cmd) or any(e.startswith("iSPBTV_debug") for e in env),
            version_label=labels.get("org.opencontainers.image.version") or labels.get("version"),
            mounts=[{"source": m.get("Source"), "destination": m.get("Destination"), "rw": m.get("RW")} for m in data.get("Mounts", [])],
        )

    async def logs(self, container_id: str, since_ns: str | None, follow: bool, tail: str | None = None) -> AsyncIterator[tuple[str, str, str]]:
        """Yield (stream, rfc3339_timestamp, line). ``since_ns`` is a Docker timestamp string or unix seconds."""
        params = {"stdout": "1", "stderr": "1", "timestamps": "1", "follow": "1" if follow else "0"}
        if since_ns:
            params["since"] = since_ns
        if tail:
            params["tail"] = tail
        info = await self.inspect(container_id)
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None) if follow else aiohttp.ClientTimeout(total=120)
        resp = await self._request("GET", f"/containers/{quote(container_id, safe='')}/logs", params=params, timeout=timeout)
        self.permissions["logs"] = True
        try:
            if info.tty:
                async for raw in resp.content:
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if text:
                        yield ("stdout", *_split_ts(text))
            else:
                async for stream, text in _demux(resp.content):
                    yield (stream, *_split_ts(text))
        finally:
            resp.release()

    async def restart(self, container_id: str, timeout_s: int = 15) -> None:
        resp = await self._request("POST", f"/containers/{quote(container_id, safe='')}/restart", params={"t": str(timeout_s)},
                                   timeout=aiohttp.ClientTimeout(total=timeout_s + 30))
        resp.release()
        self.permissions["restart"] = True
        log.info("docker.restart.requested", "Container restart requested", container=container_id[:12])


def _split_ts(text: str) -> tuple[str, str]:
    """Docker prefixes each line with an RFC3339Nano timestamp followed by a space."""
    idx = text.find(" ")
    if idx > 0 and text[:idx].count("-") >= 2 and "T" in text[:idx]:
        return text[:idx], text[idx + 1:]
    return "", text


async def _demux(content: aiohttp.StreamReader) -> AsyncIterator[tuple[str, str]]:
    """Decode Docker's multiplexed stream: 8-byte header (type, 0,0,0, size BE32) + payload."""
    buffers = {1: b"", 2: b""}
    names = {1: "stdout", 2: "stderr"}
    while True:
        header = await content.readexactly(8) if not content.at_eof() else b""
        if len(header) < 8:
            break
        stream_type, size = header[0], struct.unpack(">I", header[4:8])[0]
        payload = await content.readexactly(size)
        key = 1 if stream_type == 1 else 2
        buffers[key] += payload
        while b"\n" in buffers[key]:
            line, buffers[key] = buffers[key].split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                yield names[key], text
    for key, rest in buffers.items():
        text = rest.decode("utf-8", errors="replace").strip()
        if text:
            yield names[key], text
