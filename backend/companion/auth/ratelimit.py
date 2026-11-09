"""Bounded login throttling with progressive delays; never a permanent lockout.

Three buckets are consulted per attempt: the source IP, the (lower-cased) username, and a global
bucket. Each failure within the window raises the required wait for that bucket (exponential,
capped). Successful logins clear the IP and username buckets. State is kept in memory and
mirrored to ``login_attempts`` so a restart does not reset an attacker's budget.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from ..db import Database


@dataclass
class Bucket:
    failures: int = 0
    blocked_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class Decision:
    allowed: bool
    retry_after: float = 0.0
    delay: float = 0.0  # artificial response delay applied even when allowed
    reason: str = ""


@dataclass
class RateLimiter:
    db: Database | None = None
    window: float = 15 * 60
    free_attempts: int = 5
    base_delay: float = 1.0
    max_block: float = 15 * 60
    global_free_attempts: int = 60
    _buckets: dict[str, Bucket] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _loaded: bool = False

    async def _load(self) -> None:
        if self._loaded or self.db is None:
            return
        self._loaded = True
        try:
            cutoff = time.time() - self.window
            rows = await self.db.fetchall(
                "SELECT ip, username, at FROM login_attempts WHERE success = 0 AND at > ? ORDER BY at",
                (cutoff,),
            )
            for row in rows:
                for key in (f"ip:{row['ip']}", f"user:{(row['username'] or '').lower()}", "global"):
                    b = self._buckets.setdefault(key, Bucket())
                    b.failures += 1
                    b.last_failure = max(b.last_failure, row["at"])
        except Exception:
            pass

    def _bucket(self, key: str) -> Bucket:
        b = self._buckets.setdefault(key, Bucket())
        if b.last_failure and time.time() - b.last_failure > self.window:
            b.failures = 0
            b.blocked_until = 0.0
        return b

    async def check(self, ip: str, username: str) -> Decision:
        await self._load()
        now = time.time()
        async with self._lock:
            worst = Decision(allowed=True)
            for key, free in ((f"ip:{ip}", self.free_attempts), (f"user:{username.lower()}", self.free_attempts), ("global", self.global_free_attempts)):
                b = self._bucket(key)
                if b.blocked_until > now:
                    wait = b.blocked_until - now
                    if wait > worst.retry_after:
                        worst = Decision(allowed=False, retry_after=math.ceil(wait), reason=key.split(":")[0])
                elif b.failures >= free // 2:
                    delay = min(8.0, self.base_delay * (2 ** max(0, b.failures - free // 2)))
                    worst.delay = max(worst.delay, delay)
            return worst

    async def record(self, ip: str, username: str, success: bool) -> None:
        await self._load()
        now = time.time()
        async with self._lock:
            keys = [f"ip:{ip}", f"user:{username.lower()}", "global"]
            if success:
                for key in keys[:2]:
                    self._buckets.pop(key, None)
            else:
                for key, free in zip(keys, (self.free_attempts, self.free_attempts, self.global_free_attempts)):
                    b = self._bucket(key)
                    b.failures += 1
                    b.last_failure = now
                    if b.failures >= free:
                        over = b.failures - free
                        block = min(self.max_block, 30.0 * (2 ** over))
                        b.blocked_until = now + block
            # bound memory
            if len(self._buckets) > 10000:
                for key in sorted(self._buckets, key=lambda k: self._buckets[k].last_failure)[:5000]:
                    self._buckets.pop(key, None)
        if self.db is not None:
            try:
                await self.db.execute(
                    "INSERT INTO login_attempts (at, ip, username, success) VALUES (?, ?, ?, ?)",
                    (now, ip, username.lower()[:128], 1 if success else 0),
                )
                if int(now) % 50 == 0:
                    await self.db.execute("DELETE FROM login_attempts WHERE at < ?", (now - 7 * 86400,))
            except Exception:
                pass

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "buckets": len(self._buckets),
            "blocked": sum(1 for b in self._buckets.values() if b.blocked_until > now),
        }
