"""SQLite access: one connection, WAL mode, serialized writes, explicit migrations."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from ..logging import get_logger
from .migrations import MIGRATIONS

log = get_logger("db")


class DatabaseUnavailable(Exception):
    """Raised when the database cannot be used; callers must fail closed."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._tx_owner: asyncio.Task | None = None
        self.healthy = False
        self.last_error: str | None = None
        self.contention_count = 0

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        conn = await aiosqlite.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("PRAGMA temp_store=MEMORY")
        self._conn = conn
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        await self._migrate()
        self.healthy = True

    async def close(self) -> None:
        if self._conn:
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self._conn.close()
            self._conn = None
        self.healthy = False

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None or not self.healthy:
            raise DatabaseUnavailable(self.last_error or "database not open")
        return self._conn

    async def _migrate(self) -> None:
        conn = self._conn
        assert conn is not None
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        cur = await conn.execute("SELECT version FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row else 0
        if not row:
            await conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            log.info("db.migration.start", f"Applying schema migration {version}", version=version)
            try:
                # executescript issues its own COMMIT first, so the transaction lives inside the script.
                await conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nUPDATE schema_version SET version = {int(version)};\nCOMMIT;")
            except Exception as exc:
                try:
                    await conn.execute("ROLLBACK")
                except Exception:
                    pass
                self.last_error = f"migration {version} failed: {exc}"
                log.critical("db.migration.failed", "Schema migration failed; refusing to start",
                             version=version, error=str(exc), exc_info=True)
                raise DatabaseUnavailable(self.last_error) from exc
            log.info("db.migration.done", f"Schema migration {version} applied", version=version)

    # --- helpers ---------------------------------------------------------
    def _in_own_tx(self) -> bool:
        return self._tx_owner is not None and self._tx_owner is asyncio.current_task()

    @asynccontextmanager
    async def _locked(self):
        """Serialize writes; a task already inside ``transaction()`` re-enters without deadlocking."""
        if self._in_own_tx():
            yield
        else:
            async with self._write_lock:
                yield

    def _wrap(self, exc: Exception) -> Exception:
        if isinstance(exc, sqlite3.OperationalError):
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                self.contention_count += 1
                log.warning("db.contention", "Database busy", error=str(exc))
                return exc
            if "disk" in text or "readonly" in text or "unable to open" in text or "i/o" in text:
                self.healthy = False
                self.last_error = str(exc)
                log.critical("db.unavailable", "Database became unavailable", error=str(exc))
                return DatabaseUnavailable(str(exc))
        return exc

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        try:
            async with self._locked():
                cur = await self.conn.execute(sql, tuple(params))
                return cur.rowcount
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def execute_returning_id(self, sql: str, params: Iterable[Any] = ()) -> int:
        try:
            async with self._locked():
                cur = await self.conn.execute(sql, tuple(params))
                return cur.lastrowid or 0
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        try:
            async with self._locked():
                await self.conn.executemany(sql, [tuple(r) for r in rows])
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            await cur.close()
            return row
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        try:
            cur = await self.conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row else None

    @asynccontextmanager
    async def transaction(self):
        """Serialized write transaction. Use ``conn`` inside; commits on success."""
        if self._in_own_tx():
            raise RuntimeError("nested transaction")
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                raise self._wrap(exc) from exc
            self._tx_owner = asyncio.current_task()
            try:
                yield self.conn
                await self.conn.execute("COMMIT")
            except BaseException as exc:
                try:
                    await self.conn.execute("ROLLBACK")
                except Exception:
                    pass
                if isinstance(exc, Exception):
                    raise self._wrap(exc) from exc
                raise
            finally:
                self._tx_owner = None

    async def get_state(self, key: str) -> str | None:
        return await self.scalar("SELECT value FROM app_state WHERE key = ?", (key,))

    async def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            await self.execute("DELETE FROM app_state WHERE key = ?", (key,))
        else:
            await self.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def ping(self) -> bool:
        try:
            await self.fetchone("SELECT 1")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.healthy = False
            return False

    async def stats(self) -> dict:
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
            wal = self.path.with_name(self.path.name + "-wal")
            wal_size = wal.stat().st_size if wal.exists() else 0
            return {"path": str(self.path), "size_bytes": size, "wal_bytes": wal_size,
                    "healthy": self.healthy, "contention_count": self.contention_count,
                    "last_error": self.last_error}
        except OSError as exc:
            return {"path": str(self.path), "healthy": self.healthy, "error": str(exc)}
