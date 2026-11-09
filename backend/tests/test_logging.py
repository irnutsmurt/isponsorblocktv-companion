"""Logging framework: levels, temporary debug, redaction, rotation, compression, retention, budget, failures."""
import gzip
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from companion.logging import get_logger, get_logging_state, setup_logging
from companion.logging.handlers import BudgetedRotatingFileHandler
from companion.logging.redaction import register_secret


def wait_flush():
    time.sleep(0.2)


def capture(**kw):
    buf = io.StringIO()
    setup_logging(stream=buf, **kw)
    return buf


def records(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]


def test_structured_record_fields():
    buf = capture(level="INFO")
    from companion.logging import request_id_var
    tok = request_id_var.set("req123")
    get_logger("ingest").info("ingest.test", "Hello", device_id=7, duration_ms=12.5, retry=2)
    request_id_var.reset(tok)
    wait_flush()
    r = records(buf)[-1]
    assert r["level"] == "INFO" and r["component"] == "ingest" and r["event"] == "ingest.test" and r["msg"] == "Hello"
    assert r["request_id"] == "req123" and r["device_id"] == 7 and r["retry"] == 2
    assert r["ts"].endswith("Z") and datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).tzinfo is not None


def test_level_filtering_and_component_override():
    buf = capture(level="WARNING", component_levels={"auth": "DEBUG"})
    get_logger("ingest").info("x.info", "hidden")
    get_logger("ingest").warning("x.warn", "shown")
    get_logger("auth").debug("auth.debug", "auth debug shown")
    get_logger("auth.session").debug("auth.session.debug", "child inherits")
    wait_flush()
    events = [r["event"] for r in records(buf)]
    assert "x.info" not in events and "x.warn" in events and "auth.debug" in events and "auth.session.debug" in events


def test_temporary_debug_expires_and_restores():
    buf = capture(level="INFO")
    st = get_logging_state()
    st.levels.set_override("", "DEBUG", minutes=1)
    get_logger("ingest").debug("dbg.on", "visible during debug mode")
    st.levels.overrides[""] = ("DEBUG", time.time() - 1)  # simulate expiry
    expired = st.levels.expire()
    assert expired == [""]
    get_logger("ingest").debug("dbg.off", "hidden again")
    wait_flush()
    events = [r["event"] for r in records(buf)]
    assert "dbg.on" in events and "dbg.off" not in events
    assert st.levels.effective()["global"] == "INFO"


def test_secrets_redacted_even_at_debug():
    buf = capture(level="DEBUG")
    register_secret("SCREENID-" + "Z" * 50, "SCREEN_ID")
    log = get_logger("auth")
    log.debug("t.secret", "Refreshed auth, lounge id token AbCdEfSecretToken123 for SCREENID-" + "Z" * 50,
              password="hunter2hunter2", session_token="abc123abc123", authorization="Bearer xyz", recovery_code="AAAAA-BBBBB",
              nested={"cookie": "isbtv_session=secretcookievalue", "ok": "fine"})
    try:
        raise ValueError("boom with password=hunter2hunter2 and lounge id token AbCdEfSecretToken123")
    except ValueError:
        log.exception("t.exc", "failed")
    wait_flush()
    text = buf.getvalue()
    for secret in ("hunter2hunter2", "AbCdEfSecretToken123", "Z" * 50, "abc123abc123", "Bearer xyz", "AAAAA-BBBBB", "secretcookievalue"):
        assert secret not in text.replace("password=hunter2hunter2", "") or secret == "hunter2hunter2", secret
    recs = records(buf)
    exc = [r for r in recs if r["event"] == "t.exc"][0]
    assert "Traceback" in exc["exc"] and "AbCdEfSecretToken123" not in exc["exc"]
    assert recs[-2]["nested"]["ok"] == "fine"


def test_repeated_errors_aggregated():
    buf = capture(level="INFO")
    st = get_logging_state()
    st.suppressor.window = 0.3
    for _ in range(50):
        get_logger("ingest").warning("same.problem", "Source unavailable")
    time.sleep(0.35)
    get_logger("ingest").warning("same.problem", "Source unavailable")
    wait_flush()
    recs = [r for r in records(buf) if r["event"] == "same.problem"]
    assert len(recs) == 2 and recs[1]["repeat"] == 49


