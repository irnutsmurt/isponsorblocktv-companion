"""Parser, ingestion, dedup, checkpoints, attribution and metrics (simulated with sanitized fixtures)."""
import ast
import asyncio
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES, SCREEN_A

UPSTREAM_SRC = Path("/home/rob/Documents/scripts/isponsorblocktv/src/iSponsorBlockTV/api_helpers.py")


def test_parser_classifies_and_redacts():
    from companion.upstream.parser import UpstreamLogParser
    p = UpstreamLogParser("UTC")
    lines = (FIXTURES / "upstream_2_11_sample.log").read_text().splitlines()
    parsed = [p.parse(l) for l in lines]
    kinds = [x.kind for x in parsed]
    assert kinds[:4] == ["auth_refreshed", "connected", "subscribing", "ad_mute"]
    assert "skip" in kinds and "ad_skip" in kinds and "subscribe_ended" in kinds and "auth_failed" in kinds
    assert "continuation" in kinds and "startup_default_categories" in kinds and "shutdown" in kinds
    assert parsed[lines.index(next(l for l in lines if "future version" in l))].kind == "other"
    for x in parsed:
        assert "SECRETTOKEN" not in x.redacted_line
        assert SCREEN_A not in x.redacted_line
    skip = next(x for x in parsed if x.kind == "skip")
    assert skip.fields["seek_to"] == 95.5 and skip.screen_key
    assert parsed[0].ts == datetime(2026, 11, 8, 20, 0, 0, 100000, tzinfo=timezone.utc)


def test_parser_applies_upstream_timezone():
    from companion.upstream.parser import UpstreamLogParser
    p = UpstreamLogParser("Europe/Madrid")
    x = p.parse("2026-07-01 12:00:00,000 - iSponsorBlockTV-abcdefgh12345678 - INFO - Ad has started, muting")
    assert x.ts == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _load_upstream_process_segments():
    tree = ast.parse(UPSTREAM_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "process_segments":
            node.decorator_list = []
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, str(UPSTREAM_SRC), "exec"), ns)
            return ns["process_segments"]
    raise AssertionError("process_segments not found upstream")


@pytest.mark.skipif(not UPSTREAM_SRC.exists(), reason="upstream source not present")
def test_merge_matches_upstream_process_segments():
    from companion.upstream.sponsorblock import merge_segments
    upstream = _load_upstream_process_segments()
    rng = random.Random(7)
    for _ in range(300):
        segs = []
        for i in range(rng.randint(0, 6)):
            s = round(rng.uniform(0, 600), 2)
            segs.append({"segment": [s, round(s + rng.uniform(0.2, 90), 2)], "UUID": f"u{i}", "category": rng.choice(["sponsor", "intro", "selfpromo"]), "locked": rng.randint(0, 1)})
        msl = rng.choice([0, 1, 5])
        expected, _ = upstream({"segments": json.loads(json.dumps(segs))}, msl)
        ours = merge_segments(json.loads(json.dumps(segs)), msl)
        assert [(round(e["start"], 6), round(e["end"], 6), sorted(e["UUID"])) for e in expected] == \
               [(round(o["start"], 6), round(o["end"], 6), sorted(o["uuids"])) for o in ours]


def test_overlapping_segments_not_double_counted():
    from companion.upstream.sponsorblock import match_seek, merge_segments
    segs = [{"segment": [10, 40], "UUID": "a", "category": "sponsor"}, {"segment": [30, 60], "UUID": "b", "category": "selfpromo"}]
    merged = merge_segments(segs, 1)
    assert len(merged) == 1 and merged[0]["start"] == 10 and merged[0]["end"] == 60
    assert set(merged[0]["categories"]) == {"sponsor", "selfpromo"}
    seg, conf = match_seek(merged, 60.0)
    assert conf == "exact" and seg["end"] - seg["start"] == 50  # 50 s, not 30+30


