"""Secure first-run bootstrap.

The administrator can only be created by presenting a setup secret:
* ``SETUP_TOKEN`` from the deployment (NAS container settings), or
* a random single-use token generated at startup and announced once on the console sink
  (visible in the NAS container-log viewer), expiring after ``SETUP_TOKEN_TTL_MINUTES``.

Once a user exists the bootstrap is permanently disabled (persisted ``setup_complete`` flag);
no token is generated or accepted again.
"""

from __future__ import annotations

import hmac
import secrets
import time

from ..db import Database
from ..logging import get_logger
from ..logging.redaction import register_secret
from ..util import now_iso, sha256_hex

log = get_logger("auth.bootstrap")

USERNAME_MIN, USERNAME_MAX = 2, 64


class BootstrapService:
    def __init__(self, db: Database, env_token: str | None, ttl_minutes: int) -> None:
        self.db = db
        self.env_token_hash = sha256_hex(env_token) if env_token else None
        self.ttl = ttl_minutes * 60
        self._generated_hash: str | None = None
        self._generated_expires: float = 0.0
        self.announcements = 0
        if env_token:
            register_secret(env_token, "SETUP_TOKEN")

    async def setup_complete(self) -> bool:
        if await self.db.get_state("setup_complete") == "1":
            return True
        return (await self.db.scalar("SELECT COUNT(*) FROM users")) > 0

    async def status(self) -> dict:
        complete = await self.setup_complete()
        if complete:
            return {"setup_complete": True, "token_source": None}
        if self.env_token_hash:
            return {"setup_complete": False, "token_source": "env", "token_expires_at": None}
        self._ensure_generated()
        return {
            "setup_complete": False,
            "token_source": "generated",
            "token_expires_in_seconds": max(0, int(self._generated_expires - time.time())),
        }

    def _ensure_generated(self) -> None:
        if self.env_token_hash:
            return
        if self._generated_hash and time.time() < self._generated_expires:
            return
        token = secrets.token_urlsafe(24)
        self._generated_hash = sha256_hex(token)
        self._generated_expires = time.time() + self.ttl
        register_secret(token, "SETUP_TOKEN")
        self.announcements += 1
        # Console-only: never reaches files, the ring buffer, or exports.
        log.console_only(
            "\n" + "=" * 72 + "\n"
            "  iSponsorBlockTV Companion — first-time setup\n"
            f"  Open the web UI and enter this one-time setup token (valid {self.ttl // 60} min):\n\n"
            f"      {token}\n\n"
            "  A new token is printed here when this one expires. Set SETUP_TOKEN in the\n"
            "  container environment instead if you prefer a token you choose.\n"
            + "=" * 72
        )
        log.info("bootstrap.token.generated", "Setup token generated and announced on the console",
                 expires_in_seconds=self.ttl, announcement=self.announcements)

    async def announce_if_needed(self) -> None:
        if not await self.setup_complete():
            self._ensure_generated()

    def verify_token(self, presented: str) -> bool:
        presented_hash = sha256_hex(presented.strip())
        if self.env_token_hash:
            return hmac.compare_digest(presented_hash, self.env_token_hash)
        if self._generated_hash and time.time() < self._generated_expires:
            return hmac.compare_digest(presented_hash, self._generated_hash)
        return False

    @staticmethod
    def validate_username(username: str) -> str | None:
        u = username.strip()
        if not (USERNAME_MIN <= len(u) <= USERNAME_MAX):
            return f"Username must be {USERNAME_MIN}-{USERNAME_MAX} characters."
        if any(c.isspace() for c in u) or any(ord(c) < 32 for c in u):
            return "Username cannot contain spaces or control characters."
        return None

    async def create_admin(self, username: str, password_hash: str, recovery_hash: str) -> int:
        """Atomically create the only administrator. Raises ``RuntimeError`` if one exists."""
        async with self.db.transaction() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM users")
            (count,) = await cur.fetchone()
            flag = await conn.execute("SELECT value FROM app_state WHERE key = 'setup_complete'")
            flag_row = await flag.fetchone()
            if count > 0 or (flag_row and flag_row[0] == "1"):
                raise RuntimeError("setup already completed")
            now = now_iso()
            cur = await conn.execute(
                "INSERT INTO users (username, password_hash, created_at, password_changed_at, is_admin) VALUES (?, ?, ?, ?, 1)",
                (username.strip(), password_hash, now, now),
            )
            user_id = cur.lastrowid
            await conn.execute("INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
                               (user_id, recovery_hash, now))
            await conn.execute("INSERT INTO app_state (key, value) VALUES ('setup_complete', '1') "
                               "ON CONFLICT(key) DO UPDATE SET value = '1'")
            await conn.execute("INSERT INTO app_state (key, value) VALUES ('setup_completed_at', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
        # Invalidate bootstrap credentials for the rest of the process lifetime.
        self._generated_hash = None
        self._generated_expires = 0.0
        self.env_token_hash = None
        log.info("bootstrap.completed", "Administrator account created; bootstrap disabled", user_id=user_id)
        return int(user_id)
