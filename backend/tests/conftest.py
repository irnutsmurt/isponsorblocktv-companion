import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"
SCREEN_A = "SCREEN" + "A" * 58


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("UPSTREAM_DATA_DIR", str(upstream))
    monkeypatch.setenv("LOG_SOURCE", "none")
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token-123456")
    monkeypatch.setenv("ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("ARGON2_TIME_COST", "1")
    monkeypatch.setenv("AVAILABILITY_POLL_MINUTES", "0")
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    from companion.settings import reset_settings_for_tests
    reset_settings_for_tests()
    from companion.logging import setup_logging
    setup_logging(level="DEBUG", fmt="json", stream=open(os.devnull, "w"))
    return {"data": data, "upstream": upstream, "tmp": tmp_path}


@pytest.fixture
def app(env):
    from companion.main import create_app
    from companion.settings import get_settings
    return create_app(get_settings())


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://testserver") as c:
        c.headers.update({"X-Requested-With": "fetch"})
        yield c


PASSWORD = "correct horse battery staple"


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()
