"""Reverse-proxy handling: HTTPS detected without configuration; one-click proxy trust."""
import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD

PUBLIC = "https://isbtv.example.org:8443"


@pytest.fixture
def proxied(env):
    """Client whose TCP peer is a Docker-network proxy address (like Caddy at 172.31.0.5)."""
    from webui.main import create_app
    from webui.settings import get_settings
    with TestClient(create_app(get_settings()), base_url="http://isbtv.example.org:8443", client=("172.31.0.5", 40000)) as c:
        c.headers.update({"X-Requested-With": "fetch", "Origin": PUBLIC, "X-Forwarded-Proto": "https", "X-Forwarded-For": "192.168.1.50"})
        r = c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
        assert r.status_code == 200
        # HTTPS was detected from the browser's Origin, so the cookie is __Host- and Secure; a real browser on
        # https:// sends it back. The test transport is plain HTTP, so send it explicitly.
        sc = r.headers["set-cookie"]
        assert sc.startswith("__Host-isbtv_session=") and "Secure" in sc
        c.headers["Cookie"] = sc.split(";")[0]
        yield c


def test_https_detected_from_browser_without_trusted_proxies(proxied):
    me = proxied.get("/api/auth/me", headers={"Referer": PUBLIC + "/account"}).json()
    assert me["insecure_transport"] is False
    s = proxied.get("/api/settings", headers={"Referer": PUBLIC + "/account"}).json()
    assert s["current_origin"] == PUBLIC and s["proxy"]["https_detected_by"] == "browser"


def test_browser_claim_for_another_host_is_ignored(proxied):
    me = proxied.get("/api/auth/me", headers={"Origin": "https://other.example", "Referer": "https://other.example/x"}).json()
    assert me["insecure_transport"] is True


def test_untrusted_proxy_headers_ignored_but_offered(proxied):
    s = proxied.get("/api/settings", headers={"Referer": PUBLIC + "/"}).json()
    assert s["proxy"]["client_ip"] == "172.31.0.5"           # X-Forwarded-For not believed
    assert s["proxy"]["detected_untrusted"]["address"] == "172.31.0.5"


def test_one_click_trust_then_untrust(proxied):
    r = proxied.put("/api/settings", json={"trust_proxy": "172.31.0.5"})
    assert r.status_code == 200, r.text
    s = r.json()["proxy"]
    assert s["trusted_runtime"] == ["172.31.0.5/32"] and s["via_trusted_proxy"] is True and s["client_ip"] == "192.168.1.50"
    assert s["https_detected_by"] == "proxy" and s["detected_untrusted"] is None
    sessions = proxied.get("/api/auth/sessions").json()["sessions"]
    assert sessions  # still signed in
    r = proxied.put("/api/settings", json={"untrust_proxy": "172.31.0.5/32"})
    assert r.json()["proxy"]["trusted_runtime"] == []


def test_trust_validation(proxied):
    assert proxied.put("/api/settings", json={"trust_proxy": "not-an-ip"}).status_code == 422
    assert proxied.put("/api/settings", json={"trust_proxy": "0.0.0.0/0"}).status_code == 422


def test_trusted_proxy_survives_restart(env):
    from webui.main import create_app
    from webui.settings import get_settings
    with TestClient(create_app(get_settings()), base_url="http://h", client=("172.31.0.5", 1)) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
        assert c.put("/api/settings", json={"trust_proxy": "172.31.0.0/16"}).status_code == 200
        token = c.cookies.get("isbtv_session")
    with TestClient(create_app(get_settings()), base_url="http://h", client=("172.31.0.9", 1)) as c:
        c.cookies.set("isbtv_session", token)
        s = c.get("/api/settings", headers={"X-Forwarded-For": "10.0.0.7"}).json()["proxy"]
        assert s["via_trusted_proxy"] is True and s["client_ip"] == "10.0.0.7"


def test_trusting_requires_recent_auth(proxied):
    proxied.portal.call(proxied.app.state.db.execute, "UPDATE sessions SET auth_at = '2000-01-01T00:00:00.000Z'")
    r = proxied.put("/api/settings", json={"trust_proxy": "172.31.0.5"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_required"
