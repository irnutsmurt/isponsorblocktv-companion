"""Server-side sessions with opaque random tokens stored hashed."""

from __future__ import annotations

from datetime import timedelta

from ..db import Database
from ..logging import get_logger
from ..util import iso, now_iso, parse_iso, random_token, row_to_dict, sha256_hex, utcnow

log = get_logger("auth.session")

COOKIE_NAME = "isbtv_session"
COOKIE_NAME_SECURE = "__Host-isbtv_session"


def summarize_user_agent(ua: str | None) -> str:
    if not ua:
        return "Unknown browser"
    ua_l = ua.lower()
    browser = "Browser"
    if "edg/" in ua_l:
        browser = "Edge"
    elif "opr/" in ua_l or "opera" in ua_l:
        browser = "Opera"
    elif "firefox/" in ua_l:
        browser = "Firefox"
    elif "chrome/" in ua_l or "crios/" in ua_l:
        browser = "Chrome"
    elif "safari/" in ua_l:
        browser = "Safari"
    os_name = "Unknown OS"
    if "iphone" in ua_l:
        os_name = "iPhone"
    elif "ipad" in ua_l:
        os_name = "iPad"
    elif "android" in ua_l:
        os_name = "Android"
    elif "windows" in ua_l:
        os_name = "Windows"
    elif "mac os" in ua_l or "macintosh" in ua_l:
        os_name = "macOS"
    elif "cros" in ua_l:
        os_name = "ChromeOS"
    elif "linux" in ua_l:
        os_name = "Linux"
    return f"{browser} on {os_name}"


class SessionService:
    def __init__(self, db: Database, idle_hours: int, absolute_days: int, remember_days: int, reauth_minutes: int) -> None:
        self.db = db
        self.idle = timedelta(hours=idle_hours)
        self.absolute = timedelta(days=absolute_days)
        self.remember = timedelta(days=remember_days)
        self.reauth = timedelta(minutes=reauth_minutes)

    async def create(self, user_id: int, remember: bool, method: str, user_agent: str | None, ip: str | None) -> tuple[str, dict]:
        token = random_token(32)
        sid = sha256_hex(token)
        now = utcnow()
        if remember:
            expires = now + self.remember
            absolute = expires
        else:
            expires = now + self.idle
            absolute = now + self.absolute
        await self.db.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at, absolute_expires_at, remember, auth_at, auth_method, user_agent, device_label, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, user_id, iso(now), iso(now), iso(expires), iso(absolute), 1 if remember else 0,
             iso(now), method, (user_agent or "")[:512], summarize_user_agent(user_agent), ip),
        )
        log.info("session.created", "Session created", user_id=user_id, remember=remember, method=method,
                 device=summarize_user_agent(user_agent), session_prefix=sid[:8])
        return token, {"id": sid, "expires_at": iso(expires), "remember": remember}

    async def lookup(self, token: str | None) -> dict | None:
        if not token or len(token) < 20:
            return None
        sid = sha256_hex(token)
        row = await self.db.fetchone(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?", (sid,)
        )
        if row is None:
            return None
        s = row_to_dict(row)
        if s["revoked_at"]:
            return None
        now = utcnow()
        exp = parse_iso(s["expires_at"])
        abs_exp = parse_iso(s["absolute_expires_at"])
        if exp is None or abs_exp is None or now >= exp or now >= abs_exp:
            await self.db.execute("UPDATE sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (iso(now), sid))
            log.info("session.expired", "Session expired", session_prefix=sid[:8])
            return None
        # Throttled sliding update for non-remembered sessions (at most every 5 minutes).
        last_seen = parse_iso(s["last_seen_at"]) or now
        if (now - last_seen).total_seconds() > 300:
            new_exp = exp if s["remember"] else min(now + self.idle, abs_exp)
            await self.db.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                                  (iso(now), iso(new_exp), sid))
            s["last_seen_at"] = iso(now)
            s["expires_at"] = iso(new_exp)
        return s

    def is_recent(self, session: dict) -> bool:
        auth_at = parse_iso(session.get("auth_at"))
        return auth_at is not None and utcnow() - auth_at <= self.reauth

    async def mark_reauth(self, session_id: str) -> None:
        await self.db.execute("UPDATE sessions SET auth_at = ? WHERE id = ?", (now_iso(), session_id))

    async def revoke(self, session_id: str, user_id: int | None = None) -> bool:
        if user_id is None:
            n = await self.db.execute("UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now_iso(), session_id))
        else:
            n = await self.db.execute("UPDATE sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL", (now_iso(), session_id, user_id))
        if n:
            log.info("session.revoked", "Session revoked", session_prefix=session_id[:8])
        return bool(n)

    async def revoke_all(self, user_id: int, except_session: str | None = None) -> int:
        if except_session:
            n = await self.db.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND id != ?", (now_iso(), user_id, except_session))
        else:
            n = await self.db.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now_iso(), user_id))
        log.info("session.revoked_all", "All sessions revoked", user_id=user_id, count=n, kept_current=bool(except_session))
        return n

    async def list_active(self, user_id: int, current: str | None) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT id, created_at, last_seen_at, expires_at, remember, auth_method, device_label, ip FROM sessions "
            "WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at DESC",
            (user_id, now_iso()),
        )
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["current"] = d["id"] == current
            d["id"] = d["id"][:16]  # opaque handle; full hash never leaves the server
            out.append(d)
        return out

    async def resolve_handle(self, user_id: int, handle: str) -> str | None:
        if not handle or len(handle) < 8:
            return None
        row = await self.db.fetchone("SELECT id FROM sessions WHERE user_id = ? AND id LIKE ?", (user_id, handle + "%"))
        return row["id"] if row else None

    async def purge_expired(self) -> int:
        cutoff = iso(utcnow() - timedelta(days=30))
        return await self.db.execute("DELETE FROM sessions WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR absolute_expires_at < ?", (cutoff, cutoff))
