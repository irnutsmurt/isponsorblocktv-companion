"""Single-use recovery codes. Only an Argon2id verifier is stored."""

from __future__ import annotations

import secrets

from ..db import Database
from ..logging import get_logger
from ..util import now_iso
from .passwords import PasswordService

log = get_logger("auth.recovery")

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_recovery_code() -> str:
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4)]
    return "-".join(groups)


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class RecoveryService:
    def __init__(self, db: Database, passwords: PasswordService) -> None:
        self.db = db
        self.passwords = passwords

    async def issue(self, user_id: int, conn=None) -> str:
        code = generate_recovery_code()
        code_hash = await self.passwords.hash_async(normalize_code(code))
        if conn is None:
            async with self.db.transaction() as c:
                await c.execute("UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_iso(), user_id))
                await c.execute("INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)", (user_id, code_hash, now_iso()))
        else:
            await conn.execute("UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_iso(), user_id))
            await conn.execute("INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)", (user_id, code_hash, now_iso()))
        log.info("recovery.issued", "Recovery code issued", user_id=user_id)
        return code

    async def hash_for_new_user(self) -> tuple[str, str]:
        code = generate_recovery_code()
        return code, await self.passwords.hash_async(normalize_code(code))

    async def verify_and_consume(self, user_id: int, code: str) -> bool:
        row = await self.db.fetchone(
            "SELECT id, code_hash FROM recovery_codes WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        ok = await self.passwords.verify_async(row["code_hash"] if row else None, normalize_code(code))
        if not ok or row is None:
            return False
        n = await self.db.execute("UPDATE recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL", (now_iso(), row["id"]))
        return bool(n)

    async def has_active(self, user_id: int) -> bool:
        return bool(await self.db.scalar("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)))
