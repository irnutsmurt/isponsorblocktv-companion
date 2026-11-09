"""FastAPI dependencies enforcing authentication, freshness, and CSRF on every request."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response
from fastapi import status as http

from ..context import RequestInfo, request_info
from ..db import DatabaseUnavailable
from ..logging import get_logger
from .sessions import COOKIE_NAME, COOKIE_NAME_SECURE

log = get_logger("auth")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"


def cookie_should_be_secure(request: Request, info: RequestInfo) -> bool:
    mode = request.app.state.settings.cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    return info.is_secure


def session_cookie_name(secure: bool) -> str:
    return COOKIE_NAME_SECURE if secure else COOKIE_NAME


def read_session_token(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME_SECURE) or request.cookies.get(COOKIE_NAME)


def set_session_cookie(response: Response, request: Request, info: RequestInfo, token: str, max_age: int | None) -> None:
    secure = cookie_should_be_secure(request, info)
    response.set_cookie(
        key=session_cookie_name(secure), value=token, max_age=max_age, path="/",
        secure=secure, httponly=True, samesite="lax",
    )


def clear_session_cookies(response: Response) -> None:
    for name, secure in ((COOKIE_NAME_SECURE, True), (COOKIE_NAME, False)):
        response.delete_cookie(key=name, path="/", secure=secure, httponly=True, samesite="lax")


def allowed_origins(request: Request, info: RequestInfo) -> set[str]:
    settings = request.app.state.settings
    allowed = {info.origin} if info.origin else set()
    if settings.public_origin:
        allowed.add(settings.public_origin)
    return allowed


def enforce_csrf(request: Request, info: RequestInfo) -> None:
    """State-changing requests must carry our custom header and an Origin we serve."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER, "").lower() != "fetch":
        raise HTTPException(http.HTTP_403_FORBIDDEN, "Missing request header; refresh the page and try again.")
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer:
            from urllib.parse import urlsplit
            p = urlsplit(referer)
            origin = f"{p.scheme}://{p.netloc}"
    if origin is None:
        # Non-browser clients (tests) without Origin are allowed only with the custom header.
        return
    if origin.rstrip("/") not in allowed_origins(request, info):
        log.warning("csrf.origin_rejected", "Rejected cross-origin state-changing request",
                    origin=origin, expected=sorted(allowed_origins(request, info)))
        raise HTTPException(http.HTTP_403_FORBIDDEN, "Cross-origin request rejected.")


async def current_session(request: Request) -> dict:
    info = request_info(request)
    request.state.info = info
    enforce_csrf(request, info)
    sessions = request.app.state.sessions
    try:
        session = await sessions.lookup(read_session_token(request))
    except DatabaseUnavailable as exc:
        log.critical("auth.storage_unavailable", "Authentication storage unavailable; failing closed", error=str(exc))
        raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE, "Authentication storage unavailable.") from exc
    if session is None:
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Not signed in.")
    request.state.session = session
    return session


async def recent_auth(session: dict = Depends(current_session), request: Request = None) -> dict:
    sessions = request.app.state.sessions
    if not sessions.is_recent(session):
        raise HTTPException(http.HTTP_403_FORBIDDEN, detail={"code": "reauth_required",
                            "message": "Please confirm your password to continue."})
    return session


def actor(session: dict) -> dict:
    return {"user_id": session["user_id"], "username": session["username"]}
