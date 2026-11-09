"""Central logging: structured records, levels, redaction, aggregation, ring buffer, sinks.

Record fields (JSON sink): ts (UTC ISO-8601), level, component, event, msg, request_id,
plus any structured fields passed by the caller (after redaction), ``repeat`` for aggregated
records and ``exc`` for sanitized stack traces.
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import logging.handlers
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .handlers import BudgetedRotatingFileHandler
from .redaction import redact_mapping, redact_text

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
ROOT_NAME = "companion"
# A record carrying this flag is written only to the stdout/stderr console sink: used for the
# one-time bootstrap token announcement, which must not land in files or the ring buffer.
CONSOLE_ONLY_FLAG = "_console_only"


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, CONSOLE_ONLY_FLAG, False):
            return True  # the bootstrap announcement is the single sanctioned secret on the console sink
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                try:
                    record.msg = record.msg % record.args
                except Exception:
                    record.msg = f"{record.msg} {record.args}"
                record.args = ()
                record.msg = redact_text(record.msg)
            fields = getattr(record, "fields", None)
            if fields:
                record.fields = redact_mapping(fields)
            if record.exc_info and record.exc_info[0] is not None:
                text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text_sanitized = redact_text(text)[-12000:]
                record.exc_info = None
        except Exception:
            record.fields = {"redaction_error": True}
        return True


class RepeatSuppressor(logging.Filter):
    """Collapse identical WARNING+ (component, event) within ``window`` seconds.

    The first occurrence passes; later ones are counted. When the window elapses the next
    record (of any kind) passes through together with a summary of what was suppressed.
    """

    def __init__(self, window: float = 60.0) -> None:
        super().__init__()
        self.window = window
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str, int], list] = {}  # key -> [first_ts, count, last_msg]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or getattr(record, CONSOLE_ONLY_FLAG, False):
            return True
        event = getattr(record, "event", None) or record.getMessage()[:80]
        key = (record.name, event, record.levelno)
        now = time.monotonic()
        with self._lock:
            entry = self._seen.get(key)
            if entry is None:
                self._seen[key] = [now, 0, None]
                return True
            first_ts, count, _ = entry
            if now - first_ts < self.window:
                entry[1] = count + 1
                entry[2] = record.getMessage()
                return False
            # window elapsed: let this one through, annotate with suppressed count
            if count:
                record.repeat = count
            self._seen[key] = [now, 0, None]
            return True

    def pending_summary(self) -> list[dict]:
        with self._lock:
            return [
                {"component": k[0], "event": k[1], "level": logging.getLevelName(k[2]), "suppressed": v[1]}
                for k, v in self._seen.items() if v[1]
            ]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, CONSOLE_ONLY_FLAG, False):
            # Human-facing announcement (setup token box): printed verbatim so NAS log viewers show it legibly.
            return record.getMessage()
        return json.dumps(record_to_dict(record), ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, CONSOLE_ONLY_FLAG, False):
            return record.getMessage()
        d = record_to_dict(record)
        extras = {k: v for k, v in d.items() if k not in {"ts", "level", "component", "event", "msg", "exc"}}
        line = f"{d['ts']} {d['level']:<8} {d['component']}: {d['msg']}"
        if d.get("event"):
            line += f"  [{d['event']}]"
        if extras:
            line += "  " + json.dumps(extras, ensure_ascii=False, default=str)
        if d.get("exc"):
            line += "\n" + d["exc"]
        return line


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    d: dict[str, Any] = {
        "ts": _utc_iso(record.created),
        "level": record.levelname,
        "component": record.name.removeprefix(ROOT_NAME + ".") if record.name != ROOT_NAME else "app",
        "event": getattr(record, "event", None),
        "msg": record.getMessage(),
    }
    rid = getattr(record, "request_id", None)
    if rid:
        d["request_id"] = rid
    fields = getattr(record, "fields", None)
    if fields:
        for k, v in fields.items():
            if k not in d:
                d[k] = v
    if getattr(record, "repeat", None):
        d["repeat"] = record.repeat
    exc = getattr(record, "exc_text_sanitized", None)
    if exc:
        d["exc"] = exc
    return d


class RingBufferHandler(logging.Handler):
    """Bounded in-memory history for the authenticated log viewer and SSE tail."""

    def __init__(self, size: int) -> None:
        super().__init__()
        self.buffer: collections.deque[dict] = collections.deque(maxlen=size)
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, CONSOLE_ONLY_FLAG, False):
            return
        try:
            d = record_to_dict(record)
        except Exception:
            return
        with self._lock:
            self.seq += 1
            d["seq"] = self.seq
            self.buffer.append(d)

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            return [d for d in self.buffer if d["seq"] > seq][:limit]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.buffer)


class ConsoleOnlyFilter(logging.Filter):
    """Blocks console-only records from any sink other than the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, CONSOLE_ONLY_FLAG, False)


