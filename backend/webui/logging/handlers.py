"""File handler with size+time rotation, gzip compression, retention and a storage budget.

Design constraints (see docs/logging.md):
* rotate when the active file reaches ``max_bytes`` OR the UTC day changes, whichever first;
* closed rotations are gzip-compressed on a background thread, never on the emitting thread;
* retention deletes archives older than ``retention_days``;
* the directory budget counts the active file, archives and in-progress ``.gz.tmp`` output and
  deletes the oldest archives first until under budget;
* I/O failures mark the handler degraded, write one fallback line to stderr per minute and never
  raise into the caller (no recursive logging failures).
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class BudgetedRotatingFileHandler(logging.Handler):
    ACTIVE_NAME = "webui.log"

    def __init__(
        self,
        directory: Path,
        max_bytes: int = 10 * 1024 * 1024,
        rotate_daily: bool = True,
        retention_days: int = 7,
        budget_bytes: int = 100 * 1024 * 1024,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
    ) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.rotate_daily = rotate_daily
        self.retention_days = max(1, int(retention_days))
        self.budget_bytes = max(self.max_bytes * 2, int(budget_bytes))
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self._lock = threading.RLock()
        self._stream = None
        self._size = 0
        self._open_day: str | None = None
        self._maintenance_lock = threading.Lock()
        self.degraded: str | None = None
        self._last_fallback = 0.0
        self.rotations = 0
        self.compress_failures = 0
        self._open()

    # ---- opening / writing -------------------------------------------------
    @property
    def active_path(self) -> Path:
        return self.directory / self.ACTIVE_NAME

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _open(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.directory, self.dir_mode)
            except OSError:
                pass
            fd = os.open(self.active_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, self.file_mode)
            self._stream = os.fdopen(fd, "a", encoding="utf-8")
            self._size = self.active_path.stat().st_size
            # Day of the file's first write (mtime) governs daily rotation across restarts.
            if self._size > 0:
                mtime = datetime.fromtimestamp(self.active_path.stat().st_mtime, timezone.utc)
                self._open_day = mtime.strftime("%Y%m%d")
            else:
                self._open_day = self._today()
            self.degraded = None
        except OSError as exc:
            self._stream = None
            self.degraded = f"cannot open log file: {exc}"

    def _fallback(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_fallback >= 60:
            self._last_fallback = now
            try:
                sys.stderr.write(f"[webui-logging degraded] {self.degraded}: {text[:300]}\n")
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        line = msg + "\n"
        with self._lock:
            if self._stream is None:
                self._open()
            if self._stream is None:
                self._fallback(line)
                return
            try:
                if self._should_rotate(len(line.encode("utf-8"))):
                    self._rotate()
                self._stream.write(line)
                self._stream.flush()
                self._size += len(line.encode("utf-8"))
            except OSError as exc:
                self.degraded = f"write failed: {exc}"
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                self._fallback(line)

    def _should_rotate(self, incoming: int) -> bool:
        if self._size + incoming > self.max_bytes and self._size > 0:
            return True
        if self.rotate_daily and self._open_day and self._open_day != self._today() and self._size > 0:
            return True
        return False

    def _rotate(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        target = self.directory / f"webui-{stamp}.log"
        n = 0
        # Never reuse a name whose compressed archive (or in-progress compression) already exists.
        while target.exists() or target.with_suffix(".log.gz").exists() or target.with_suffix(".log.gz.tmp").exists():
            n += 1
            target = self.directory / f"webui-{stamp}-{n}.log"
        os.replace(self.active_path, target)
        self.rotations += 1
        self._open()
        threading.Thread(target=self._compress_and_prune, args=(target,), daemon=True).start()

    def rotate_now(self) -> None:
        with self._lock:
            if self._size > 0:
                self._rotate()

    # ---- background maintenance -------------------------------------------
    def _compress_and_prune(self, path: Path) -> None:
        with self._maintenance_lock:
            gz = path.with_suffix(path.suffix + ".gz")
            tmp = gz.with_suffix(".gz.tmp")
            try:
                with open(path, "rb") as src, open(tmp, "wb") as raw:
                    os.chmod(tmp, self.file_mode)
                    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as dst:
                        shutil.copyfileobj(src, dst, 64 * 1024)
                os.replace(tmp, gz)
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.compress_failures += 1
                self.degraded = f"compression failed: {exc}"
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self.prune()

    def _archives(self) -> list[Path]:
        return sorted(
            [p for p in self.directory.glob("webui-*.log*") if p.name != self.ACTIVE_NAME],
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
        )

    def prune(self) -> dict:
        """Apply retention and budget. Returns a summary for diagnostics."""
        removed_age = removed_budget = 0
        cutoff = time.time() - self.retention_days * 86400
        try:
            for p in self._archives():
                try:
                    if p.stat().st_mtime < cutoff and not p.name.endswith(".tmp"):
                        p.unlink()
                        removed_age += 1
                except OSError:
                    continue
            total = self.directory_size()
            for p in self._archives():
                if total <= self.budget_bytes:
                    break
                try:
                    size = p.stat().st_size
                    p.unlink()
                    total -= size
                    removed_budget += 1
                except OSError:
                    continue
            if total > self.budget_bytes and self._size > 0:
                # Only the active file remains and it exceeds the budget: rotate it so it
                # becomes an archive and can be removed on the next pass.
                with self._lock:
                    self._rotate()
        except OSError as exc:
            self.degraded = f"prune failed: {exc}"
        return {"removed_for_age": removed_age, "removed_for_budget": removed_budget,
                "directory_bytes": self.directory_size()}

    def directory_size(self) -> int:
        total = 0
        try:
            for p in self.directory.iterdir():
                if p.is_file():
                    total += p.stat().st_size
        except OSError:
            pass
        return total

    def status(self) -> dict:
        return {
            "directory": str(self.directory),
            "active_bytes": self._size,
            "directory_bytes": self.directory_size(),
            "budget_bytes": self.budget_bytes,
            "max_bytes": self.max_bytes,
            "retention_days": self.retention_days,
            "rotate_daily": self.rotate_daily,
            "rotations": self.rotations,
            "compress_failures": self.compress_failures,
            "degraded": self.degraded,
            "archives": len(self._archives()),
        }

    def close(self) -> None:
        with self._lock:
            try:
                if self._stream is not None:
                    self._stream.flush()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
        super().close()
