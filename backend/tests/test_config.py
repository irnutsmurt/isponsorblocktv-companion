"""Config store: validation, atomic writes, conflicts, unknown-key preservation, backups, restore."""
import json
import os

import pytest

from conftest import PASSWORD, SCREEN_A

SCREEN_B = "SCREEN" + "B" * 58


def write(env, data):
    (env["upstream"] / "config.json").write_text(json.dumps(data, indent=2))


@pytest.fixture
def authed(client, admin):
    return client


def test_validation_mirrors_upstream():
    from companion.upstream.config_schema import validate_config
    assert not validate_config({"devices": []}).ok
    assert not validate_config({"devices": [{"screen_id": ""}]}).ok
    assert not validate_config({"devices": [{"screen_id": "x" * 64}], "channel_whitelist": [{"id": "UC1"}], "apikey": ""}).ok
    assert not validate_config({"devices": [{"screen_id": "x" * 64}], "atvs": []}).ok
    assert not validate_config({"devices": [{"screen_id": "x" * 64}, {"screen_id": "x" * 64}]}).ok
    r = validate_config({"devices": [{"screen_id": "x" * 26}]})
    assert r.ok and r.warnings


def test_save_preserves_unknown_and_other_devices(env, authed):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A", "offset": 0, "custom": 1}, {"screen_id": SCREEN_B, "name": "B", "offset": 0}],
                "skip_categories": ["sponsor"], "future_key": {"keep": True}, "apikey": "AIzaSECRET"})
    cfg = authed.get("/api/config").json()
    assert "AIzaSECRET" not in json.dumps(cfg) and SCREEN_A not in json.dumps(cfg)
    form = {"skip_categories": ["sponsor", "intro"], "apikey": "[REDACTED]", "devices": [{"index": 1, "name": "B renamed", "offset": 50}]}
    r = authed.put("/api/config", json={"config": form, "expected_etag": cfg["etag"], "apply": False})
    assert r.status_code == 200, r.text
    import time; time.sleep(0.5)
    data = json.loads((env["upstream"] / "config.json").read_text())
    assert data["future_key"] == {"keep": True} and data["apikey"] == "AIzaSECRET"
    assert data["devices"][0] == {"screen_id": SCREEN_A, "name": "A", "offset": 0, "custom": 1}
    assert data["devices"][1]["name"] == "B renamed" and data["devices"][1]["screen_id"] == SCREEN_B
    assert data["skip_categories"] == ["sponsor", "intro"]
    job = authed.get("/api/config/jobs/latest").json()["job"]
    assert job["status"] == "saved" and job["steps"][-1]["name"] == "apply" and job["steps"][-1]["status"] == "skipped"
    assert authed.get("/api/config/backups").json()["backups"]


def test_external_change_detected(env, authed):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A", "offset": 0}]})
    etag = authed.get("/api/config").json()["etag"]
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A edited elsewhere", "offset": 0}]})
    r = authed.put("/api/config", json={"config": {"mute_ads": True}, "expected_etag": etag, "apply": False})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"
    data = json.loads((env["upstream"] / "config.json").read_text())
    assert data["devices"][0]["name"] == "A edited elsewhere"


def test_malformed_config_never_replaced(env, authed):
    (env["upstream"] / "config.json").write_text("{ not json")
    cfg = authed.get("/api/config").json()
    assert cfg["parse_error"]
    r = authed.put("/api/config", json={"config": {"mute_ads": True}, "expected_etag": cfg["etag"], "apply": False})
    assert r.status_code == 409
    assert (env["upstream"] / "config.json").read_text() == "{ not json"


def test_invalid_result_rejected(env, authed):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A", "offset": 0}]})
    etag = authed.get("/api/config").json()["etag"]
    r = authed.put("/api/config", json={"config": {"channel_whitelist": [{"id": "UCabc", "name": "x"}], "apikey": ""}, "expected_etag": etag, "apply": False})
    assert r.status_code == 422
    assert any(e["path"] == "channel_whitelist" for e in r.json()["detail"]["errors"])


def test_write_permission_failure_reported(env, authed):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A", "offset": 0}]})
    etag = authed.get("/api/config").json()["etag"]
    os.chmod(env["upstream"], 0o500)
    try:
        if os.access(env["upstream"], os.W_OK):
            pytest.skip("running as root; permission test not meaningful")
        r = authed.put("/api/config", json={"config": {"mute_ads": True}, "expected_etag": etag, "apply": False})
        assert r.status_code == 200
        import time; time.sleep(0.5)
        job = authed.get("/api/config/jobs/latest").json()["job"]
        assert job["status"] == "failed" and json.loads(job["error"])["code"] == "permission"
        assert authed.get("/api/config").json()["writable"] is False
    finally:
        os.chmod(env["upstream"], 0o700)