class LevelManager:
    """Global + per-component levels, with optional expiring overrides (temporary debug)."""

    def __init__(self, base_level: str, component_levels: dict[str, str]) -> None:
        self.base_level = base_level if base_level in LEVELS else "INFO"
        self.base_components = {k: v for k, v in component_levels.items() if v in LEVELS}
        self.overrides: dict[str, tuple[str, float | None]] = {}  # name ('' = global) -> (level, expires_at)
        self.previous_global: str | None = None
        self._lock = threading.Lock()
        self.apply()

    def effective(self) -> dict[str, Any]:
        with self._lock:
            g = self.overrides.get("")
            comps = dict(self.base_components)
            temp = {}
            for name, (lvl, exp) in self.overrides.items():
                if name:
                    comps[name] = lvl
                    temp[name] = exp
            return {
                "global": g[0] if g else self.base_level,
                "global_base": self.base_level,
                "global_override_expires_at": _utc_iso(g[1]) if g and g[1] else None,
                "components": comps,
                "component_override_expires": {k: (_utc_iso(v) if v else None) for k, v in temp.items()},
            }

    def apply(self) -> None:
        with self._lock:
            g = self.overrides.get("")
            logging.getLogger(ROOT_NAME).setLevel(LEVELS[g[0] if g else self.base_level])
            names = set(self.base_components) | {n for n in self.overrides if n}
            for name in names:
                override = self.overrides.get(name)
                lvl = override[0] if override else self.base_components.get(name)
                logger = logging.getLogger(f"{ROOT_NAME}.{name}")
                logger.setLevel(LEVELS[lvl] if lvl else logging.NOTSET)
            # components that lost their override and had no base go back to inherit
            for name in list(logging.Logger.manager.loggerDict):
                if name.startswith(ROOT_NAME + ".") and name[len(ROOT_NAME) + 1:] not in names:
                    lg = logging.Logger.manager.loggerDict[name]
                    if isinstance(lg, logging.Logger) and lg.level != logging.NOTSET:
                        lg.setLevel(logging.NOTSET)

    def set_override(self, component: str, level: str, minutes: int | None) -> None:
        level = level.upper()
        if level not in LEVELS:
            raise ValueError("unknown level")
        expires = time.time() + minutes * 60 if minutes else None
        with self._lock:
            self.overrides[component] = (level, expires)
        self.apply()

    def clear_override(self, component: str) -> None:
        with self._lock:
            self.overrides.pop(component, None)
        self.apply()

    def expire(self) -> list[str]:
        """Remove expired overrides; returns names of expired components ('' for global)."""
        now = time.time()
        expired = []
        with self._lock:
            for name, (_, exp) in list(self.overrides.items()):
                if exp and exp <= now:
                    expired.append(name)
                    del self.overrides[name]
        if expired:
            self.apply()
        return expired


