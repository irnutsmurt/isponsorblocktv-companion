"""WebAuthn registration/authentication against a software authenticator.

These are simulated tests: they exercise the real server-side verification (py_webauthn) with
correctly formed credentials, but no physical authenticator or browser is involved.
"""
import base64
import hashlib
import json
import os
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import PASSWORD

ORIGIN = "https://webui.example.test"
RP_ID = "webui.example.test"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self, rp_id=RP_ID):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.rp_id = rp_id
        self.count = 0

    def _cose(self):
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def register(self, options, origin=ORIGIN, uv=True):
        client_data = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin, "crossOrigin": False}).encode()
        flags = 0x01 | (0x04 if uv else 0) | 0x40
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0) + b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "attestationObject": b64u(att), "transports": ["internal"]}, "clientExtensionResults": {}}

    def assert_(self, options, origin=ORIGIN, uv=True):
        self.count += 1
        client_data = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin, "crossOrigin": False}).encode()
        flags = 0x01 | (0x04 if uv else 0)
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data), "signature": b64u(sig), "userHandle": None}, "clientExtensionResults": {}}


@pytest.fixture
def pk_client(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", ORIGIN)
    from webui.settings import reset_settings_for_tests, get_settings
    reset_settings_for_tests()
    from fastapi.testclient import TestClient
    from webui.main import create_app
    with TestClient(create_app(get_settings()), base_url=ORIGIN) as c:
        c.headers.update({"X-Requested-With": "fetch", "Origin": ORIGIN})
        assert c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD}).status_code == 200
        yield c


def register(c, auth, name="Test key"):
    begin = c.post("/api/auth/passkeys/register/begin", json={"origin": ORIGIN})
    assert begin.status_code == 200, begin.text
    assert begin.json()["options"]["authenticatorSelection"]["userVerification"] == "required"
    cred = auth.register(begin.json()["options"])
    return c.post("/api/auth/passkeys/register/finish", json={"challenge_id": begin.json()["challenge_id"], "credential": cred, "name": name, "origin": ORIGIN})


def test_enroll_and_login(pk_client):
    a = SoftAuthenticator()
    r = register(pk_client, a)
    assert r.status_code == 200, r.text
    me = pk_client.get("/api/auth/me").json()
    assert me["passkeys"][0]["name"] == "Test key"
    pk_client.cookies.clear()
    begin = pk_client.post("/api/auth/passkeys/login/begin", json={"origin": ORIGIN}).json()
    r = pk_client.post("/api/auth/passkeys/login/finish", json={"challenge_id": begin["challenge_id"], "credential": a.assert_(begin["options"]), "origin": ORIGIN, "remember": True})
    assert r.status_code == 200, r.text
    assert "Max-Age=2592000" in r.headers["set-cookie"]
    me = pk_client.get("/api/auth/me").json()
    assert me["session"]["auth_method"] == "passkey" and me["passkeys"][0]["last_used_at"]


def test_challenge_replay_rejected(pk_client):
    a = SoftAuthenticator()
    assert register(pk_client, a).status_code == 200
    begin = pk_client.post("/api/auth/passkeys/login/begin", json={"origin": ORIGIN}).json()
    cred = a.assert_(begin["options"])
    assert pk_client.post("/api/auth/passkeys/login/finish", json={"challenge_id": begin["challenge_id"], "credential": cred, "origin": ORIGIN}).status_code == 200
    replay = pk_client.post("/api/auth/passkeys/login/finish", json={"challenge_id": begin["challenge_id"], "credential": cred, "origin": ORIGIN})
    assert replay.status_code == 401


def test_wrong_origin_rejected(pk_client):
    a = SoftAuthenticator()
    assert register(pk_client, a).status_code == 200
    begin = pk_client.post("/api/auth/passkeys/login/begin", json={"origin": ORIGIN}).json()
    cred = a.assert_(begin["options"], origin="https://evil.example")
    r = pk_client.post("/api/auth/passkeys/login/finish", json={"challenge_id": begin["challenge_id"], "credential": cred, "origin": ORIGIN})
    assert r.status_code == 401


def test_wrong_rp_id_rejected(pk_client):
    a = SoftAuthenticator(rp_id="evil.example")
    r = register(pk_client, a)
    assert r.status_code == 400


def test_user_verification_required(pk_client):
    a = SoftAuthenticator()
    begin = pk_client.post("/api/auth/passkeys/register/begin", json={"origin": ORIGIN}).json()
    r = pk_client.post("/api/auth/passkeys/register/finish", json={"challenge_id": begin["challenge_id"], "credential": a.register(begin["options"], uv=False), "origin": ORIGIN})
    assert r.status_code == 400


def test_page_origin_mismatch_explained(pk_client):
    r = pk_client.post("/api/auth/passkeys/register/begin", json={"origin": "https://192.168.1.10:8099"})
    assert r.status_code == 400 and "configured for" in r.json()["detail"]


def test_cancelled_ceremony_leaves_no_state(pk_client):
    begin = pk_client.post("/api/auth/passkeys/register/begin", json={"origin": ORIGIN}).json()
    assert begin["challenge_id"]  # user cancels in the browser: nothing is submitted
    assert pk_client.get("/api/auth/me").json()["passkeys"] == []


def test_removal_requires_recent_auth_and_blocks_login(pk_client):
    a = SoftAuthenticator()
    pk_id = register(pk_client, a).json()["id"]
    pk_client.portal.call(pk_client.app.state.db.execute, "UPDATE sessions SET auth_at = '2000-01-01T00:00:00.000Z'")
    r = pk_client.delete(f"/api/auth/passkeys/{pk_id}")
    assert r.status_code == 403
    pk_client.post("/api/auth/reauth", json={"password": PASSWORD})
    assert pk_client.delete(f"/api/auth/passkeys/{pk_id}").status_code == 200
    pk_client.cookies.clear()
    r = pk_client.post("/api/auth/passkeys/login/begin", json={"origin": ORIGIN})
    assert r.status_code == 400  # no passkeys registered; password login still works
    assert pk_client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


def test_http_origin_blocks_enrollment(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", "http://192.168.1.10:8099")
    from webui.settings import reset_settings_for_tests, get_settings
    reset_settings_for_tests()
    from fastapi.testclient import TestClient
    from webui.main import create_app
    with TestClient(create_app(get_settings()), base_url="http://192.168.1.10:8099") as c:
        c.headers.update({"X-Requested-With": "fetch"})
        c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
        r = c.post("/api/auth/passkeys/register/begin", json={"origin": "http://192.168.1.10:8099"})
        assert r.status_code == 400 and "HTTPS" in r.json()["detail"]