def test_atomic_write_preserves_mode(env, authed):
    p = env["upstream"] / "config.json"
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "A", "offset": 0}]})
    os.chmod(p, 0o640)
    etag = authed.get("/api/config").json()["etag"]
    authed.put("/api/config", json={"config": {"mute_ads": True}, "expected_etag": etag, "apply": False})
    import time; time.sleep(0.5)
    assert oct(os.stat(p).st_mode & 0o777) == "0o640"
    assert not [x for x in os.listdir(env["upstream"]) if x.endswith(".tmp")]


def test_backup_bound_and_restore(env, authed):
    store = authed.app.state.config_store
    store.keep = 3
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "v0", "offset": 0}]})
    for i in range(1, 6):
        etag = authed.get("/api/config").json()["etag"]
        authed.put("/api/config", json={"config": {"devices": [{"index": 0, "name": f"v{i}"}]}, "expected_etag": etag, "apply": False})
        import time; time.sleep(0.4)
    backups = authed.get("/api/config/backups").json()["backups"]
    assert len(backups) == 3
    assert len(list((env["data"] / "config-backups").glob("*.json"))) == 3
    oldest = backups[-1]
    r = authed.post(f"/api/config/backups/{oldest['id']}/restore?apply=false")
    assert r.status_code == 200
    import time; time.sleep(0.5)
    assert json.loads((env["upstream"] / "config.json").read_text())["devices"][0]["name"] == "v2"


def test_add_device_flow_without_screen_id_exposure(env, authed, monkeypatch):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "First", "offset": 0}], "skip_categories": ["sponsor"]})
    from companion.upstream.youtube import PairedScreen, YouTubeClient

    async def fake_pair(self, code):
        return PairedScreen(screen_id=SCREEN_B, name="Bedroom TV")

    async def fake_avail(self, sid):
        return "online"
    monkeypatch.setattr(YouTubeClient, "pair_with_code", fake_pair)
    monkeypatch.setattr(YouTubeClient, "screen_availability", fake_avail)
    authed.portal.call(authed.app.state.collector.on_config_changed, json.loads((env["upstream"] / "config.json").read_text()))
    v = authed.post("/api/pairing/verify", json={"code": "1234 5678 9012"})
    assert v.status_code == 200 and SCREEN_B not in v.text and v.json()["screen_name"] == "Bedroom TV"
    etag = authed.get("/api/config").json()["etag"]
    r = authed.post("/api/devices", json={"ticket": v.json()["ticket"], "name": "Bedroom", "apply": False, "expected_etag": etag})
    assert r.status_code == 200, r.text
    import time; time.sleep(0.5)
    data = json.loads((env["upstream"] / "config.json").read_text())
    assert [d["name"] for d in data["devices"]] == ["First", "Bedroom"]
    assert data["devices"][0]["screen_id"] == SCREEN_A
    # duplicate pairing of the same TV is blocked
    v2 = authed.post("/api/pairing/verify", json={"code": "123456789012"}).json()
    assert v2["duplicate_of"]["configured"] is True
    etag = authed.get("/api/config").json()["etag"]
    d = authed.post("/api/devices", json={"ticket": v2["ticket"], "name": "Again", "apply": False, "expected_etag": etag})
    assert d.status_code == 409 and d.json()["detail"]["code"] == "duplicate"
    # ticket single use
    again = authed.post("/api/devices", json={"ticket": v.json()["ticket"], "name": "x", "apply": False})
    assert again.status_code == 410


def test_invalid_pairing_code_rejected(authed):
    assert authed.post("/api/pairing/verify", json={"code": "12345"}).status_code == 422
    assert authed.post("/api/pairing/verify", json={"code": "abcd efgh ijkl"}).status_code == 422


def test_rejected_pairing_code(authed, monkeypatch):
    from companion.upstream.youtube import PairingError, YouTubeClient

    async def reject(self, code):
        raise PairingError("YouTube did not recognise this code.", "rejected")
    monkeypatch.setattr(YouTubeClient, "pair_with_code", reject)
    r = authed.post("/api/pairing/verify", json={"code": "123456789012"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "rejected"


def test_cannot_remove_last_device(env, authed):
    write(env, {"devices": [{"screen_id": SCREEN_A, "name": "Only", "offset": 0}]})
    authed.portal.call(authed.app.state.collector.on_config_changed, json.loads((env["upstream"] / "config.json").read_text()))
    did = authed.get("/api/devices").json()["devices"][0]["id"]
    r = authed.post(f"/api/devices/{did}/remove", json={"apply": False})
    assert r.status_code == 200
    import time; time.sleep(0.5)
    job = authed.get("/api/config/jobs/latest").json()["job"]
    assert job["status"] == "failed"
    assert json.loads((env["upstream"] / "config.json").read_text())["devices"][0]["name"] == "Only"
