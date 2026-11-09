"""Ordered schema migrations. Never edit an applied migration; append a new one."""

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE app_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,                -- sha256(token)
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,           -- sliding (idle) expiry or remembered absolute expiry
        absolute_expires_at TEXT NOT NULL,
        remember INTEGER NOT NULL DEFAULT 0,
        auth_at TEXT NOT NULL,              -- last fresh password/passkey authentication
        auth_method TEXT NOT NULL,
        user_agent TEXT,
        device_label TEXT,
        ip TEXT,
        revoked_at TEXT
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);

    CREATE TABLE passkeys (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        credential_id BLOB NOT NULL UNIQUE,
        public_key BLOB NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        transports TEXT,
        name TEXT NOT NULL,
        aaguid TEXT,
        backup_eligible INTEGER,
        backed_up INTEGER,
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );

    CREATE TABLE webauthn_challenges (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,                 -- registration | authentication
        user_id INTEGER,
        challenge BLOB NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE recovery_codes (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        used_at TEXT
    );

    CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY,
        at REAL NOT NULL,
        ip TEXT,
        username TEXT,
        success INTEGER NOT NULL
    );
    CREATE INDEX idx_login_attempts_at ON login_attempts(at);

    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor_user_id INTEGER,
        actor_username TEXT,
        action TEXT NOT NULL,
        target TEXT,
        fields TEXT,                        -- JSON list of affected field names / summary (no values)
        result TEXT NOT NULL,
        request_id TEXT,
        ip TEXT
    );
    CREATE INDEX idx_audit_at ON audit_log(at);

    -- ---------------- upstream devices and activity ----------------
    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        screen_key TEXT NOT NULL UNIQUE,    -- sha256(screen_id)[:16]; the only identifier stored
        screen_hint TEXT,                   -- last 4 chars of screen_id, for correlating with upstream's TUI
        screen_id_length INTEGER,
        config_name TEXT,                   -- name in config.json
        screen_name TEXT,                   -- name YouTube reports ("Connected to device <screen_name>")
        display_name TEXT NOT NULL,
        configured INTEGER NOT NULL DEFAULT 0,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT,
        removed_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE device_state (
        device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'unknown',   -- configured|loaded|connected|playing|disconnected|unknown
        state_since TEXT,
        state_evidence TEXT,
        last_log_at TEXT,
        last_connected_at TEXT,
        last_disconnected_at TEXT,
        last_loaded_at TEXT,
        current_video_id TEXT,
        current_video_since TEXT,
        yt_availability TEXT,               -- online|offline|unknown|error
        yt_availability_at TEXT,
        yt_availability_error TEXT,
        last_error TEXT,
        last_error_at TEXT,
        upstream_run_id TEXT                -- run during which the device was last loaded
    );

    CREATE TABLE playbacks (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        segments_detected INTEGER,
        state_changes INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX idx_playbacks_device_time ON playbacks(device_id, started_at);
    CREATE INDEX idx_playbacks_video ON playbacks(video_id);

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                 -- skip|ad_skip|ad_mute|ad_unmute|connected|disconnected|loaded|error|warning
        device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
        at TEXT NOT NULL,
        at_epoch REAL NOT NULL,
        video_id TEXT,
        playback_id INTEGER,
        seek_to REAL,
        segment_start REAL,
        segment_end REAL,
        segment_duration REAL,
        categories TEXT,                    -- JSON list
        segment_uuids TEXT,                 -- JSON list
        match_confidence TEXT,              -- exact|nearest|none|pending|unavailable
        outcome TEXT NOT NULL DEFAULT 'requested',
        paired_event_id INTEGER,            -- ad_unmute -> ad_mute
        duration REAL,                      -- ad mute duration (seconds) when paired
        details TEXT,                       -- JSON
        dedup_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        enriched_at TEXT
    );
    CREATE INDEX idx_events_time ON events(at_epoch);
    CREATE INDEX idx_events_kind_time ON events(kind, at_epoch);
    CREATE INDEX idx_events_device_time ON events(device_id, at_epoch);
    CREATE INDEX idx_events_video ON events(video_id);

    CREATE TABLE videos (
        video_id TEXT PRIMARY KEY,
        title TEXT,
        channel_key TEXT,
        channel_id TEXT,
        channel_name TEXT,
        channel_url TEXT,
        duration REAL,
        meta_status TEXT NOT NULL DEFAULT 'pending',    -- pending|ok|not_found|failed|disabled
        meta_attempts INTEGER NOT NULL DEFAULT 0,
        meta_next_attempt_at REAL,
        meta_fetched_at TEXT,
        meta_source TEXT,
        sb_status TEXT NOT NULL DEFAULT 'pending',      -- pending|ok|none|failed
        sb_attempts INTEGER NOT NULL DEFAULT 0,
        sb_next_attempt_at REAL,
        sb_fetched_at TEXT,
        sb_segments TEXT,                               -- JSON raw segments (category, start, end, UUID, locked, votes)
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );
    CREATE INDEX idx_videos_channel ON videos(channel_key);

    CREATE TABLE upstream_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        at_epoch REAL NOT NULL,
        level TEXT,
        logger TEXT,                        -- redacted logger name
        device_id INTEGER,
        message TEXT NOT NULL,              -- redacted
        parsed TEXT,                        -- parser classification
        stream TEXT,
        run_id TEXT,
        dedup_key TEXT NOT NULL UNIQUE
    );
    CREATE INDEX idx_upstream_log_time ON upstream_log(at_epoch);
    CREATE INDEX idx_upstream_log_device ON upstream_log(device_id, at_epoch);

    CREATE TABLE ingest_checkpoint (
        source TEXT PRIMARY KEY,            -- docker:<container-id> | file:<path>
        cursor TEXT NOT NULL,               -- docker: RFC3339 ns timestamp; file: "<inode>:<offset>"
        last_line_hash TEXT,
        lines_processed INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        details TEXT
    );

    CREATE TABLE collection_gaps (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        reason TEXT NOT NULL
    );

    CREATE TABLE upstream_runs (
        run_id TEXT PRIMARY KEY,            -- container id + started-at, or file-mode startup marker
        started_at TEXT NOT NULL,
        ended_at TEXT,
        container_id TEXT,
        image TEXT,
        version TEXT,
        exit_reason TEXT
    );

    CREATE TABLE daily_totals (
        day TEXT NOT NULL,                  -- UTC date YYYY-MM-DD
        device_id INTEGER,
        kind TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        count INTEGER NOT NULL DEFAULT 0,
        duration REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (day, device_id, kind, category)
    );

    CREATE TABLE config_backups (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        filename TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        reason TEXT NOT NULL,
        actor TEXT
    );

    CREATE TABLE apply_jobs (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        config_sha256 TEXT,
        status TEXT NOT NULL,               -- pending|saved|restarting|verifying|applied|failed|manual_restart_required
        steps TEXT,                         -- JSON list of {name, status, detail, at}
        error TEXT,
        finished_at TEXT,
        actor TEXT
    );
    """),
]
