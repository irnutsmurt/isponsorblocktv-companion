"""Authentication endpoints. Public: setup status/complete, login, passkey login, recovery."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http
from pydantic import BaseModel, Field

from ..context import request_info
from ..db import DatabaseUnavailable
from ..logging import get_logger
from ..util import now_iso
from .deps import (
    actor,
    clear_session_cookies,
    cookie_should_be_secure,
    current_session,
    enforce_csrf,
    read_session_token,
    recent_auth,
    set_session_cookie,
)
from .passkeys import PasskeyError, origin_allows_passkeys

log = get_logger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "Incorrect username or password."


class SetupBody(BaseModel):
    setup_token: str = Field(min_length=8, max_length=256)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    remember: bool = False


class ReauthBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class RecoverBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    recovery_code: str = Field(min_length=10, max_length=64)
    new_password: str = Field(min_length=1, max_length=1024)


class PasskeyFinishBody(BaseModel):
    challenge_id: str
    credential: dict
    name: str | None = None
    origin: str | None = None
    remember: bool = False


class PasskeyBeginBody(BaseModel):
    origin: str | None = None


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _https_guard(request: Request, info) -> None:
    settings = request.app.state.settings
    if settings.require_https and not info.is_secure:
        raise HTTPException(http.HTTP_403_FORBIDDEN, "This server only accepts sign-in over HTTPS.")


def _effective_origin(request: Request, info, claimed: str | None) -> str:
    """Origin used for WebAuthn: the configured public origin, which must match the page's."""
    settings = request.app.state.settings
    configured = request.app.state.runtime_settings.get("public_origin") or settings.public_origin
    page_origin = (claimed or info.origin or "").rstrip("/")
    if not configured:
        raise PasskeyError("Set the public origin (Settings → Access) before using passkeys. "
                           "Passkeys are bound to a stable hostname.")
    if page_origin and page_origin != configured:
        raise PasskeyError(f"You are using {page_origin}, but passkeys are configured for {configured}. "
                           "Open the app at the configured address or update the public origin.")
    return configured


@router.get("/setup/status")
async def setup_status(request: Request):
    info = request_info(request)
    try:
        status = await request.app.state.bootstrap.status()
    except DatabaseUnavailable:
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE, "Storage unavailable.")
    status["https"] = info.is_secure
    status["require_https"] = request.app.state.settings.require_https
    return status


@router.post("/setup/complete")
async def setup_complete(body: SetupBody, request: Request, response: Response):
    info = request_info(request)
    enforce_csrf(request, info)
    _https_guard(request, info)
    st = request.app.state
    limiter = st.limiter
    decision = await limiter.check(info.client_ip, "__setup__")
    if not decision.allowed:
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again later.",
                            headers={"Retry-After": str(int(decision.retry_after))})
    if decision.delay:
        await asyncio.sleep(decision.delay)
    if await st.bootstrap.setup_complete():
        raise HTTPException(http.HTTP_409_CONFLICT, "Setup has already been completed.")
    if not st.bootstrap.verify_token(body.setup_token):
        await limiter.record(info.client_ip, "__setup__", False)
        log.warning("bootstrap.token.rejected", "Setup token rejected", ip=info.client_ip)
        raise HTTPException(http.HTTP_403_FORBIDDEN, "Setup token is invalid or expired. Check the container log for the current token.")
    err = st.bootstrap.validate_username(body.username) or st.passwords.validate_new_password(body.password)
    if err:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, err)
    pw_hash = await st.passwords.hash_async(body.password)
    recovery_code, recovery_hash = await st.recovery.hash_for_new_user()
    try:
        user_id = await st.bootstrap.create_admin(body.username, pw_hash, recovery_hash)
    except RuntimeError:
        raise HTTPException(http.HTTP_409_CONFLICT, "Setup has already been completed.")
    await limiter.record(info.client_ip, "__setup__", True)
    token, meta = await st.sessions.create(user_id, False, "setup", request.headers.get("user-agent"), info.client_ip)
    set_session_cookie(response, request, info, token, None)
    await st.audit.record("setup.completed", "ok", actor={"user_id": user_id, "username": body.username.strip()}, ip=info.client_ip)
    return {"ok": True, "username": body.username.strip(), "recovery_code": recovery_code,
            "cookie_secure": cookie_should_be_secure(request, info)}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    info = request_info(request)
    enforce_csrf(request, info)
    _https_guard(request, info)
    st = request.app.state
    try:
        if not await st.bootstrap.setup_complete():
            raise HTTPException(http.HTTP_409_CONFLICT, "Setup has not been completed yet.")
        decision = await st.limiter.check(info.client_ip, body.username)
        if not decision.allowed:
            log.warning("login.throttled", "Login throttled", ip=info.client_ip, retry_after=decision.retry_after, bucket=decision.reason)
            raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS, "Too many sign-in attempts. Please wait and try again.",
                                headers={"Retry-After": str(int(decision.retry_after))})
        if decision.delay:
            await asyncio.sleep(decision.delay)
        row = await st.db.fetchone("SELECT id, username, password_hash FROM users WHERE username = ?", (body.username.strip(),))
        ok = await st.passwords.verify_async(row["password_hash"] if row else None, body.password)
        if not ok or row is None:
            await st.limiter.record(info.client_ip, body.username, False)
            log.warning("login.failed", "Login failed", ip=info.client_ip, username_known=row is not None)
            raise HTTPException(http.HTTP_401_UNAUTHORIZED, GENERIC_LOGIN_ERROR)
        await st.limiter.record(info.client_ip, body.username, True)
        if st.passwords.needs_rehash(row["password_hash"]):
            await st.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (await st.passwords.hash_async(body.password), row["id"]))
        token, meta = await st.sessions.create(row["id"], body.remember, "password", request.headers.get("user-agent"), info.client_ip)
    except DatabaseUnavailable:
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE, "Authentication storage unavailable.")
    max_age = st.settings.remember_days * 86400 if body.remember else None
    set_session_cookie(response, request, info, token, max_age)
    log.info("login.success", "User signed in", user_id=row["id"], remember=body.remember, ip=info.client_ip, https=info.is_secure)
    return {"ok": True, "username": row["username"], "remember": body.remember, "insecure_transport": not info.is_secure}


