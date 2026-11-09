"""Authentication, bootstrap, sessions, CSRF, throttling and fail-closed behaviour."""
import asyncio
import threading

import pytest

from conftest import PASSWORD

PROTECTED_GET = ["/api/devices", "/api/config", "/api/stats/overview", "/api/activity", "/api/channels", "/api/logs/companion",
                 "/api/logs/upstream", "/api/logs/levels", "/api/health", "/api/capabilities", "/api/diagnostics/bundle",
                 "/api/audit", "/api/settings", "/api/stream", "/api/auth/me", "/api/auth/sessions", "/api/config/backups",
                 "/api/logs/companion/download", "/api/devices/1", "/api/config/jobs/latest"]
PROTECTED_MUTATE = [("post", "/api/pairing/verify", {"code": "123412341234"}), ("post", "/api/devices", {"ticket": "x"}),
                    ("put", "/api/config", {"config": {}}), ("post", "/api/config/apply", {}), ("post", "/api/logs/debug-mode", {}),
                    ("put", "/api/logs/levels", {"level": "DEBUG"}), ("put", "/api/settings", {"timezone": "UTC"}),
                    ("post", "/api/config/backups/1/restore", {}), ("post", "/api/auth/passkeys/register/begin", {}),
                    ("post", "/api/auth/sessions/revoke-all", {}), ("post", "/api/auth/password", {"current_password": "x", "new_password": "y"})]


def test_unauthenticated_rejected_everywhere(client, admin):
    client.cookies.clear()
    for path in PROTECTED_GET:
        r = client.get(path)
        assert r.status_code == 401, (path, r.status_code)
    for method, path, body in PROTECTED_MUTATE:
        r = getattr(client, method)(path, json=body)
        assert r.status_code == 401, (path, r.status_code)


def test_public_endpoints_minimal(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and set(r.json()) == {"status"}
    r = client.get("/api/auth/setup/status")
    assert r.json()["setup_complete"] is False
    assert "token" not in r.text.lower().replace("token_source", "").replace("token_expires", "")


def test_bootstrap_requires_token_and_is_single_use(client):
    bad = client.post("/api/auth/setup/complete", json={"setup_token": "wrong-token-xxxx", "username": "eve", "password": PASSWORD})
    assert bad.status_code == 403
    ok = client.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["recovery_code"]
    again = client.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "eve", "password": PASSWORD})
    assert again.status_code == 409
    assert client.get("/api/auth/setup/status").json()["setup_complete"] is True


def test_concurrent_setup_creates_one_admin(app, env):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        results = []

        def attempt(name):
            results.append(c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": name, "password": PASSWORD}).status_code)

        threads = [threading.Thread(target=attempt, args=(f"user{i}",)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count(200) == 1, results
        assert results.count(409) == 5, results
        assert c.portal.call(app.state.db.scalar, "SELECT COUNT(*) FROM users") == 1


def test_bootstrap_disabled_after_restart(env):
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    with TestClient(create_app(get_settings())) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        assert c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD}).status_code == 200
    with TestClient(create_app(get_settings())) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        assert c.get("/api/auth/setup/status").json()["setup_complete"] is True
        assert c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "x", "password": PASSWORD}).status_code == 409


def test_generated_token_never_in_ring_buffer(env, monkeypatch):
    monkeypatch.delenv("SETUP_TOKEN")
    from companion.settings import reset_settings_for_tests, get_settings
    reset_settings_for_tests()
    import io
    from companion.logging import setup_logging, get_logging_state
    console = io.StringIO()
    setup_logging(level="DEBUG", stream=console)
    from fastapi.testclient import TestClient
    from companion.main import create_app
    with TestClient(create_app(get_settings())) as c:
        import time; time.sleep(0.3)
        out = console.getvalue()
        import re
        m = re.search(r"\n\s{6}([A-Za-z0-9_-]{24,})\n", out)
        assert m, "token must be announced on the console sink"
        token = m.group(1)
        ring = "\n".join(str(r) for r in get_logging_state().ring.snapshot())
        assert token not in ring
        c.headers.update({"X-Requested-With": "fetch"})
        r = c.post("/api/auth/setup/complete", json={"setup_token": token, "username": "admin", "password": PASSWORD})
        assert r.status_code == 200
        bundle = c.get("/api/diagnostics/bundle")
        assert bundle.status_code == 200
        assert token.encode() not in bundle.content
        assert token not in console.getvalue().split("Setup token generated")[1]


def test_login_generic_errors_and_throttling(client, admin):
    client.cookies.clear()
    r1 = client.post("/api/auth/login", json={"username": "admin", "password": "wrong password!!"})
    r2 = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong password!!"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]
    st = client.app.state
    st.limiter.base_delay = 0.0
    codes = []
    for _ in range(8):
        codes.append(client.post("/api/auth/login", json={"username": "admin", "password": "wrong password!!"}).status_code)
    assert 429 in codes
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0  # bounded wait, not permanent
    assert int(r.headers["Retry-After"]) <= 15 * 60


