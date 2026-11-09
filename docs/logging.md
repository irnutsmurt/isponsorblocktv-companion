# Logging and diagnostics

## Architecture

```
code ──► CompanionLogger("component").info("event.name", "message", key=value)
           │  (stdlib logging; level checked first, nothing built if filtered)
           ▼
       QueueHandler ── RedactingFilter (secrets removed before serialization)
           │          RepeatSuppressor (identical WARNING+ within 60 s counted, not repeated)
           ▼
       QueueListener thread (the only writer; request handlers never block on I/O)
           ├─► stdout: JSON lines (default) or text            → Docker's logging driver
           ├─► in-memory ring buffer (5 000 records)           → Diagnostics log viewer, bundle
           └─► optional rotating file in /data/logs            → BudgetedRotatingFileHandler
```

Each record: `ts` (UTC, ms), `level`, `component`, `event` (stable name), `msg`, `request_id` (HTTP requests; also
returned in the `X-Request-ID` response header), plus structured fields such as `device_id`, `duration_ms`,
`retry`, `outcome`, `error`; `repeat` for aggregated records; `exc` for a redacted stack trace (unexpected
exceptions only, logged once at the boundary that handles them).

## Levels

| Level | Used for |
|---|---|
| DEBUG | Request timings, parser decisions, checkpoint commits, enrichment results, availability polls, passkey ceremony steps |
| INFO | Startup/shutdown, migrations, config writes/backups/restores, apply jobs, collection start/stop, device state changes, logins, session changes |
| WARNING | Source unavailable, failed logins, throttling, CSRF rejections, rate limits from SponsorBlock/YouTube, unlisted devices, degraded file logging |
| ERROR | Failed apply/restart, verification failures, upstream errors, unexpected exceptions in background tasks |
| CRITICAL | Database unavailable or migration failure (the app fails closed) |

Default `INFO`. Overrides:

* Deployment: `LOG_LEVEL=DEBUG`, per component `LOG_LEVEL_INGEST=DEBUG`, `LOG_LEVEL_AUTH=WARNING`
  (component names appear in every record; dots become `__`, e.g. `LOG_LEVEL_UPSTREAM__DOCKER`).
* Runtime: **Diagnostics → Companion log level**. *Enable debug mode* switches everything to DEBUG for
  `DEBUG_MODE_MINUTES` (default 30) and then returns to the previous level automatically; a banner shows when it ends.
  Per-component overrides can be time-limited too. Runtime overrides do not survive a restart.

iSponsorBlockTV's own level is separate: the companion needs only its default INFO output. Running iSponsorBlockTV with
`--debug` is not required and multiplies its log volume.

## What is never logged

Passwords, password hashes, session tokens/cookies, `Authorization` headers, recovery codes, setup tokens (one
deliberate exception below), WebAuthn challenges and private data, YouTube lounge tokens, screen IDs (replaced by
`[screen:<hash>]`), API keys, and request/response bodies. Redaction is key-based (any field whose name contains
password, token, secret, cookie, session, recovery, lounge, apikey, screen_id, …) and pattern-based (known secret
shapes in free text, plus every configured screen ID and API key registered at runtime). Tests assert this at DEBUG.

**Exception:** when no `SETUP_TOKEN` is configured, the generated one-time setup token is printed once per token on
stdout only, as a plain text box, so a NAS user can read it in the container log viewer. It is excluded from the ring
buffer, log files, the diagnostic bundle, and every other record, and it stops working when the account is created.

Device names, video IDs and titles are private viewing data. They appear in logs and the bundle; review before sharing.

## File logs (optional)

`LOG_FILE_ENABLED=true` writes JSON lines to `/data/logs/companion.log` (mode 0600, directory 0700).

| Setting | Default | Meaning |
|---|---|---|
| `LOG_FILE_MAX_BYTES` | `10MiB` | rotate when the active file would exceed this |
| `LOG_FILE_ROTATE_DAILY` | `true` | also rotate at the first write of a new UTC day — whichever comes first |
| `LOG_RETENTION_DAYS` | `7` | archives older than this are deleted |
| `LOG_DIR_BUDGET_BYTES` | `100MiB` | total for the directory, **including** the active file and in-progress `.gz.tmp`; oldest archives deleted first. Minimum 2× max bytes. |

Rotated files are gzip-compressed on a background thread (`companion-YYYYmmdd-HHMMSS-ffffff.log.gz`; readable with
`zcat`). Retention and budget are enforced after every rotation and hourly. If the directory is unwritable or the disk
is full, the handler marks itself degraded, writes one notice per minute to stderr, keeps logging to stdout, and the
problem shows in **Diagnostics** and `/api/health`. It never raises into the application or recurses.

File logs duplicate stdout, so they are off by default. **Docker owns stdout retention**: set `logging.options`
(`max-size`, `max-file`) on the companion service as in the compose example; the companion's own rotation does not
touch Docker's files.

## Viewing logs and collecting diagnostics (no terminal needed)

**Diagnostics** page:

* *Companion* log: filter by minimum level, component and text; live tail every 4 s with pause/resume; download as NDJSON.
* *iSponsorBlockTV* log: the collected, redacted upstream lines with level/type/text filters (kept separately from the activity history).
* *Diagnostic bundle*: a zip (≤ 8 MiB) with versions, health, capability table, collection gaps, the last 20 apply
  jobs, the audit trail, the last 3 000 companion and upstream log records, and optionally the configuration with
  screen IDs and API key removed. No database, cookies, passwords, tokens. Requires a recent password confirmation.
* *Recent administrative changes*: who changed what (field names, never values) and the result.

To diagnose a failed pairing: the companion log shows `pairing.failed` with the YouTube result code (`rejected`,
`timeout`, `network`, `upstream_error`). For an ingestion outage: `ingest.source.down` with the reason, then
`ingest.source.up` when it recovers; gaps are listed in the bundle.