@router.post("/logout")
async def logout(request: Request, response: Response):
    info = request_info(request)
    enforce_csrf(request, info)
    st = request.app.state
    token = read_session_token(request)
    if token:
        from ..util import sha256_hex
        try:
            await st.sessions.revoke(sha256_hex(token))
        except DatabaseUnavailable:
            pass
    clear_session_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    info = request.state.info
    passkeys = await st.passkeys.list(session["user_id"])
    configured_origin = st.runtime_settings.get("public_origin") or st.settings.public_origin
    return {
        "username": session["username"],
        "user_id": session["user_id"],
        "session": {"created_at": session["created_at"], "expires_at": session["expires_at"],
                    "remember": bool(session["remember"]), "auth_method": session["auth_method"],
                    "recent_auth": st.sessions.is_recent(session)},
        "passkeys": passkeys,
        "passkey_origin": configured_origin,
        "passkey_blocker": origin_allows_passkeys(configured_origin) if configured_origin else "Public origin not configured.",
        "has_recovery_code": await st.recovery.has_active(session["user_id"]),
        "insecure_transport": not info.is_secure,
    }


@router.post("/reauth")
async def reauth(body: ReauthBody, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    info = request.state.info
    decision = await st.limiter.check(info.client_ip, session["username"])
    if not decision.allowed:
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please wait.",
                            headers={"Retry-After": str(int(decision.retry_after))})
    if decision.delay:
        await asyncio.sleep(decision.delay)
    row = await st.db.fetchone("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],))
    if not await st.passwords.verify_async(row["password_hash"] if row else None, body.password):
        await st.limiter.record(info.client_ip, session["username"], False)
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Incorrect password.")
    await st.limiter.record(info.client_ip, session["username"], True)
    await st.sessions.mark_reauth(session["id"])
    log.info("reauth.success", "Re-authentication succeeded", user_id=session["user_id"])
    return {"ok": True}


@router.post("/password")
async def change_password(body: ChangePasswordBody, request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    info = request.state.info
    row = await st.db.fetchone("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],))
    if not await st.passwords.verify_async(row["password_hash"] if row else None, body.current_password):
        await st.limiter.record(info.client_ip, session["username"], False)
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Current password is incorrect.")
    err = st.passwords.validate_new_password(body.new_password)
    if err:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, err)
    new_hash = await st.passwords.hash_async(body.new_password)
    await st.db.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?", (new_hash, now_iso(), session["user_id"]))
    revoked = await st.sessions.revoke_all(session["user_id"], except_session=session["id"])
    await st.audit.record("password.changed", "ok", actor=actor(session), fields=["password"], ip=info.client_ip)
    return {"ok": True, "other_sessions_revoked": revoked}


@router.post("/recover")
async def recover(body: RecoverBody, request: Request, response: Response):
    info = request_info(request)
    enforce_csrf(request, info)
    _https_guard(request, info)
    st = request.app.state
    decision = await st.limiter.check(info.client_ip, body.username)
    if not decision.allowed:
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please wait.",
                            headers={"Retry-After": str(int(decision.retry_after))})
    await asyncio.sleep(max(decision.delay, 1.0))
    err = st.passwords.validate_new_password(body.new_password)
    if err:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, err)
    row = await st.db.fetchone("SELECT id, username FROM users WHERE username = ?", (body.username.strip(),))
    ok = row is not None and await st.recovery.verify_and_consume(row["id"], body.recovery_code)
    if not ok:
        await st.limiter.record(info.client_ip, body.username, False)
        log.warning("recovery.failed", "Account recovery failed", ip=info.client_ip)
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Recovery code or username is incorrect.")
    new_hash = await st.passwords.hash_async(body.new_password)
    await st.db.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?", (new_hash, now_iso(), row["id"]))
    await st.sessions.revoke_all(row["id"])
    new_code = await st.recovery.issue(row["id"])
    await st.limiter.record(info.client_ip, body.username, True)
    token, meta = await st.sessions.create(row["id"], False, "recovery", request.headers.get("user-agent"), info.client_ip)
    set_session_cookie(response, request, info, token, None)
    await st.audit.record("account.recovered", "ok", actor={"user_id": row["id"], "username": row["username"]}, fields=["password", "sessions", "recovery_code"], ip=info.client_ip)
    return {"ok": True, "recovery_code": new_code}