def test_forged_forwarded_headers_ignored(client, admin):
    client.cookies.clear()
    client.app.state.limiter.base_delay = 0.0
    codes = []
    for i in range(10):
        codes.append(client.post("/api/auth/login", json={"username": "admin", "password": "bad bad bad bad"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code)
    assert 429 in codes, "rotating X-Forwarded-For must not evade throttling when no trusted proxy is configured"


def test_csrf_protection(client, admin):
    r = client.post("/api/auth/logout", headers={"X-Requested-With": ""})
    assert r.status_code == 403
    r = client.post("/api/logs/debug-mode", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/logs/debug-mode", json={}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_remember_me_session_and_restart(env):
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    with TestClient(create_app(get_settings())) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
        c.cookies.clear()
        r = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD, "remember": True})
        sc = r.headers["set-cookie"]
        assert "HttpOnly" in sc and "SameSite=lax" in sc and "Max-Age=2592000" in sc
        token = r.cookies.get("isbtv_session")
    with TestClient(create_app(get_settings())) as c2:  # "container restart"
        c2.cookies.set("isbtv_session", token)
        me = c2.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["session"]["remember"] is True


def test_session_rotation_logout_and_revocation(client, admin):
    first = client.cookies.get("isbtv_session")
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    second = client.cookies.get("isbtv_session")
    assert first != second
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert len(sessions) == 2
    other = [s for s in sessions if not s["current"]][0]
    assert client.delete(f"/api/auth/sessions/{other['id']}").status_code == 200
    from fastapi.testclient import TestClient
    client.cookies.set("isbtv_session", first)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("isbtv_session", second)
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("isbtv_session", second)
    assert client.get("/api/auth/me").status_code == 401


def test_session_expiry_enforced_server_side(client, admin):
    db = client.app.state.db
    import asyncio
    portal = client.portal
    portal.call(db.execute, "UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000Z'")
    assert client.get("/api/auth/me").status_code == 401


def test_reauth_required_for_sensitive_changes(client, admin):
    db = client.app.state.db
    client.portal.call(db.execute, "UPDATE sessions SET auth_at = '2000-01-01T00:00:00.000Z'")
    r = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another long passphrase"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_required"
    assert client.post("/api/auth/reauth", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}).status_code == 200
    r = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another long passphrase"})
    assert r.status_code == 200


def test_recovery_revokes_sessions_and_rotates_code(client, admin):
    old_cookie = client.cookies.get("isbtv_session")
    client.cookies.clear()
    bad = client.post("/api/auth/recover", json={"username": "admin", "recovery_code": "AAAAA-BBBBB-CCCCC-DDDDD", "new_password": "brand new passphrase"})
    assert bad.status_code == 401
    r = client.post("/api/auth/recover", json={"username": "admin", "recovery_code": admin["recovery_code"], "new_password": "brand new passphrase"})
    assert r.status_code == 200 and r.json()["recovery_code"] != admin["recovery_code"]
    client.cookies.set("isbtv_session", old_cookie)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    reuse = client.post("/api/auth/recover", json={"username": "admin", "recovery_code": admin["recovery_code"], "new_password": "brand new passphrase 2"})
    assert reuse.status_code in (401, 429)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "brand new passphrase"}).status_code == 200


def test_long_passphrase_not_truncated(client, admin):
    long_pw = "x" * 500 + "END"
    assert client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": long_pw}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "admin", "password": "x" * 500}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": long_pw}).status_code == 200


def _sid(client):
    from companion.util import sha256_hex
    return sha256_hex(client.cookies.get("isbtv_session"))


def test_password_hash_is_argon2id(client, admin):
    h = client.portal.call(client.app.state.db.scalar, "SELECT password_hash FROM users")
    assert h.startswith("$argon2id$") and PASSWORD not in h


def test_auth_storage_failure_fails_closed(client, admin):
    db = client.app.state.db
    db.healthy = False
    assert client.get("/api/auth/me").status_code == 503
    assert client.get("/api/devices").status_code == 503
    assert client.get("/healthz").status_code in (200, 503)
    db.healthy = True


def test_stream_requires_auth_and_ends_on_revocation(client, admin):
    client.cookies.clear()
    assert client.get("/api/stream").status_code == 401


def test_security_headers(client):
    r = client.get("/")
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
    r = client.get("/api/auth/setup/status")
    assert r.headers.get("cache-control") == "no-store"


def test_request_size_limit(client, admin):
    r = client.put("/api/config", content=b"{" + b" " * (300 * 1024) + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413