class Harness:
    def __init__(self, app):
        st = app.state
        self.st = st
        self.c = st.collector
        self.c._source_key = "test:fixture"
        self.c._run_id = "run1"

    async def feed(self, lines, base_ts="2026-11-08T20:00:00"):
        from companion.upstream.sources import RawLine
        last = "2026-11-08T19:59:59.000000000Z"
        for i, line in enumerate(lines):
            # Docker stamps every line (also print()/traceback lines); emulate with the last seen time + i ns
            if line[:4].isdigit():
                last = line[:23].replace(" ", "T").replace(",", ".") + "000000Z"
                ts = last
            else:
                ts = last[:-4] + f"{i:03d}Z"
            await self.c._handle_raw(RawLine(text=line, docker_ts=ts, stream="stderr", cursor=ts))
        await self.c._commit_checkpoint(force=True)


def _config(env, devices):
    (env["upstream"] / "config.json").write_text(json.dumps({"devices": devices, "skip_categories": ["sponsor"], "apikey": "", "custom_key": {"x": 1}}, indent=2))


def test_ingest_fixture_end_to_end(env, monkeypatch):
    _config(env, [{"screen_id": SCREEN_A, "name": "Living room", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    app = create_app(get_settings())
    with TestClient(app) as c:
        h = Harness(app)
        lines = (FIXTURES / "upstream_2_11_sample.log").read_text().splitlines()
        c.portal.call(h.feed, lines)
        db = app.state.db
        kinds = c.portal.call(db.fetchall, "SELECT kind, COUNT(*) n FROM events GROUP BY kind")
        counts = {r["kind"]: r["n"] for r in kinds}
        assert counts["skip"] == 3          # includes the legitimate repeat at 20:31
        assert counts["ad_mute"] == 1 and counts["ad_unmute"] == 1  # duplicate unmute collapsed
        assert counts["ad_skip"] == 1 and counts["connected"] == 1 and counts["loaded"] == 1 and counts["disconnected"] == 1
        assert counts["error"] == 1
        mute = c.portal.call(db.fetchone, "SELECT duration FROM events WHERE kind='ad_mute'")
        assert mute["duration"] == 15.0
        assert c.portal.call(db.scalar, "SELECT COUNT(*) FROM devices") == 1  # log device mapped to the configured one
        dev = c.portal.call(db.fetchone, "SELECT d.display_name, d.screen_name, s.state FROM devices d JOIN device_state s ON s.device_id=d.id")
        assert dev["display_name"] == "Living room" and dev["screen_name"] == "Living Room TV" and dev["state"] == "disconnected"
        logs = c.portal.call(db.fetchall, "SELECT message FROM upstream_log")
        assert all("SECRETTOKEN" not in r["message"] and SCREEN_A not in r["message"] for r in logs)
        # replaying the same stream (Docker 'since' overlap) must not duplicate anything
        c.portal.call(h.feed, lines)
        again = c.portal.call(db.scalar, "SELECT COUNT(*) FROM events WHERE kind='skip'")
        assert again == 3
        assert app.state.collector.status.duplicates_skipped >= len(lines) - 2
        cp = c.portal.call(db.fetchone, "SELECT * FROM ingest_checkpoint WHERE source='test:fixture'")
        assert cp and cp["cursor"].startswith("2026-11-08T20:40")


def test_checkpoint_survives_restart(env):
    _config(env, [{"screen_id": SCREEN_A, "name": "TV", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    lines = (FIXTURES / "upstream_2_11_sample.log").read_text().splitlines()
    app = create_app(get_settings())
    with TestClient(app) as c:
        c.portal.call(Harness(app).feed, lines[:9])
    app2 = create_app(get_settings())
    with TestClient(app2) as c:
        h = Harness(app2)
        c.portal.call(h.c._load_checkpoint)
        assert h.c._cursor.startswith("2026-11-08T20:02:00")
        c.portal.call(h.feed, lines)  # overlap replays the first 9; only new ones count
        assert c.portal.call(app2.state.db.scalar, "SELECT COUNT(*) FROM events WHERE kind='skip'") == 3


def test_attribution_with_sponsorblock(env):
    _config(env, [{"screen_id": SCREEN_A, "name": "TV", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    app = create_app(get_settings())
    with TestClient(app) as c:
        lines = (FIXTURES / "upstream_2_11_sample.log").read_text().splitlines()
        c.portal.call(Harness(app).feed, lines)

        async def fake_fetch(self, vid, cats):
            return [{"segment": [80.0, 95.5], "UUID": "u1", "category": "sponsor", "locked": 1, "votes": 3, "videoDuration": 212.0},
                    {"segment": [250.0, 290.0], "UUID": "u2", "category": "sponsor", "locked": 1, "votes": 3, "videoDuration": 212.0}]
        from companion.upstream import sponsorblock
        orig = sponsorblock.SponsorBlockClient.fetch_segments
        sponsorblock.SponsorBlockClient.fetch_segments = fake_fetch
        try:
            c.portal.call(app.state.enrichment._tick_sponsorblock, {"skip_categories": ["sponsor"], "minimum_skip_length": 1})
        finally:
            sponsorblock.SponsorBlockClient.fetch_segments = orig
        rows = c.portal.call(app.state.db.fetchall, "SELECT seek_to, match_confidence, segment_duration, categories FROM events WHERE kind='skip' ORDER BY id")
        assert [r["match_confidence"] for r in rows] == ["exact", "none", "exact"]
        assert rows[0]["segment_duration"] == 15.5 and json.loads(rows[0]["categories"]) == ["sponsor"]
        ov = c.get("/api/stats/overview?range=all")
        assert ov.status_code == 401  # still protected


def test_overview_metrics_and_timezone(env):
    _config(env, [{"screen_id": SCREEN_A, "name": "TV", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    from companion.stats.queries import range_bounds, resolve_tz
    from conftest import PASSWORD
    app = create_app(get_settings())
    with TestClient(app) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        c.post("/api/auth/setup/complete", json={"setup_token": "test-setup-token-123456", "username": "admin", "password": PASSWORD})
        db = app.state.db
        did = c.portal.call(db.scalar, "SELECT id FROM devices")
        # 2026-11-09T04:30Z is 23:30 on Nov 8 in New York
        for i, (at, dur) in enumerate([("2026-11-09T04:30:00.000Z", 30.0), ("2026-11-09T05:30:00.000Z", 20.0)]):
            ts = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
            c.portal.call(db.execute, "INSERT INTO events (kind, device_id, at, at_epoch, segment_duration, categories, match_confidence, dedup_key, created_at) VALUES ('skip', ?, ?, ?, ?, '[\"sponsor\"]', 'exact', ?, ?)", (did, at, ts, dur, f"k{i}", at))
        c.portal.call(db.execute, "INSERT INTO events (kind, device_id, at, at_epoch, duration, dedup_key, created_at) VALUES ('ad_mute', ?, '2026-11-09T05:00:00.000Z', ?, 12.0, 'm1', 'x')", (did, datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()))
        tz = resolve_tz("America/New_York")
        import functools
        r = c.portal.call(functools.partial(app.state.stats.trend, datetime(2026, 11, 8, 5, tzinfo=timezone.utc), datetime(2026, 11, 10, 5, tzinfo=timezone.utc), tz, bucket="day"))
        by = {b["bucket"]: b for b in r}
        assert by["2026-11-08"]["skips"] == 1 and by["2026-11-09"]["skips"] == 1
        ov = c.get("/api/stats/overview?range=custom&start=2000-01-01&end=2100-01-01").json()
        assert ov["skips"]["count"] == 2 and ov["skips"]["segment_duration_seconds"] == 50.0
        assert ov["ad_mutes"]["muted_seconds"] == 12.0  # reported separately; never added to segment time
        s, e, _ = range_bounds("today", None, None, tz)
        assert (e - s).total_seconds() in (23 * 3600, 24 * 3600, 25 * 3600)


def test_retention_rollup_preserves_totals(env, monkeypatch):
    monkeypatch.setenv("EVENT_RETENTION_DAYS", "7")
    from companion.settings import reset_settings_for_tests, get_settings
    reset_settings_for_tests()
    _config(env, [{"screen_id": SCREEN_A, "name": "TV", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    app = create_app(get_settings())
    with TestClient(app) as c:
        db = app.state.db
        did = c.portal.call(db.scalar, "SELECT id FROM devices")
        old = datetime(2020, 1, 5, 12, tzinfo=timezone.utc)
        c.portal.call(db.execute, "INSERT INTO events (kind, device_id, at, at_epoch, segment_duration, categories, match_confidence, dedup_key, created_at) VALUES ('skip', ?, ?, ?, 40, '[\"sponsor\",\"intro\"]', 'exact', 'old1', 'x')", (did, old.isoformat(), old.timestamp()))
        res = c.portal.call(app.state.maintenance.rollup_and_prune_events)
        assert res["rolled"] == 1
        assert c.portal.call(db.scalar, "SELECT COUNT(*) FROM events") == 0
        rows = c.portal.call(db.fetchall, "SELECT category, count, duration FROM daily_totals ORDER BY category")
        assert [(r["category"], r["count"], r["duration"]) for r in rows] == [("intro", 1, 20.0), ("sponsor", 1, 20.0)]


def test_file_source_rotation_and_truncation(tmp_path):
    from companion.upstream.sources import FileLogSource, SourceUnavailable
    f = tmp_path / "up.log"
    f.write_text("line1\nline2\n")

    async def run():
        src = FileLogSource(f, poll_interval=0.05)
        got = []
        gen = src.stream(None)
        got.append((await gen.__anext__()).text)
        got.append((await gen.__anext__()).text)
        cursor = None
        with open(f, "a") as fh:
            fh.write("line3\n")
        r = await gen.__anext__()
        got.append(r.text)
        cursor = r.cursor
        await gen.aclose()
        # resume from cursor: no duplicates
        gen = src.stream(cursor)
        with open(f, "a") as fh:
            fh.write("line4\n")
        got.append((await gen.__anext__()).text)
        # rotation: replace the file
        f.rename(tmp_path / "up.log.1")
        f.write_text("new1\n")
        with pytest.raises(SourceUnavailable):
            await asyncio.wait_for(gen.__anext__(), 2)
        gen = src.stream(cursor)  # inode changed → starts at 0
        got.append((await gen.__anext__()).text)
        return got
    assert asyncio.run(run()) == ["line1", "line2", "line3", "line4", "new1"]


def test_docker_demux():
    import struct
    from companion.upstream.docker_client import _demux

    class R:
        def __init__(self, data): self.data = data; self.pos = 0
        def at_eof(self): return self.pos >= len(self.data)
        async def readexactly(self, n):
            if self.pos + n > len(self.data):
                raise asyncio.IncompleteReadError(b"", n)
            b = self.data[self.pos:self.pos + n]; self.pos += n; return b

    def frame(t, payload): return bytes([t, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload
    data = frame(2, b"2026-11-08T20:00:00.000000001Z hello wor") + frame(2, b"ld\n2026-11-08T20:00:01.0Z second\n") + frame(1, b"out\n")

    async def run():
        return [x async for x in _demux(R(data))]
    assert asyncio.run(run()) == [("stderr", "2026-11-08T20:00:00.000000001Z hello world"), ("stderr", "2026-11-08T20:00:01.0Z second"), ("stdout", "out")]


def test_docker_since_format():
    from companion.upstream.sources import docker_since
    assert docker_since("2026-11-09T18:01:11.265812693Z") == "1794247271.265812693"
    assert docker_since("2026-11-09T18:01:11Z") == "1794247271.000000000"
    assert docker_since(None) is None
    assert docker_since("1793037671.5") == "1793037671.5"


def test_replay_after_upstream_restart_not_double_counted(env):
    """Docker returns the previous run's lines again after a container restart; they must dedupe."""
    _config(env, [{"screen_id": SCREEN_A, "name": "TV", "offset": 0}])
    from fastapi.testclient import TestClient
    from companion.main import create_app
    from companion.settings import get_settings
    lines = (FIXTURES / "upstream_2_11_sample.log").read_text().splitlines()
    app = create_app(get_settings())
    with TestClient(app) as c:
        h = Harness(app)
        h.c._dedup_scope = "docker:abc123"
        c.portal.call(h.feed, lines)
        # simulate: upstream restarted → new run id, then the companion replays from an older cursor
        h.c._run_id = "run2"
        h.c._run_started = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)
        c.portal.call(h.feed, lines)
        db = app.state.db
        assert c.portal.call(db.scalar, "SELECT COUNT(*) FROM events WHERE kind='skip'") == 3
        assert c.portal.call(db.scalar, "SELECT COUNT(*) FROM events WHERE kind='loaded'") == 1
