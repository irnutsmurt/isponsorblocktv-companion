"""Version-aware adapter that turns iSponsorBlockTV log lines into typed records.

Formats handled (``iSponsorBlockTV`` 2.x, logging format
``%(asctime)s - %(name)s - %(levelname)s - %(message)s``):

    2026-11-08 20:14:01,123 - iSponsorBlockTV-<screen_id> - INFO - Playing video dQw4w9WgXcQ with 2 segments
    2026-11-08 20:14:31,004 - iSponsorBlockTV-<screen_id> - INFO - Skipping segment: seeking to 95.231
    2026-11-08 20:10:00,001 - iSponsorBlockTV-<screen_id> - INFO - Connected to device Living Room TV (Living room)
    2026-11-08 20:09:50,000 - iSponsorBlockTV-<screen_id> - INFO - Refreshed auth, lounge id token <token>
    ... - INFO - Ad has started, muting / Ad can be skipped, skipping / Ad has ended, unmuting
    ... - INFO - Subscribe completed, status 200 OK
    ... - INFO - Getting segments for next video: <id>
    ... - WARNING - Connection denied, attempting to circumvent the issue
    No config file found, using defaults. ...   (bare print() lines)
    Error getting segments for video <id>, hashed as abcd. Code: 503 - ...

Every parsed record carries ``kind`` and ``fields``; unknown lines are classified as ``other`` so
callers can still store/redact them. Screen ids and lounge tokens are replaced in ``redacted_line``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..util import screen_key

PARSER_VERSION = "2.x-r1"
SUPPORTED_UPSTREAM = ">=2.5,<3"

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?) - (?P<logger>[^ ]+(?: [^ ]+)*?) - "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) - (?P<msg>.*)$"
)
DEVICE_LOGGER_RE = re.compile(r"^iSponsorBlockTV-(?P<screen_id>\S+)$")
TOKEN_RE = re.compile(r"(lounge id token |Subscribing to lounge id )(\S+)", re.I)

MESSAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("playing", re.compile(r"^Playing video (?P<video_id>[A-Za-z0-9_-]{6,20}) with (?P<segments>\d+) segments?$")),
    ("skip", re.compile(r"^Skipping segment: seeking to (?P<seek_to>-?\d+(?:\.\d+)?)$")),
    ("connected", re.compile(r"^Connected to device (?P<screen_name>.*) \((?P<config_name>.*)\)$")),
    ("auth_refreshed", re.compile(r"^Refreshed auth, lounge id token ", re.I)),
    ("ad_mute", re.compile(r"^Ad has started, muting$")),
    ("ad_skip", re.compile(r"^Ad can be skipped, skipping$")),
    ("ad_unmute", re.compile(r"^Ad has ended, unmuting$")),
    ("subscribe_ended", re.compile(r"^Subscribe completed, status (?P<status>\d+)")),
    ("subscribing", re.compile(r"^Subscribing to lounge id ", re.I)),
    ("prefetch", re.compile(r"^Getting segments for next video: (?P<video_id>\S+)$")),
    ("connection_denied", re.compile(r"^Connection denied, attempting to circumvent")),
    ("connected_as_screen", re.compile(r"^Connected as screen: please force close")),
    ("unknown_reply", re.compile(r"^Unknown reply to connect (?P<status>\d+)")),
    ("auth_failed", re.compile(r"^Refresh auth failed")),
    ("subscribe_failed", re.compile(r"^Handle subscribe (?:failed|payload error)")),
    ("connect_failed", re.compile(r"^Handle connect failed")),
    ("command_failed", re.compile(r"^Command failed")),
    ("legacy_screen_id", re.compile(r"^The screen id .* is 26 characters long")),
    ("watchdog", re.compile(r"^Watchdog triggered")),
    ("debug_event", re.compile(r"^process_event\(")),
]
BARE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("startup_no_config", re.compile(r"^No config file found")),
    ("startup_no_devices", re.compile(r"^No devices found, please add at least one device")),
    ("startup_default_categories", re.compile(r"^No categories found, using default")),
    ("startup_exiting", re.compile(r"^Exiting in \d+ seconds")),
    ("shutdown", re.compile(r"^Cancelling tasks and exiting")),
    ("exited", re.compile(r"^Exited$")),
    ("shutdown", re.compile(r"^Aborted\.$")),              # rich_click after SIGTERM
    ("sb_error", re.compile(r"^Error getting segments for video (?P<video_id>\S+), hashed as \w+\. Code: (?P<status>\d+)")),
    ("legacy_atvs", re.compile(r"^The atvs config option is deprecated")),
    ("traceback", re.compile(r"^Traceback \(most recent call last\)")),
]


@dataclass
class ParsedLine:
    kind: str                       # see MESSAGE_PATTERNS/BARE_PATTERNS, plus 'other', 'continuation'
    level: str | None
    logger: str | None
    screen_key: str | None          # hashed device identity if a device logger
    screen_id: str | None           # raw (never persisted; used only to map to config devices)
    message: str                    # redacted message
    redacted_line: str
    ts: datetime | None             # from the line itself (container local time → UTC via tz)
    fields: dict = field(default_factory=dict)
    structured: bool = True


class UpstreamLogParser:
    def __init__(self, upstream_tz: str = "UTC") -> None:
        try:
            self.tz = ZoneInfo(upstream_tz)
        except Exception:
            self.tz = timezone.utc
        self.known_screen_ids: dict[str, str] = {}   # screen_id -> screen_key
        self.stats = {"lines": 0, "structured": 0, "unstructured": 0, "unknown": 0}

    def register_screen_id(self, screen_id: str) -> None:
        if screen_id:
            self.known_screen_ids[screen_id] = screen_key(screen_id)

    def _parse_ts(self, text: str) -> datetime | None:
        text = text.replace(",", ".")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=self.tz).astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    def redact(self, text: str) -> str:
        text = TOKEN_RE.sub(r"\1[REDACTED]", text)
        for sid, key in self.known_screen_ids.items():
            if sid in text:
                text = text.replace(sid, f"[screen:{key}]")
        return text

    def parse(self, line: str) -> ParsedLine:
        self.stats["lines"] += 1
        line = line.rstrip("\r\n")
        m = LINE_RE.match(line)
        if m:
            self.stats["structured"] += 1
            logger_name = m.group("logger")
            level = m.group("level")
            msg = m.group("msg")
            ts = self._parse_ts(m.group("ts"))
            sid = None
            skey = None
            dm = DEVICE_LOGGER_RE.match(logger_name)
            if dm:
                sid = dm.group("screen_id")
                skey = screen_key(sid)
                if sid not in self.known_screen_ids:
                    self.known_screen_ids[sid] = skey
            kind, fields = self._classify(msg, MESSAGE_PATTERNS)
            if kind == "other" and level in ("ERROR", "CRITICAL"):
                kind = "error"
            elif kind == "other" and level == "WARNING":
                kind = "warning"
            if kind == "other":
                self.stats["unknown"] += 1
            red_msg = self.redact(msg)
            red_logger = f"iSponsorBlockTV-[screen:{skey}]" if skey else logger_name
            return ParsedLine(kind, level, red_logger, skey, sid, red_msg,
                              f"{m.group('ts')} - {red_logger} - {level} - {red_msg}", ts, fields, True)
        self.stats["unstructured"] += 1
        kind, fields = self._classify(line, BARE_PATTERNS)
        if kind == "other":
            if line.startswith((" ", "\t")) or line.startswith("  File "):
                kind = "continuation"
            else:
                self.stats["unknown"] += 1
        red = self.redact(line)
        return ParsedLine(kind, None, None, None, None, red, red, None, fields, False)

    @staticmethod
    def _classify(msg: str, patterns: list[tuple[str, re.Pattern[str]]]) -> tuple[str, dict]:
        for kind, pat in patterns:
            pm = pat.match(msg)
            if pm:
                fields = {k: v for k, v in pm.groupdict().items() if v is not None}
                if "segments" in fields:
                    fields["segments"] = int(fields["segments"])
                if "seek_to" in fields:
                    fields["seek_to"] = float(fields["seek_to"])
                if "status" in fields:
                    fields["status"] = int(fields["status"])
                return kind, fields
        return "other", {}
