"""Secret redaction applied to every record before it is serialized.

Two layers:
* key-based: any structured field whose name looks sensitive is replaced wholesale;
* pattern-based: free text is scrubbed for well-known secret shapes (lounge tokens, cookies,
  bearer tokens, pairing codes, long hex/base64 credentials that match known screen IDs).

Known screen IDs are registered at runtime so the configured TV identifiers never appear in
logs even when a message interpolates an upstream line.
"""

from __future__ import annotations

import re
import threading
from typing import Any

SENSITIVE_KEY_PARTS = (
    "password", "passwd", "secret", "token", "cookie", "authorization", "session",
    "private_key", "recovery", "lounge", "apikey", "api_key", "pairing_code", "screen_id",
    "credential", "challenge", "set-cookie",
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(lounge id token )\S+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(Subscribing to lounge id )\S+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(loungeIdToken=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(loungeToken[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(Authorization:\s*)(Bearer|Basic)\s+\S+", re.I), r"\1\2 [REDACTED]"),
    (re.compile(r"((?:__Host-|__Secure-)?[A-Za-z0-9_\-]*session[A-Za-z0-9_\-]*=)[^;\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(pairing_code=)\d+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(screen_ids?=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(recovery code[:\s]+)[A-Z0-9\-]{10,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"(setup token[:\s]+)[A-Za-z0-9_\-]{16,}", re.I), r"\1[REDACTED]"),
]

_lock = threading.Lock()
_known_secrets: dict[str, str] = {}  # secret -> replacement label


def register_secret(value: str, label: str) -> None:
    """Make ``value`` disappear from all future log output."""
    if not value or len(value) < 8:
        return
    with _lock:
        _known_secrets[value] = label


def unregister_secret(value: str) -> None:
    with _lock:
        _known_secrets.pop(value, None)


def known_secret_labels() -> dict[str, str]:
    with _lock:
        return dict(_known_secrets)


NON_SENSITIVE_SUFFIXES = ("_source", "_mode", "_set", "_configured", "_env", "_enabled", "_prefix", "_count", "_length", "_hint", "_rejected", "_expires_at", "_ttl", "_minutes", "_known", "_id", "_ids")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith(NON_SENSITIVE_SUFFIXES) and "screen_id" not in lowered:
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = list(_known_secrets.items())
    for secret, label in secrets:
        if secret in text:
            text = text.replace(secret, f"[{label}]")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_value(key: str, value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, v, depth + 1) for v in value]
    return value


def redact_mapping(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: redact_value(k, v) for k, v in fields.items()}
