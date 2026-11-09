"""Argon2id password hashing (RFC 9106 parameters) via argon2-cffi."""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # Argon2 has no practical input limit; this bounds request size only.


class PasswordService:
    def __init__(self, time_cost: int = 3, memory_kib: int = 65536, parallelism: int = 4) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism,
            hash_len=32, salt_len=16, type=Type.ID,
        )
        # Used to burn equal CPU time when the username does not exist (timing uniformity).
        self._dummy_hash = self._hasher.hash("dummy-password-for-timing-equalization")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    async def hash_async(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    def verify(self, hashed: str | None, password: str) -> bool:
        target = hashed or self._dummy_hash
        try:
            ok = self._hasher.verify(target, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return bool(ok) and hashed is not None

    async def verify_async(self, hashed: str | None, password: str) -> bool:
        return await asyncio.to_thread(self.verify, hashed, password)

    def needs_rehash(self, hashed: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(hashed)
        except Exception:
            return False

    @staticmethod
    def validate_new_password(password: str) -> str | None:
        """Return an error message or None."""
        if len(password) < MIN_PASSWORD_LENGTH:
            return f"Use at least {MIN_PASSWORD_LENGTH} characters. Long passphrases are welcome."
        if len(password) > MAX_PASSWORD_LENGTH:
            return f"Passwords longer than {MAX_PASSWORD_LENGTH} characters are not accepted."
        if password.strip() != password:
            return "Passwords cannot start or end with whitespace."
        return None