class CompanionLogger:
    """Structured facade over a stdlib logger: ``log.info("event.name", "message", key=value)``."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(f"{ROOT_NAME}.{name}" if name else ROOT_NAME)
        self.component = name

    def _log(self, level: int, event: str, msg: str, exc_info: Any = None, console_only: bool = False, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra: dict[str, Any] = {"event": event, "fields": fields}
        rid = request_id_var.get()
        if rid:
            extra["request_id"] = rid
        if console_only:
            extra[CONSOLE_ONLY_FLAG] = True
        self._logger.log(level, msg, extra=extra, exc_info=exc_info, stacklevel=3)

    def debug(self, event: str, msg: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, msg, **fields)

    def info(self, event: str, msg: str, **fields: Any) -> None:
        self._log(logging.INFO, event, msg, **fields)

    def warning(self, event: str, msg: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, msg, **fields)

    def error(self, event: str, msg: str, exc_info: Any = None, **fields: Any) -> None:
        self._log(logging.ERROR, event, msg, exc_info=exc_info, **fields)

    def critical(self, event: str, msg: str, exc_info: Any = None, **fields: Any) -> None:
        self._log(logging.CRITICAL, event, msg, exc_info=exc_info, **fields)

    def exception(self, event: str, msg: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, msg, exc_info=sys.exc_info(), **fields)

    def console_only(self, msg: str) -> None:
        """Write to the console sink only (never files, ring buffer, exports)."""
        self._log(logging.WARNING, "console.notice", msg, console_only=True)

    def is_enabled_for(self, level: str) -> bool:
        return self._logger.isEnabledFor(LEVELS[level])


class LoggingState:
    def __init__(self) -> None:
        self.levels: LevelManager | None = None
        self.ring: RingBufferHandler | None = None
        self.file_handler: BudgetedRotatingFileHandler | None = None
        self.listener: logging.handlers.QueueListener | None = None
        self.suppressor: RepeatSuppressor | None = None
        self.started_at = time.time()
        self.debug_mode_default_minutes = 30
        self.previous_global_level: str | None = None

    def health(self) -> dict:
        return {
            "levels": self.levels.effective() if self.levels else None,
            "file": self.file_handler.status() if self.file_handler else {"enabled": False},
            "ring_size": len(self.ring.buffer) if self.ring else 0,
            "suppressed_pending": self.suppressor.pending_summary() if self.suppressor else [],
            "degraded": bool(self.file_handler and self.file_handler.degraded),
        }


_state = LoggingState()


def get_logging_state() -> LoggingState:
    return _state


def get_logger(name: str) -> CompanionLogger:
    return CompanionLogger(name)


def setup_logging(
    level: str = "INFO",
    component_levels: dict[str, str] | None = None,
    fmt: str = "json",
    file_enabled: bool = False,
    log_dir: Path | None = None,
    file_max_bytes: int = 10 * 1024 * 1024,
    rotate_daily: bool = True,
    retention_days: int = 7,
    budget_bytes: int = 100 * 1024 * 1024,
    ring_size: int = 5000,
    debug_mode_default_minutes: int = 30,
    stream=None,
) -> LoggingState:
    """Configure the ``companion`` logger tree. Safe to call again (tests)."""
    root = logging.getLogger(ROOT_NAME)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    if _state.listener:
        try:
            _state.listener.stop()
        except Exception:
            pass
    if _state.file_handler:
        _state.file_handler.close()

    formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    console = logging.StreamHandler(stream or sys.stdout)
    console.setFormatter(formatter)

    ring = RingBufferHandler(ring_size)
    sinks: list[logging.Handler] = [console, ring]

    file_handler = None
    if file_enabled and log_dir is not None:
        file_handler = BudgetedRotatingFileHandler(
            log_dir, max_bytes=file_max_bytes, rotate_daily=rotate_daily,
            retention_days=retention_days, budget_bytes=budget_bytes,
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(ConsoleOnlyFilter())
        sinks.append(file_handler)

    # Emitting threads only enqueue; a single listener thread formats and writes.
    q: queue.Queue = queue.Queue(maxsize=20000)
    qh = logging.handlers.QueueHandler(q)
    redactor = RedactingFilter()
    suppressor = RepeatSuppressor()
    qh.addFilter(redactor)
    qh.addFilter(suppressor)
    root.addHandler(qh)
    listener = logging.handlers.QueueListener(q, *sinks, respect_handler_level=True)
    listener.start()

    # Quiet noisy third-party loggers but keep their warnings visible through our sinks.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "aiohttp", "asyncio", "aiosqlite"):
        lg = logging.getLogger(noisy)
        lg.handlers = []
        lg.propagate = False
        lg.setLevel(logging.WARNING)
        lg.addHandler(qh)

    _state.levels = LevelManager(level, component_levels or {})
    _state.ring = ring
    _state.file_handler = file_handler
    _state.listener = listener
    _state.suppressor = suppressor
    _state.debug_mode_default_minutes = debug_mode_default_minutes
    return _state


def shutdown_logging() -> None:
    if _state.listener:
        try:
            _state.listener.stop()
        except Exception:
            pass
        _state.listener = None
    if _state.file_handler:
        _state.file_handler.close()
