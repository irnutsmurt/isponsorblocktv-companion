"""Safe reading and atomic writing of upstream ``config.json`` with backups and conflict detection."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..db import Database
from ..logging import get_logger
from ..util import now_iso, utcnow
from .config_schema import normalize_for_write, validate_config

log = get_logger("upstream.config")


class ConfigError(Exception):
    def __init__(self, message: str, code: str = "config_error", details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class ConfigConflict(ConfigError):
    def __init__(self, current_etag: str) -> None:
        super().__init__("The configuration changed on disk since you loaded it.", "conflict", {"current_etag": current_etag})


@dataclass
class ConfigSnapshot:
    exists: bool
    data: dict | None
    raw: str | None
    etag: str                 # sha256 of raw bytes, or "missing"
    mtime: float | None
    size: int
    parse_error: str | None = None
    path: str = ""
    writable: bool = False
    write_problem: str | None = None
    owner: str | None = None
    mode: str | None = None


class ConfigStore:
    def __init__(self, path: Path, backup_dir: Path, db: Database, keep_backups: int = 20) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.db = db
        self.keep = max(1, keep_backups)
        self.last_written_etag: str | None = None  # lets the watcher tell our own writes from external edits

    # ---- reading ----------------------------------------------------------
    def read(self) -> ConfigSnapshot:
        p = self.path
        writable, problem = self.writability()
        owner = mode = None
        try:
            st = p.stat()
            owner = f"{st.st_uid}:{st.st_gid}"
            mode = oct(stat.S_IMODE(st.st_mode))
        except FileNotFoundError:
            return ConfigSnapshot(False, None, None, "missing", None, 0, path=str(p), writable=writable, write_problem=problem)
        except OSError as exc:
            return ConfigSnapshot(False, None, None, "missing", None, 0, parse_error=f"Cannot stat config: {exc}", path=str(p), writable=writable, write_problem=problem)
        try:
            raw_bytes = p.read_bytes()
        except OSError as exc:
            return ConfigSnapshot(True, None, None, "unreadable", st.st_mtime, st.st_size, parse_error=f"Cannot read config: {exc}", path=str(p), writable=writable, write_problem=problem, owner=owner, mode=mode)
        etag = hashlib.sha256(raw_bytes).hexdigest()
        raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON value is not an object")
            parse_error = None
        except ValueError as exc:
            data = None
            parse_error = f"config.json is not valid JSON: {exc}"
        return ConfigSnapshot(True, data, raw, etag, st.st_mtime, st.st_size, parse_error=parse_error, path=str(p), writable=writable, write_problem=problem, owner=owner, mode=mode)

    def writability(self) -> tuple[bool, str | None]:
        d = self.path.parent
        if not d.exists():
            return False, f"Directory {d} does not exist inside the companion container. Check the UPSTREAM_DATA_DIR volume mount."
        if not os.access(d, os.W_OK | os.X_OK):
            return False, (f"The companion cannot write to {d} (running as uid {os.getuid()}, gid {os.getgid()}). "
                           "Grant write permission to that user on the iSponsorBlockTV data folder, or run the companion with PUID/PGID matching the folder owner.")
        if self.path.exists() and not os.access(self.path, os.R_OK):
            return False, f"{self.path} exists but is not readable by uid {os.getuid()}."
        return True, None

    # ---- writing ----------------------------------------------------------
    async def write(self, new_config: dict, *, expected_etag: str | None, reason: str, actor: str | None) -> ConfigSnapshot:
        """Validate, back up, atomically replace, verify. Raises ConfigError/ConfigConflict."""
        current = self.read()
        if expected_etag is not None and current.etag != expected_etag:
            log.warning("config.write.conflict", "Config changed on disk since client loaded it",
                        expected=expected_etag[:12], current=current.etag[:12])
            raise ConfigConflict(current.etag)
        if current.exists and current.parse_error and reason != "restore":
            # Never clobber a malformed file blindly: the UI must ask to restore from backup or edit raw.
            raise ConfigError("The existing config.json is malformed; restore a backup or fix it before saving.", "malformed_existing",
                              {"parse_error": current.parse_error})
        new_config = normalize_for_write(new_config)
        result = validate_config(new_config)
        if not result.ok:
            raise ConfigError("Configuration is invalid.", "invalid", {"errors": result.errors, "warnings": result.warnings})
        writable, problem = self.writability()
        if not writable:
            raise ConfigError(problem or "Config directory is not writable.", "permission")
        payload = json.dumps(new_config, indent=4, ensure_ascii=False) + "\n"
        if current.exists and current.raw is not None:
            await self._backup(current, reason, actor)
        try:
            self._atomic_replace(payload, current)
        except OSError as exc:
            code = "disk_full" if exc.errno == errno.ENOSPC else "permission" if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS) else "io_error"
            log.error("config.write.failed", "Writing config.json failed", error=str(exc), code=code)
            raise ConfigError(f"Could not write config.json: {exc.strerror or exc}", code) from exc
        after = self.read()
        if after.parse_error or after.data != new_config:
            log.error("config.write.verify_failed", "Read-back of config.json does not match what was written")
            raise ConfigError("Read-back verification failed after writing config.json.", "verify_failed")
        self.last_written_etag = after.etag
        log.info("config.written", "config.json updated", reason=reason, actor=actor, bytes=len(payload), etag=after.etag[:12])
        return after

    def _atomic_replace(self, payload: str, current: ConfigSnapshot) -> None:
        d = self.path.parent
        fd, tmp = tempfile.mkstemp(prefix=".config.json.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # Preserve the existing mode/owner so the upstream container keeps reading it.
            if current.exists:
                try:
                    st = self.path.stat()
                    os.chmod(tmp, stat.S_IMODE(st.st_mode))
                    try:
                        os.chown(tmp, st.st_uid, st.st_gid)
                    except (PermissionError, OSError):
                        pass
                except OSError:
                    pass
            else:
                os.chmod(tmp, 0o644)  # upstream runs as root in its container; world-readable is fine for a fresh file
            os.replace(tmp, self.path)
            dir_fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- backups ----------------------------------------------------------
    async def _backup(self, snapshot: ConfigSnapshot, reason: str, actor: str | None) -> dict | None:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.backup_dir, 0o700)
        except OSError as exc:
            log.warning("config.backup.dir_failed", "Cannot create backup directory", error=str(exc))
            return None
        last = await self.db.fetchone("SELECT sha256 FROM config_backups ORDER BY id DESC LIMIT 1")
        if last and last["sha256"] == snapshot.etag:
            return None  # identical content already backed up
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        name = f"config-{stamp}.json"
        target = self.backup_dir / name
        try:
            target.write_text(snapshot.raw or "", encoding="utf-8")
            os.chmod(target, 0o600)
        except OSError as exc:
            log.warning("config.backup.failed", "Config backup failed; continuing with write", error=str(exc))
            return None
        bid = await self.db.execute_returning_id(
            "INSERT INTO config_backups (created_at, filename, sha256, size, reason, actor) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), name, snapshot.etag, snapshot.size, reason, actor),
        )
        await self._prune_backups()
        log.info("config.backup.created", "Config backup created", filename=name, reason=reason)
        return {"id": bid, "filename": name}

    async def _prune_backups(self) -> None:
        rows = await self.db.fetchall("SELECT id, filename FROM config_backups ORDER BY id DESC")
        for row in rows[self.keep:]:
            try:
                (self.backup_dir / row["filename"]).unlink(missing_ok=True)
            except OSError:
                pass
            await self.db.execute("DELETE FROM config_backups WHERE id = ?", (row["id"],))

    async def list_backups(self) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM config_backups ORDER BY id DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["available"] = (self.backup_dir / d["filename"]).exists()
            out.append(d)
        return out

    async def read_backup(self, backup_id: int) -> tuple[dict, dict]:
        row = await self.db.fetchone("SELECT * FROM config_backups WHERE id = ?", (backup_id,))
        if row is None:
            raise ConfigError("Backup not found.", "not_found")
        p = self.backup_dir / row["filename"]
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Backup cannot be read: {exc}", "backup_unreadable")
        return dict(row), data

    async def restore(self, backup_id: int, actor: str | None) -> ConfigSnapshot:
        meta, data = await self.read_backup(backup_id)
        snap = await self.write(data, expected_etag=None, reason="restore", actor=actor)
        log.info("config.restored", "Config restored from backup", backup_id=backup_id, filename=meta["filename"])
        return snap

    def directory_report(self) -> dict:
        d = self.path.parent
        try:
            st = d.stat()
            usage = shutil.disk_usage(d)
            return {"path": str(d), "exists": True, "owner": f"{st.st_uid}:{st.st_gid}", "mode": oct(stat.S_IMODE(st.st_mode)),
                    "free_bytes": usage.free, "process_uid": os.getuid(), "process_gid": os.getgid()}
        except OSError as exc:
            return {"path": str(d), "exists": False, "error": str(exc), "process_uid": os.getuid(), "process_gid": os.getgid()}
