# Configuration reference (environment variables)

All optional. Defaults are safe for a home NAS.

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `/data` | Web UI data (database, backups, logs, TLS) |
| `UPSTREAM_DATA_DIR` | `/upstream-data` | iSponsorBlockTV's data folder (`config.json`) |
| `UPSTREAM_CONFIG_FILE` | `$UPSTREAM_DATA_DIR/config.json` | Override the config path |
| `PORT` / `HOST` | `8099` / `0.0.0.0` | Listener |
| `SETUP_TOKEN` | — | Fixed bootstrap token; otherwise one is generated and printed |
| `SETUP_TOKEN_TTL_MINUTES` | `60` | Lifetime of a generated token |
| `PUBLIC_ORIGIN` | — | Exact URL users open, e.g. `https://isbtv.example.home`. Needed for passkeys and accepted as a request origin. Can also be set in the UI. |
| `REQUIRE_HTTPS` | `false` | Refuse sign-in/setup/recovery over HTTP |
| `COOKIE_SECURE` | `auto` | `auto` follows the detected scheme (works behind TLS proxies); `true`/`false` to force |
| `TRUSTED_PROXIES` | — | Optional. Proxies whose `X-Forwarded-*` headers are honoured (real client IPs). Proxies can also be trusted with one click on the Access page. Not needed for HTTPS detection. |
| `TLS_SELF_SIGNED` | `false` | Serve HTTPS with a generated certificate |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | Serve HTTPS with your certificate |
| `MAX_REQUEST_BYTES` | `256KiB` | Request body limit |
| `SESSION_IDLE_HOURS` / `SESSION_ABSOLUTE_DAYS` / `REMEMBER_DAYS` | `24` / `7` / `30` | Session lifetimes |
| `REAUTH_MINUTES` | `10` | Window for sensitive actions after entering the password |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_KIB` / `ARGON2_PARALLELISM` | `3` / `65536` / `4` | Password hashing cost |
| `LOG_SOURCE` | `auto` | `docker`, `file`, `none`; `auto` uses the file if it exists, else Docker |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Use `tcp://docker-gate:2375` with the gate |
| `GATE_TOKEN` | — | Shared secret sent to the gate (set the same value on the gate) |
| `UPSTREAM_CONTAINER` | `iSponsorBlockTV` | iSponsorBlockTV container name |
| `UPSTREAM_LOG_FILE` | — | File mode: path of iSponsorBlockTV's output file |
| `UPSTREAM_TZ` | `UTC` | Timezone of timestamps inside iSponsorBlockTV's log lines (file mode) |
| `ALLOW_RESTART` | `true` | Offer “Save and apply” when the gate permits restarts |
| `AVAILABILITY_POLL_MINUTES` | `10` | Ask YouTube whether each TV's app is online; `0` disables |
| `ENRICHMENT_ENABLED` | `true` | Look up SponsorBlock segments, titles and channels |
| `YOUTUBE_API_KEY` | — | Optional, adds channel IDs (also settable in the UI) |
| `CONFIG_BACKUPS_KEEP` | `20` | Number of `config.json` backups kept |
| `EVENT_RETENTION_DAYS` | `365` | Detailed history before roll-up |
| `UPSTREAM_LOG_RETENTION_DAYS` / `UPSTREAM_LOG_MAX_ROWS` | `14` / `200000` | Stored upstream log lines |
| `LOG_LEVEL`, `LOG_LEVEL_<COMPONENT>` | `INFO` | Web UI log levels |
| `LOG_FORMAT` | `json` | `json` or `text` on stdout |
| `LOG_FILE_ENABLED`, `LOG_FILE_MAX_BYTES`, `LOG_FILE_ROTATE_DAILY`, `LOG_RETENTION_DAYS`, `LOG_DIR_BUDGET_BYTES` | off, 10MiB, true, 7, 100MiB | See logging.md |
| `LOG_RING_SIZE` | `5000` | Records kept in memory for the viewer |
| `DEBUG_MODE_MINUTES` | `30` | Length of temporary debug mode |
| `YOUTUBE_LOUNGE_API_BASE` | YouTube | **Tests only**: points pairing at a fake server |

Gate (`python -m webui.dockerproxy`): `GATE_TARGET_CONTAINER`, `GATE_ALLOW_RESTART` (`true`), `GATE_TOKEN`,
`GATE_PORT` (`2375`), `GATE_DOCKER_SOCKET` (`/var/run/docker.sock`).