@router.post("/recovery-code/rotate")
async def rotate_recovery(request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    code = await st.recovery.issue(session["user_id"])
    await st.audit.record("recovery_code.rotated", "ok", actor=actor(session), ip=request.state.info.client_ip)
    return {"recovery_code": code}


# ---- sessions ---------------------------------------------------------------------
@router.get("/sessions")
async def list_sessions(request: Request, session: dict = Depends(current_session)):
    return {"sessions": await request.app.state.sessions.list_active(session["user_id"], session["id"])}


@router.delete("/sessions/{handle}")
async def revoke_session(handle: str, request: Request, session: dict = Depends(current_session)):
    st = request.app.state
    sid = await st.sessions.resolve_handle(session["user_id"], handle)
    if not sid:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Session not found.")
    await st.sessions.revoke(sid, session["user_id"])
    return {"ok": True, "current": sid == session["id"]}


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(request: Request, response: Response, session: dict = Depends(current_session)):
    st = request.app.state
    n = await st.sessions.revoke_all(session["user_id"])
    clear_session_cookies(response)
    await st.audit.record("sessions.revoked_all", "ok", actor=actor(session), ip=request.state.info.client_ip)
    return {"ok": True, "revoked": n}


# ---- passkeys -----------------------------------------------------------------------
@router.post("/passkeys/register/begin")
async def passkey_register_begin(body: PasskeyBeginBody, request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    try:
        origin = _effective_origin(request, request.state.info, body.origin)
        blocker = origin_allows_passkeys(origin)
        if blocker:
            raise PasskeyError(blocker)
        return await st.passkeys.begin_registration(session["user_id"], session["username"], origin)
    except PasskeyError as exc:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/passkeys/register/finish")
async def passkey_register_finish(body: PasskeyFinishBody, request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    try:
        origin = _effective_origin(request, request.state.info, body.origin)
        result = await st.passkeys.finish_registration(session["user_id"], origin, body.challenge_id, body.credential, body.name or "Passkey")
    except PasskeyError as exc:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, str(exc))
    await st.audit.record("passkey.registered", "ok", actor=actor(session), target=str(result["id"]), ip=request.state.info.client_ip)
    return result


@router.post("/passkeys/login/begin")
async def passkey_login_begin(body: PasskeyBeginBody, request: Request):
    info = request_info(request)
    enforce_csrf(request, info)
    st = request.app.state
    if not await st.bootstrap.setup_complete():
        raise HTTPException(http.HTTP_409_CONFLICT, "Setup has not been completed yet.")
    try:
        origin = _effective_origin(request, info, body.origin)
        if not await st.passkeys.any_registered():
            raise PasskeyError("No passkeys are registered. Sign in with your password first.")
        return await st.passkeys.begin_authentication(origin)
    except PasskeyError as exc:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/passkeys/login/finish")
async def passkey_login_finish(body: PasskeyFinishBody, request: Request, response: Response):
    info = request_info(request)
    enforce_csrf(request, info)
    _https_guard(request, info)
    st = request.app.state
    decision = await st.limiter.check(info.client_ip, "__passkey__")
    if not decision.allowed:
        raise HTTPException(http.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please wait.",
                            headers={"Retry-After": str(int(decision.retry_after))})
    try:
        origin = _effective_origin(request, info, body.origin)
        result = await st.passkeys.finish_authentication(origin, body.challenge_id, body.credential)
    except PasskeyError as exc:
        await st.limiter.record(info.client_ip, "__passkey__", False)
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, str(exc))
    await st.limiter.record(info.client_ip, "__passkey__", True)
    token, meta = await st.sessions.create(result["user_id"], body.remember, "passkey", request.headers.get("user-agent"), info.client_ip)
    set_session_cookie(response, request, info, token, st.settings.remember_days * 86400 if body.remember else None)
    return {"ok": True, "username": result["username"], "remember": body.remember}


@router.patch("/passkeys/{passkey_id}")
async def passkey_rename(passkey_id: int, body: RenameBody, request: Request, session: dict = Depends(current_session)):
    try:
        ok = await request.app.state.passkeys.rename(session["user_id"], passkey_id, body.name)
    except PasskeyError as exc:
        raise HTTPException(http.HTTP_400_BAD_REQUEST, str(exc))
    if not ok:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Passkey not found.")
    return {"ok": True}


@router.delete("/passkeys/{passkey_id}")
async def passkey_remove(passkey_id: int, request: Request, session: dict = Depends(recent_auth)):
    st = request.app.state
    if not await st.passkeys.remove(session["user_id"], passkey_id):
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Passkey not found.")
    await st.audit.record("passkey.removed", "ok", actor=actor(session), target=str(passkey_id), ip=request.state.info.client_ip)
    return {"ok": True}
