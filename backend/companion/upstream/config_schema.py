"""iSponsorBlockTV ``config.json`` schema, mirrored from upstream ``helpers.Config`` (2.11.0).

Unknown keys (top-level and per-device) are preserved verbatim; validation only enforces what
upstream itself would reject or misbehave on.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

SKIP_CATEGORIES: list[tuple[str, str]] = [
    ("Sponsor", "sponsor"),
    ("Self Promotion", "selfpromo"),
    ("Intro", "intro"),
    ("Outro", "outro"),
    ("Music: Off-topic", "music_offtopic"),
    ("Interaction reminder", "interaction"),
    ("Exclusive access", "exclusive_access"),
    ("Preview / recap", "preview"),
    ("Filler", "filler"),
    ("Hook / greetings", "hook"),
]
KNOWN_CATEGORIES = {v for _, v in SKIP_CATEGORIES}
DEFAULT_SB_API = "https://sponsor.ajay.app/api/"

# Upstream defaults from helpers.Config.__init__ (absent keys behave like these at runtime).
DEFAULTS: dict[str, Any] = {
    "devices": [],
    "apikey": "",
    "skip_categories": ["sponsor"],   # upstream: None → ["sponsor"] with a notice
    "channel_whitelist": [],
    "skip_count_tracking": True,
    "mute_ads": False,
    "skip_ads": False,
    "minimum_skip_length": 1,
    "auto_play": True,
    "join_name": "iSponsorBlockTV",
    "use_proxy": False,
    "sponsorblock_api_url": DEFAULT_SB_API,
}

# Field metadata for the UI: scope explains that every change needs an upstream restart.
FIELDS: list[dict[str, Any]] = [
    {"key": "skip_categories", "type": "categories", "label": "Segment categories to skip",
     "help": "SponsorBlock categories iSponsorBlockTV will skip. An empty list disables segment skipping."},
    {"key": "minimum_skip_length", "type": "int", "min": 0, "label": "Minimum segment length (seconds)",
     "help": "Segments shorter than this are ignored. 0 skips everything."},
    {"key": "skip_count_tracking", "type": "bool", "label": "Report skips to SponsorBlock",
     "help": "Sends only the segment UUID so contributors can see their submission helped."},
    {"key": "skip_ads", "type": "bool", "label": "Skip YouTube ads",
     "help": "Presses “Skip Ad” as soon as YouTube allows it."},
    {"key": "mute_ads", "type": "bool", "label": "Mute YouTube ads",
     "help": "Mutes the TV while an ad plays and unmutes afterwards."},
    {"key": "auto_play", "type": "bool", "label": "Keep autoplay enabled",
     "help": "iSponsorBlockTV re-enables YouTube autoplay when the TV changes it."},
    {"key": "join_name", "type": "string", "label": "Name shown on the TV",
     "help": "How iSponsorBlockTV appears in the YouTube app's connected devices list."},
    {"key": "apikey", "type": "secret", "label": "YouTube Data API key",
     "help": "Only needed for the channel whitelist. Stored in config.json by upstream."},
    {"key": "channel_whitelist", "type": "channels", "label": "Channel whitelist",
     "help": "Videos from these channels are never skipped. Requires a YouTube API key, otherwise iSponsorBlockTV refuses to start."},
    {"key": "use_proxy", "type": "bool", "label": "Use system proxy",
     "help": "Honour HTTP(S)_PROXY environment variables inside the iSponsorBlockTV container."},
    {"key": "sponsorblock_api_url", "type": "url", "label": "SponsorBlock API URL",
     "help": "Change only if you run your own SponsorBlock mirror."},
]

DEVICE_FIELDS = [
    {"key": "name", "type": "string", "label": "Device name"},
    {"key": "offset", "type": "int", "label": "Skip offset (milliseconds)",
     "help": "Positive values make skips happen slightly earlier to compensate for TV latency."},
]


@dataclass
class ValidationResult:
    errors: list[dict] = field(default_factory=list)      # {path, message}
    warnings: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})

    def warn(self, path: str, message: str) -> None:
        self.warnings.append({"path": path, "message": message})


def validate_config(cfg: Any) -> ValidationResult:
    """Validate a complete config document as upstream would load it."""
    r = ValidationResult()
    if not isinstance(cfg, dict):
        r.error("", "Configuration must be a JSON object.")
        return r
    if "atvs" in cfg:
        r.error("atvs", "Legacy 'atvs' key present: iSponsorBlockTV 2.x exits on startup until it is removed.")
    devices = cfg.get("devices", [])
    if not isinstance(devices, list):
        r.error("devices", "'devices' must be a list.")
        devices = []
    if len(devices) == 0:
        r.error("devices", "At least one device is required; iSponsorBlockTV exits when the list is empty.")
    seen: dict[str, int] = {}
    for i, dev in enumerate(devices):
        p = f"devices[{i}]"
        if not isinstance(dev, dict):
            r.error(p, "Each device must be an object.")
            continue
        sid = dev.get("screen_id")
        if not isinstance(sid, str) or not sid.strip():
            r.error(f"{p}.screen_id", "Device has no screen ID; iSponsorBlockTV refuses to start. Pair it again or remove it.")
        else:
            if sid in seen:
                r.error(f"{p}.screen_id", f"Duplicate of device #{seen[sid] + 1}; the same TV is listed twice.")
            seen.setdefault(sid, i)
            if len(sid) == 26:
                r.warn(f"{p}.screen_id", "Legacy 26-character screen ID. YouTube is revoking these; re-pair this TV.")
        name = dev.get("name")
        if name is not None and not isinstance(name, str):
            r.error(f"{p}.name", "Name must be text.")
        offset = dev.get("offset", 0)
        if not isinstance(offset, (int, float)) or isinstance(offset, bool):
            r.error(f"{p}.offset", "Offset must be a number of milliseconds.")
        elif not (-10000 <= offset <= 10000):
            r.warn(f"{p}.offset", "Offset outside ±10000 ms is unusual.")
    cats = cfg.get("skip_categories")
    if cats is not None:
        if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
            r.error("skip_categories", "'skip_categories' must be a list of category names.")
        else:
            for c in cats:
                if c not in KNOWN_CATEGORIES:
                    r.warn("skip_categories", f"Unknown category '{c}' will be sent to SponsorBlock as-is.")
            if len(cats) == 0:
                r.warn("skip_categories", "No categories selected: segment skipping is disabled (ads can still be skipped/muted).")
    msl = cfg.get("minimum_skip_length", 1)
    if isinstance(msl, bool) or not isinstance(msl, (int, float)):
        r.error("minimum_skip_length", "Minimum skip length must be a non-negative number of seconds.")
    elif msl < 0:
        r.error("minimum_skip_length", "Minimum skip length cannot be negative.")
    for key in ("skip_count_tracking", "mute_ads", "skip_ads", "auto_play", "use_proxy"):
        if key in cfg and not isinstance(cfg[key], bool):
            r.error(key, f"'{key}' must be true or false.")
    apikey = cfg.get("apikey", "")
    if apikey is not None and not isinstance(apikey, str):
        r.error("apikey", "API key must be text.")
    wl = cfg.get("channel_whitelist", [])
    if not isinstance(wl, list):
        r.error("channel_whitelist", "'channel_whitelist' must be a list.")
    else:
        for i, ch in enumerate(wl):
            if not isinstance(ch, dict) or not isinstance(ch.get("id"), str) or not ch.get("id", "").strip():
                r.error(f"channel_whitelist[{i}]", "Each whitelisted channel needs a channel ID.")
        if wl and not (isinstance(apikey, str) and apikey.strip()):
            r.error("channel_whitelist", "A YouTube API key is required when the channel whitelist is not empty; iSponsorBlockTV raises an error at startup otherwise.")
    jn = cfg.get("join_name", "iSponsorBlockTV")
    if not isinstance(jn, str) or not jn.strip():
        r.error("join_name", "The name shown on the TV cannot be empty.")
    url = cfg.get("sponsorblock_api_url", DEFAULT_SB_API)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        r.error("sponsorblock_api_url", "SponsorBlock API URL must start with http:// or https://.")
    return r


def normalize_for_write(cfg: dict) -> dict:
    """Light normalization: ensure the SponsorBlock URL ends with '/', ints are ints."""
    out = copy.deepcopy(cfg)
    url = out.get("sponsorblock_api_url")
    if isinstance(url, str) and url and not url.endswith("/"):
        out["sponsorblock_api_url"] = url + "/"
    if isinstance(out.get("minimum_skip_length"), float) and out["minimum_skip_length"].is_integer():
        out["minimum_skip_length"] = int(out["minimum_skip_length"])
    for dev in out.get("devices", []) if isinstance(out.get("devices"), list) else []:
        if isinstance(dev, dict):
            dev.setdefault("offset", 0)
            if isinstance(dev.get("offset"), float) and dev["offset"].is_integer():
                dev["offset"] = int(dev["offset"])
    return out


def effective(cfg: dict) -> dict:
    """Config with upstream defaults applied for display."""
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    if cfg.get("skip_categories") is None:
        out["skip_categories"] = list(DEFAULTS["skip_categories"])
    return out


def unknown_keys(cfg: dict) -> list[str]:
    return sorted(k for k in cfg if k not in DEFAULTS and k != "atvs")


def changed_fields(before: dict, after: dict) -> list[str]:
    keys = set(before) | set(after)
    changed = []
    for k in sorted(keys):
        if k == "devices":
            b = before.get("devices") or []
            a = after.get("devices") or []
            if b != a:
                changed.append("devices")
        elif before.get(k) != after.get(k):
            changed.append(k)
    return changed


def redact_config(cfg: dict) -> dict:
    """Diagnostics view: screen IDs hashed, API key hidden."""
    from ..util import screen_key
    out = copy.deepcopy(cfg)
    if isinstance(out.get("apikey"), str) and out["apikey"]:
        out["apikey"] = "[REDACTED]"
    for dev in out.get("devices", []) if isinstance(out.get("devices"), list) else []:
        if isinstance(dev, dict) and isinstance(dev.get("screen_id"), str):
            dev["screen_id"] = f"[screen:{screen_key(dev['screen_id'])}]"
    return out