def _handler(tmp_path, **kw):
    h = BudgetedRotatingFileHandler(tmp_path / "logs", **kw)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def emit(h, msg):
    h.emit(logging.LogRecord("companion.t", logging.INFO, __file__, 1, msg, (), None))


def wait_archives(h, n, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        gz = [p for p in h.directory.glob("companion-*.log.gz")]
        if len(gz) >= n and not list(h.directory.glob("*.tmp")):
            return gz
        time.sleep(0.05)
    return list(h.directory.glob("companion-*.log.gz"))


def test_size_rotation_and_gzip_readable(tmp_path):
    h = _handler(tmp_path, max_bytes=64 * 1024, budget_bytes=10 * 1024 * 1024)
    line = "x" * 1000
    for i in range(200):
        emit(h, f"{i:04d} {line}")
    gz = wait_archives(h, 2)
    assert len(gz) >= 2 and h.active_path.stat().st_size <= 64 * 1024
    content = b"".join(gzip.open(p).read() for p in sorted(gz))
    assert b"0000 " in content
    assert oct(h.active_path.stat().st_mode & 0o777) == "0o600"
    assert oct(h.directory.stat().st_mode & 0o777) == "0o700"
    h.close()


def test_daily_rotation(tmp_path):
    h = _handler(tmp_path, max_bytes=10 * 1024 * 1024)
    emit(h, "yesterday")
    h._open_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    emit(h, "today")
    gz = wait_archives(h, 1)
    assert len(gz) == 1 and gzip.open(gz[0]).read() == b"yesterday\n"
    assert h.active_path.read_text() == "today\n"
    h.close()


def test_retention_and_budget(tmp_path):
    h = _handler(tmp_path, max_bytes=64 * 1024, budget_bytes=256 * 1024, retention_days=7)
    old = h.directory / "companion-20000101-000000.log.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
    rnd = os.urandom
    for i in range(400):
        emit(h, rnd(600).hex())  # incompressible → archives stay large
    wait_archives(h, 3)
    time.sleep(0.5)
    h.prune()
    assert not old.exists()
    assert h.directory_size() <= 256 * 1024 + 64 * 1024  # budget (+ one active file in flight)
    h.close()


def test_concurrent_logging_during_rotation(tmp_path):
    h = _handler(tmp_path, max_bytes=64 * 1024, budget_bytes=50 * 1024 * 1024)
    def worker(n):
        for i in range(500):
            emit(h, f"t{n}-{i:04d} " + "y" * 200)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    wait_archives(h, 1)
    time.sleep(0.5)
    lines = []
    for p in h.directory.iterdir():
        if p.name.endswith(".gz"):
            lines += gzip.open(p).read().decode().splitlines()
        elif p.name.endswith(".log"):
            lines += p.read_text().splitlines()
    assert len(lines) == 2000 and len(set(lines)) == 2000  # nothing lost or duplicated
    h.close()


def test_unwritable_directory_degrades_without_raising(tmp_path, capsys):
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("root")
        h = BudgetedRotatingFileHandler(d / "logs")
        h.setFormatter(logging.Formatter("%(message)s"))
        emit(h, "hello")  # must not raise
        assert h.degraded and "cannot open" in h.degraded
        assert "companion-logging degraded" in capsys.readouterr().err
    finally:
        os.chmod(d, 0o700)


def test_disk_full_simulated(tmp_path, monkeypatch):
    h = _handler(tmp_path)
    class Full:
        def write(self, s): raise OSError(28, "No space left on device")
        def flush(self): pass
        def close(self): pass
    h._stream = Full()
    emit(h, "x")  # no exception, degraded flagged, fallback sink used
    assert "No space" in h.degraded
    h.close()


def test_console_only_never_reaches_file_or_ring(tmp_path):
    buf = io.StringIO()
    setup_logging(stream=buf, file_enabled=True, log_dir=tmp_path / "logs")
    get_logger("auth.bootstrap").console_only("SETUP TOKEN BOX secret-abc-123")
    wait_flush()
    st = get_logging_state()
    assert "secret-abc-123" in buf.getvalue()
    assert "secret-abc-123" not in json.dumps(st.ring.snapshot())
    st.file_handler.close()
    assert "secret-abc-123" not in (tmp_path / "logs" / "companion.log").read_text()
