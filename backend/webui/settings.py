"""Deployment configuration, read once from the environment.

Every value has a safe default so the container starts with nothing but the two volumes.
Secrets are never stored on the Settings object in a way that gets logged: see ``describe()``.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _size(name: str, default: int) -> int:
    """Parse sizes like ``10MiB``, ``100M``, ``512k``, ``1048576``."""
    raw = os.getenv(name)
    if not raw:
        return default
    raw = raw.strip().lower()
    units = {"k": 1024, "kib": 1024, "kb": 1000, "m": 1024**2, "mib": 1024**2, "mb": 1000**2,
             "g": 1024**3, "gib": 1024**3, "gb": 1000**3}
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * mult)
            except ValueError:
                return default
    try:
        return int(raw)
    except ValueError:
        return default


def _networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return out


@dataclass
class Settings:
    # Paths
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "/data")))
    upstream_data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("UPSTREAM_DATA_DIR", "/upstream-data"))
    )
    upstream_config_file: Path | None = None  # derived unless overridden

    # Server
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8099, 1))
    public_origin: str | None = field(default_factory=lambda: (os.getenv("PUBLIC_ORIGIN") or "").strip().rstrip("/") or None)
    trusted_proxies: list = field(default_factory=lambda: _networks(os.getenv("TRUSTED_PROXIES")))
    require_https: bool = field(default_factory=lambda: _bool("REQUIRE_HTTPS", False))
    cookie_secure: str = field(default_factory=lambda: (os.getenv("COOKIE_SECURE", "auto") or "auto").lower())  # auto|true|false
    tls_cert_file: Path | None = field(default_factory=lambda: Path(os.environ["TLS_CERT_FILE"]) if os.getenv("TLS_CERT_FILE") else None)
    tls_key_file: Path | None = field(default_factory=lambda: Path(os.environ["TLS_KEY_FILE"]) if os.getenv("TLS_KEY_FILE") else None)
    tls_self_signed: bool = field(default_factory=lambda: _bool("TLS_SELF_SIGNED", False))
    max_request_bytes: int = field(default_factory=lambda: _size("MAX_REQUEST_BYTES", 256 * 1024))

    # Auth
    setup_token_env: str | None = field(default_factory=lambda: os.getenv("SETUP_TOKEN") or None)
    setup_token_ttl_minutes: int = field(default_factory=lambda: _int("SETUP_TOKEN_TTL_MINUTES", 60, 1))
    session_idle_hours: int = field(default_factory=lambda: _int("SESSION_IDLE_HOURS", 24, 1))
    session_absolute_days: int = field(default_factory=lambda: _int("SESSION_ABSOLUTE_DAYS", 7, 1))
    remember_days: int = field(default_factory=lambda: _int("REMEMBER_DAYS", 30, 1))
    reauth_minutes: int = field(default_factory=lambda: _int("REAUTH_MINUTES", 10, 1))
    argon2_time_cost: int = field(default_factory=lambda: _int("ARGON2_TIME_COST", 3, 1))
    argon2_memory_kib: int = field(default_factory=lambda: _int("ARGON2_MEMORY_KIB", 65536, 8192))
    argon2_parallelism: int = field(default_factory=lambda: _int("ARGON2_PARALLELISM", 4, 1))

    # Upstream integration
    log_source: str = field(default_factory=lambda: (os.getenv("LOG_SOURCE", "auto") or "auto").lower())  # auto|docker|file|none
    docker_host: str = field(default_factory=lambda: os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock"))
    upstream_container: str = field(default_factory=lambda: os.getenv("UPSTREAM_CONTAINER", "iSponsorBlockTV"))
    upstream_log_file: Path | None = field(default_factory=lambda: Path(os.environ["UPSTREAM_LOG_FILE"]) if os.getenv("UPSTREAM_LOG_FILE") else None)
    upstream_tz: str = field(default_factory=lambda: os.getenv("UPSTREAM_TZ", "UTC"))
    allow_restart: bool = field(default_factory=lambda: _bool("ALLOW_RESTART", True))
    availability_poll_minutes: int = field(default_factory=lambda: _int("AVAILABILITY_POLL_MINUTES", 10, 0))
    enable_discovery: bool = field(default_factory=lambda: _bool("ENABLE_DISCOVERY", False))
    enrichment_enabled: bool = field(default_factory=lambda: _bool("ENRICHMENT_ENABLED", True))
    youtube_api_key_env: str | None = field(default_factory=lambda: os.getenv("YOUTUBE_API_KEY") or None)
    config_backups_keep: int = field(default_factory=lambda: _int("CONFIG_BACKUPS_KEEP", 20, 1))

    # Data retention
    event_retention_days: int = field(default_factory=lambda: _int("EVENT_RETENTION_DAYS", 365, 7))
    raw_log_retention_days: int = field(default_factory=lambda: _int("UPSTREAM_LOG_RETENTION_DAYS", 14, 1))
    raw_log_max_rows: int = field(default_factory=lambda: _int("UPSTREAM_LOG_MAX_ROWS", 200_000, 1000))

    # Web UI logging
    log_level: str = field(default_factory=lambda: (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper())
    log_format: str = field(default_factory=lambda: (os.getenv("LOG_FORMAT", "json") or "json").lower())  # json|text
    log_file_enabled: bool = field(default_factory=lambda: _bool("LOG_FILE_ENABLED", False))
    log_file_max_bytes: int = field(default_factory=lambda: _size("LOG_FILE_MAX_BYTES", 10 * 1024 * 1024))
    log_file_rotate_daily: bool = field(default_factory=lambda: _bool("LOG_FILE_ROTATE_DAILY", True))
    log_retention_days: int = field(default_factory=lambda: _int("LOG_RETENTION_DAYS", 7, 1))
    log_dir_budget_bytes: int = field(default_factory=lambda: _size("LOG_DIR_BUDGET_BYTES", 100 * 1024 * 1024))
    log_ring_size: int = field(default_factory=lambda: _int("LOG_RING_SIZE", 5000, 500))
    debug_mode_default_minutes: int = field(default_factory=lambda: _int("DEBUG_MODE_MINUTES", 30, 1))

    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))

    def __post_init__(self) -> None:
        if os.getenv("UPSTREAM_CONFIG_FILE"):
            self.upstream_config_file = Path(os.environ["UPSTREAM_CONFIG_FILE"])
        else:
            self.upstream_config_file = self.upstream_data_dir / "config.json"
        if self.upstream_log_file is None and self.log_source == "file":
            self.upstream_log_file = self.upstream_data_dir / "isponsorblocktv.log"
        if self.cookie_secure not in {"auto", "true", "false"}:
            self.cookie_secure = "auto"
        if self.log_source not in {"auto", "docker", "file", "none"}:
            self.log_source = "auto"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "webui.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "config-backups"

    def component_levels(self) -> dict[str, str]:
        """``LOG_LEVEL_AUTH=DEBUG`` → ``{"auth": "DEBUG"}``."""
        out: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("LOG_LEVEL_") and value:
                out[key[len("LOG_LEVEL_"):].lower().replace("__", ".")] = value.upper()
        return out

    def describe(self) -> dict:
        """Non-secret view for diagnostics."""
        return {
            "data_dir": str(self.data_dir),
            "upstream_data_dir": str(self.upstream_data_dir),
            "upstream_config_file": str(self.upstream_config_file),
            "port": self.port,
            "public_origin": self.public_origin,
            "trusted_proxies": [str(n) for n in self.trusted_proxies],
            "require_https": self.require_https,
            "cookie_secure": self.cookie_secure,
            "tls": bool(self.tls_cert_file or self.tls_self_signed),
            "setup_token_source": "env" if self.setup_token_env else "generated",
            "log_source": self.log_source,
            "docker_host": self.docker_host,
            "upstream_container": self.upstream_container,
            "upstream_log_file": str(self.upstream_log_file) if self.upstream_log_file else None,
            "upstream_tz": self.upstream_tz,
            "allow_restart": self.allow_restart,
            "availability_poll_minutes": self.availability_poll_minutes,
            "enrichment_enabled": self.enrichment_enabled,
            "youtube_api_key_env": bool(self.youtube_api_key_env),
            "event_retention_days": self.event_retention_days,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "log_file_enabled": self.log_file_enabled,
            "log_file_max_bytes": self.log_file_max_bytes,
            "log_retention_days": self.log_retention_days,
            "log_dir_budget_bytes": self.log_dir_budget_bytes,
            "demo_mode": self.demo_mode,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
