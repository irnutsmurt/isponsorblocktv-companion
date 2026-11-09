"""WebAuthn passkeys using py_webauthn. Private keys never exist server-side."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..db import Database
from ..logging import get_logger
from ..util import iso, now_iso, parse_iso, row_to_dict, utcnow

log = get_logger("auth.passkey")

CHALLENGE_TTL = timedelta(minutes=5)


class PasskeyError(Exception):
    pass


def rp_from_origin(origin: str) -> tuple[str, str]:
    parts = urlsplit(origin)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise PasskeyError("Public origin must be an http(s) URL with a hostname.")
    return parts.hostname, f"{parts.scheme}://{parts.netloc}"


def origin_allows_passkeys(origin: str) -> str | None:
    """Return a reason passkeys are unavailable for this origin, or None."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return None
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost"):
        return None
    return "Passkeys require HTTPS (browsers only expose WebAuthn in a secure context). Set up HTTPS first."


class PasskeyService:
    def __init__(self, db: Database, rp_name: str = "iSponsorBlockTV Companion") -> None:
        self.db = db
        self.rp_name = rp_name

    async def _store_challenge(self, kind: str, user_id: int | None, challenge: bytes) -> str:
        cid = secrets.token_urlsafe(16)
        now = utcnow()
        await self.db.execute(
            "INSERT INTO webauthn_challenges (id, kind, user_id, challenge, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, kind, user_id, challenge, iso(now), iso(now + CHALLENGE_TTL)),
        )
        await self.db.execute("DELETE FROM webauthn_challenges WHERE expires_at < ?", (iso(now - timedelta(hours=1)),))
        return cid

    async def _consume_challenge(self, cid: str, kind: str) -> dict:
        """Single use: mark used atomically before verification; a replay fails here."""
        async with self.db.transaction() as conn:
            cur = await conn.execute("SELECT * FROM webauthn_challenges WHERE id = ? AND kind = ?", (cid, kind))
            row = await cur.fetchone()
            if row is None or row["used"]:
                raise PasskeyError("This passkey request has expired or was already used. Try again.")
            exp = parse_iso(row["expires_at"])
            if exp is None or utcnow() > exp:
                raise PasskeyError("This passkey request has expired. Try again.")
            await conn.execute("UPDATE webauthn_challenges SET used = 1 WHERE id = ?", (cid,))
            return row_to_dict(row)

    async def list(self, user_id: int) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT id, name, created_at, last_used_at, transports, backed_up FROM passkeys WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        return [row_to_dict(r) for r in rows]

    async def begin_registration(self, user_id: int, username: str, origin: str) -> dict:
        rp_id, _ = rp_from_origin(origin)
        existing = await self.db.fetchall("SELECT credential_id, transports FROM passkeys WHERE user_id = ?", (user_id,))
        exclude = [PublicKeyCredentialDescriptor(id=r["credential_id"]) for r in existing]
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name=self.rp_name,
            user_id=str(user_id).encode(),
            user_name=username,
            user_display_name=username,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude,
            timeout=120000,
        )
        cid = await self._store_challenge("registration", user_id, options.challenge)
        log.debug("passkey.registration.begin", "Registration challenge issued", user_id=user_id, rp_id=rp_id)
        return {"challenge_id": cid, "options": json.loads(options_to_json(options))}

    async def finish_registration(self, user_id: int, origin: str, challenge_id: str, credential: dict, name: str) -> dict:
        rp_id, expected_origin = rp_from_origin(origin)
        ch = await self._consume_challenge(challenge_id, "registration")
        if ch["user_id"] != user_id:
            raise PasskeyError("Passkey request does not belong to this account.")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=ch["challenge"],
                expected_rp_id=rp_id,
                expected_origin=expected_origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            log.warning("passkey.registration.rejected", "Passkey registration rejected", user_id=user_id, reason=str(exc)[:200])
            raise PasskeyError("The browser's passkey response could not be verified.") from exc
        transports = credential.get("response", {}).get("transports") or []
        name = (name or "").strip()[:64] or "Passkey"
        try:
            pk_id = await self.db.execute_returning_id(
                "INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, transports, name, aaguid, backup_eligible, backed_up, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, verified.credential_id, verified.credential_public_key, verified.sign_count,
                 json.dumps(transports), name, verified.aaguid, int(verified.credential_backed_up is not None and verified.credential_device_type == "multi_device"),
                 int(bool(verified.credential_backed_up)), now_iso()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise PasskeyError("This passkey is already registered.") from exc
            raise
        log.info("passkey.registered", "Passkey registered", user_id=user_id, passkey_id=pk_id, name=name)
        return {"id": pk_id, "name": name}

    async def begin_authentication(self, origin: str) -> dict:
        rp_id, _ = rp_from_origin(origin)
        options = generate_authentication_options(
            rp_id=rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
            timeout=120000,
        )
        cid = await self._store_challenge("authentication", None, options.challenge)
        return {"challenge_id": cid, "options": json.loads(options_to_json(options))}

    async def finish_authentication(self, origin: str, challenge_id: str, credential: dict) -> dict:
        rp_id, expected_origin = rp_from_origin(origin)
        ch = await self._consume_challenge(challenge_id, "authentication")
        raw_id = credential.get("rawId") or credential.get("id")
        if not raw_id:
            raise PasskeyError("Malformed passkey response.")
        from webauthn.helpers import base64url_to_bytes
        try:
            cred_id = base64url_to_bytes(raw_id)
        except Exception as exc:
            raise PasskeyError("Malformed passkey response.") from exc
        row = await self.db.fetchone(
            "SELECT p.*, u.username FROM passkeys p JOIN users u ON u.id = p.user_id WHERE p.credential_id = ?", (cred_id,)
        )
        if row is None:
            log.warning("passkey.auth.unknown_credential", "Unknown passkey presented")
            raise PasskeyError("This passkey is not registered here.")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=ch["challenge"],
                expected_rp_id=rp_id,
                expected_origin=expected_origin,
                credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"],
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            log.warning("passkey.auth.rejected", "Passkey authentication rejected", passkey_id=row["id"], reason=str(exc)[:200])
            raise PasskeyError("Passkey could not be verified.") from exc
        await self.db.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
                              (verified.new_sign_count, now_iso(), row["id"]))
        log.info("passkey.auth.success", "Passkey authentication succeeded", user_id=row["user_id"], passkey_id=row["id"])
        return {"user_id": row["user_id"], "username": row["username"], "passkey_id": row["id"], "name": row["name"]}

    async def rename(self, user_id: int, passkey_id: int, name: str) -> bool:
        name = (name or "").strip()[:64]
        if not name:
            raise PasskeyError("Name cannot be empty.")
        return bool(await self.db.execute("UPDATE passkeys SET name = ? WHERE id = ? AND user_id = ?", (name, passkey_id, user_id)))

    async def remove(self, user_id: int, passkey_id: int) -> bool:
        n = await self.db.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?", (passkey_id, user_id))
        if n:
            log.info("passkey.removed", "Passkey removed", user_id=user_id, passkey_id=passkey_id)
        return bool(n)

    async def any_registered(self) -> bool:
        return bool(await self.db.scalar("SELECT COUNT(*) FROM passkeys"))


def b64(data: bytes) -> str:
    return bytes_to_base64url(data)
