"""Administrative audit trail: who did what, when, to which fields — never the values."""

from __future__ import annotations

from ..db import Database
from ..logging import get_logger, request_id_var
from ..util import dumps, now_iso

log = get_logger("audit")


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(self, action: str, result: str, *, actor: dict | None = None, target: str | None = None,
                     fields: list[str] | dict | None = None, ip: str | None = None) -> None:
        try:
            await self.db.execute(
                "INSERT INTO audit_log (at, actor_user_id, actor_username, action, target, fields, result, request_id, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), actor.get("user_id") if actor else None, actor.get("username") if actor else None,
                 action, target, dumps(fields) if fields is not None else None, result, request_id_var.get(), ip),
            )
        except Exception:
            log.exception("audit.write_failed", "Could not write audit record", action=action)
        log.info("audit." + action, f"{action} → {result}", actor=actor.get("username") if actor else None,
                 target=target, fields=fields, result=result)

    async def recent(self, limit: int = 100) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 500),))
        return [dict(r) for r in rows]
