"""Log sources: Docker logs API (follow) and a tailed log file. Both yield ``RawLine``."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ..logging import get_logger
from .docker_client import ContainerInfo, DockerClient, DockerError

log = get_logger("upstream.source")


@dataclass
class RawLine:
    text: str
    docker_ts: str | None      # RFC3339Nano from Docker, if any
    stream: str
    cursor: str                # resume cursor after this line


class SourceUnavailable(Exception):
    def __init__(self, message: str, code: str, retry_in: float = 15.0) -> None:
        super().__init__(message)
        self.code = code
        self.retry_in = retry_in


def parse_docker_ts(ts: str) -> datetime | None:
    """RFC3339Nano → aware datetime (microsecond precision)."""
    if not ts:
        return None
    try:
        base, _, frac = ts[:-1].partition(".") if ts.endswith("Z") else (ts, "", "")
        if not frac and "+" in ts:
            return datetime.fromisoformat(ts)
        micro = (frac + "000000")[:6]
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=int(micro))
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def docker_since(cursor: str | None) -> str | None:
    """Docker's ``since`` wants ``<unix seconds>.<nanoseconds>``; checkpoints store RFC3339Nano."""
    if not cursor:
        return None
    if cursor.replace(".", "", 1).isdigit():
        return cursor
    base = cursor[:-1] if cursor.endswith("Z") else cursor
    whole, _, frac = base.partition(".")
    try:
        dt = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        dt2 = parse_docker_ts(cursor)
        return f"{dt2.timestamp():.6f}" if dt2 else None
    nanos = (frac + "000000000")[:9] if frac.isdigit() else "000000000"
    return f"{int(dt.timestamp())}.{nanos}"


UNSUPPORTED_LOG_DRIVERS = {"none", "syslog", "fluentd", "gelf", "awslogs", "splunk", "etwlogs", "gcplogs", "logentries"}


class DockerLogSource:
    def __init__(self, docker: DockerClient, container_name: str) -> None:
        self.docker = docker
        self.container_name = container_name
        self.container: ContainerInfo | None = None

    async def resolve(self) -> ContainerInfo:
        try:
            info = await self.docker.find_container(self.container_name)
        except DockerError as exc:
            raise SourceUnavailable(str(exc), exc.code, retry_in=30.0 if exc.code == "unreachable" else 15.0) from exc
        if info is None:
            raise SourceUnavailable(
                f"No container named '{self.container_name}' is visible to the webui. Check UPSTREAM_CONTAINER and that both containers share the same Docker host.",
                "container_missing", retry_in=30.0)
        if info.log_driver and info.log_driver in UNSUPPORTED_LOG_DRIVERS:
            raise SourceUnavailable(
                f"The iSponsorBlockTV container uses the '{info.log_driver}' logging driver, which does not support reading logs back. Use json-file, local or journald.",
                "unsupported_log_driver", retry_in=120.0)
        self.container = info
        return info

    async def stream(self, cursor: str | None) -> AsyncIterator[RawLine]:
        info = await self.resolve()
        since = docker_since(cursor)
        try:
            async for stream, ts, text in self.docker.logs(info.id, since, follow=True):
                yield RawLine(text=text, docker_ts=ts or None, stream=stream, cursor=ts or (cursor or ""))
        except DockerError as exc:
            raise SourceUnavailable(str(exc), exc.code) from exc
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            raise SourceUnavailable(f"Log stream ended unexpectedly: {exc}", "stream_error", retry_in=5.0) from exc


class FileLogSource:
    """Tail a file written by upstream's stdout redirect. Cursor is ``inode:offset``."""

    def __init__(self, path: Path, poll_interval: float = 1.0) -> None:
        self.path = Path(path)
        self.poll = poll_interval

    def _open_state(self) -> tuple[int, int]:
        st = self.path.stat()
        return st.st_ino, st.st_size

    async def stream(self, cursor: str | None) -> AsyncIterator[RawLine]:
        if not self.path.exists():
            raise SourceUnavailable(f"Log file {self.path} does not exist yet. Make sure the iSponsorBlockTV container writes its output there.", "file_missing", retry_in=30.0)
        if not os.access(self.path, os.R_OK):
            raise SourceUnavailable(f"Log file {self.path} is not readable by the webui (uid {os.getuid()}).", "file_permission", retry_in=60.0)
        inode, size = self._open_state()
        offset = 0
        if cursor and ":" in cursor:
            c_ino, c_off = cursor.split(":", 1)
            if c_ino.isdigit() and c_off.isdigit() and int(c_ino) == inode and int(c_off) <= size:
                offset = int(c_off)
            elif int(c_ino or 0) == inode and int(c_off or 0) > size:
                log.warning("source.file.truncated", "Log file truncated; restarting from beginning", path=str(self.path))
        with open(self.path, "rb") as f:
            f.seek(offset)
            partial = b""
            while True:
                chunk = f.read(64 * 1024)
                if chunk:
                    partial += chunk
                    while b"\n" in partial:
                        line, partial = partial.split(b"\n", 1)
                        offset = f.tell() - len(partial)
                        text = line.decode("utf-8", errors="replace").rstrip("\r")
                        if text:
                            yield RawLine(text=text, docker_ts=None, stream="file", cursor=f"{inode}:{offset}")
                    continue
                await asyncio.sleep(self.poll)
                try:
                    new_inode, new_size = self._open_state()
                except FileNotFoundError:
                    log.info("source.file.rotated", "Log file disappeared (rotation?); reopening", path=str(self.path))
                    raise SourceUnavailable("Log file rotated; reopening.", "rotated", retry_in=2.0)
                if new_inode != inode:
                    log.info("source.file.rotated", "Log file inode changed; reopening", path=str(self.path))
                    raise SourceUnavailable("Log file rotated; reopening.", "rotated", retry_in=0.5)
                if new_size < f.tell():
                    log.warning("source.file.truncated", "Log file truncated; restarting from beginning", path=str(self.path))
                    f.seek(0)
                    partial = b""
