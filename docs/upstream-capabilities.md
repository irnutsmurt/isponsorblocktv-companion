# Upstream integration audit: iSponsorBlockTV

Audited source: iSponsorBlockTV **2.11.0** (`pyproject.toml`), with `pyytlounge==3.6.0`.
Release line covered by this webui: **2.x** (log messages used below exist unchanged in
the 2.11.0 source; 2.5 → 2.11 are expected to parse but only 2.11.0 was exercised live).

## What upstream actually exposes

| Interface | Exists? | Notes |
|---|---|---|
| REST / HTTP API | **No** | `src/iSponsorBlockTV/main.py` runs a plain asyncio loop; no server. |
| Structured event stream / IPC | **No** | Nothing is written except Python `logging` to stderr/stdout and a few `print()` calls. |
| Config hot reload | **No** | `helpers.Config.__load()` runs once at startup; `main_async()` copies values into `ApiHelper` and one `DeviceListener` per device. A **process restart** is the only way to apply changes. |
| Non-interactive configurator | **No** | `setup` (Textual TUI) and `setup-cli` (prompts) are interactive. The web UI writes `config.json` directly using the schema in `helpers.Config`. |
| Pairing endpoint | **Yes (YouTube)** | `api_helpers.ApiHelper.pair_with_code` → `POST https://www.youtube.com/api/lounge/pairing/get_screen` with `pairing_code` (12 digits, dashes/spaces stripped). Returns `screen.screenId`, `screen.name`, `screen.loungeToken`. Only `screen_id` and `name` are persisted. |
| Screen status endpoint | **Yes (YouTube)** | `pyytlounge.wrapper.refresh_auth` → `pairing/get_lounge_token_batch` (screen_id → lounge token), then `is_available` → `pairing/get_screen_availability` (token → `online`/`offline`). These are status calls, not a lounge session. |
| Graceful stop | **Yes** | `SIGTERM`/`SIGINT` → `KeyboardInterrupt` → `finish()` disconnects every device. `docker restart` is safe. |
| Logging format | stdout/stderr, text | `logging.StreamHandler` with `%(asctime)s - %(name)s - %(levelname)s - %(message)s`. Level INFO by default, DEBUG with `--debug`. Logger name per device is `iSponsorBlockTV-<screen_id>`. |

Because the only runtime signal is the log stream, the web UI's integration is a
**version-aware log adapter** (`backend/webui/upstream/parser.py`) fed by either the
Docker logs API or a log file.

## Capability table

Legend — **Observed**: read directly from an upstream record. **Inferred**: derived by the
web UI from observed records plus its own lookups. **Unavailable**: upstream does not
expose it.

| Capability | Status | Source (upstream file) | Requirements | Reliability / caveats |
|---|---|---|---|---|
| Configured devices and stable identifiers | **Observed** | `config.json` → `devices[].screen_id`, `name`, `offset` (`helpers.py` `Device`, `Config`) | Read access to the upstream data directory | `screen_id` is stable across renames and is the identity the web UI uses (stored hashed-prefixed, never shown in full). 26-char IDs are the legacy format YouTube is revoking (`Device.__validate`). |
| Device loaded by upstream at startup | **Observed** (weak) | pyytlounge `refresh_auth` logs `Refreshed auth, lounge id token …` through the device logger (`wrapper.py`) | INFO | Emitted once per device shortly after start when `get_lounge_token_batch` succeeds. Confirms upstream parsed the config entry and YouTube issued a lounge token. **Live finding:** YouTube issues a token even for a made-up 64-character screen ID, so this does not prove the TV exists; the optional availability poll (`unknown_screen`) does. The line contains the lounge token → **redacted** before storage. |
| Connection status | **Observed (edges only)** | `main.py` `Connected to device %s (%s)` (connect); pyytlounge `Subscribe completed, status …` (subscription ended) | INFO | "Waiting for device to be available/connected" are DEBUG only. Between edges the web UI shows the last known state and its timestamp; silence is **not** treated as offline. Optional YouTube availability poll (`get_screen_availability`) adds an independent `online/offline` observation. |
| Last activity | **Observed** | Timestamp of the most recent record for the device logger | INFO | Docker log timestamps (UTC, ns) are authoritative; in file mode `asctime` is parsed with the configured upstream timezone. |
| Current video ID | **Observed** | `main.py` `Playing video %s with %d segments` | INFO | Fires on every playback-state change while playing (also after pause/seek). Deduplicated into one "playback" per (device, video) run. |
| Video title / channel ID / channel name | **Inferred (optional enrichment)** | Not logged by upstream (upstream only fetches channel IDs when `apikey` + whitelist are set, and never logs them) | Web-UI-side lookups: YouTube oEmbed (no key; title, channel name, channel URL) and optionally YouTube Data API v3 (`channelId`) when the user provides a key | Cached, rate-limited, retried later on failure; missing metadata is displayed as "unknown", never invented. |
| SponsorBlock segments detected | **Observed (count only)** | `Playing video %s with %d segments` → `%d` is the merged, min-length-filtered count (`api_helpers.process_segments`) | INFO | Count after upstream's overlap merging. Boundaries not logged. |
| Segment boundaries and category | **Inferred** | `main.py` `Skipping segment: seeking to %s` logs only the seek target (segment **end**). The web UI queries the SponsorBlock API for the same video with the configured categories, applies the same merge rules as `process_segments`, and matches the merged segment whose end equals the logged position (±0.5 s) | SponsorBlock reachable from the web UI; same `skip_categories`, `minimum_skip_length`, `sponsorblock_api_url` as upstream | Match confidence is stored (`exact`, `nearest`, `none`). Duration shown is **segment duration**, labelled as such, not measured time saved. Merged segments spanning several categories are stored with all categories. |
| Skip requested vs. confirmed | **Requested only** | `Skipping segment: seeking to %s` is logged *before* the seek command is sent (`DeviceListener.skip`); no success log exists | — | The web UI records `outcome = requested`. There is no upstream evidence of a confirmed seek; the UI says so. |
| YouTube ad skip | **Observed (requested)** | `ytlounge.py` `Ad can be skipped, skipping` | INFO | Logged when `skip_ads` is on and `isSkipEnabled` is true; `skipAd` command is dispatched as a task — no confirmation. |
| YouTube ad mute / unmute | **Observed (requested)** | `ytlounge.py` `Ad has started, muting`, `Ad has ended, unmuting` | INFO, `mute_ads` enabled | Mute → unmute pairs give an *ad mute duration* (labelled; it is not time saved). "Ad has ended" fires on several events and may repeat; repeats are collapsed. |
| Errors and reconnects | **Observed (partial)** | pyytlounge `exception()` logs (`Handle subscribe failed`, `Refresh auth failed`, `Command failed`), `Connection denied…` WARNING, `Connected as screen…` ERROR, `Unknown reply to connect` WARNING; `api_helpers` `print()` for SponsorBlock errors | INFO/WARNING/ERROR | Many failure paths are swallowed by `except BaseException: pass` in `main.py` and produce **no** log. |
| Upstream process availability | **Observed** | Docker API `inspect` (state, started-at, restart count, image labels) | Docker API access | Unavailable in file-only mode; the web UI then reports "unknown" and relies on log freshness. |
| Upstream version | **Observed (best effort)** | OCI `org.opencontainers.image.version` label if present; otherwise "unknown" | Docker API | Not logged by upstream at runtime. |
| Pairing | **Observed** | Web UI performs the same `pairing/get_screen` call upstream uses | Internet access | One-shot HTTP call; no lounge session is opened. Verification: `get_lounge_token_batch` + `get_screen_availability`. |
| Configuration validation | **Web-UI-side** | Rules mirrored from `helpers.Config.validate`, `Device.__validate`, `config_setup`, `setup_wizard`: at least one device; non-empty `screen_id`; `channel_whitelist` non-empty requires `apikey`; `minimum_skip_length` non-negative integer; `skip_categories` subset of known categories; legacy `atvs` key makes upstream exit | — | Unknown keys are preserved verbatim. |
| Reload / restart | **Restart only** | Docker API `POST /containers/{id}/restart` through the web UI's single-container gate (`webui/dockerproxy.py`) | Docker API via gate | No reload exists. Without Docker access the web UI asks the user to restart from the NAS UI and verifies by watching for the new run (Docker: new `StartedAt`; file mode: upstream's `Exited` line) followed by every configured device logging `Refreshed auth`. |
| Playback position / pause / idle | **Unavailable at INFO** | `onStateChange` raw dumps only with `--debug` (`ytlounge._process_event`) | — | Not required by the web UI; not used. |
| Discovery | **Partial** | DIAL (`dial_client.py`) and Chromecast (`chromecast_client.py`) need L2 multicast; inside a bridge-network container they do not work | `network_mode: host` | The web UI offers passive DIAL discovery only when it detects it can bind multicast; manual code pairing is always available. |

## Upstream logging settings the web UI needs

* Keep the default INFO level. **`--debug` is not required.** Enabling it adds raw lounge event
  dumps that include playback positions and device metadata, and it makes the log stream far
  larger; the web UI ignores those lines.
* Keep stdout/stderr going to a Docker logging driver that supports `docker logs`
  (`json-file`, `local`, `journald`). With `none`, `syslog`, `fluentd`, etc. the Docker logs
  API returns nothing and the web UI reports "history unavailable".
* Privacy: upstream INFO lines contain the **screen ID** (logger name) and the **lounge
  token** (pyytlounge). Both are credentials for controlling the TV. The web UI redacts
  them before anything is stored, displayed, or exported.

## Not derivable from upstream (and therefore not shown as fact)

* Whether a seek, mute, or skip-ad actually succeeded on the TV.
* Measured "time saved" (only segment duration is known).
* Video title, channel name, channel ID (need enrichment).
* Device online/offline between log edges (unless the YouTube availability poll is enabled).

## Shutdown and startup records (verified live with 2.11.0)

* On `docker stop`/restart upstream prints `Exited` and then `Aborted.` (from rich_click). `Cancelling tasks and
  exiting...` was not observed on SIGTERM in 2.11.0 although the code path exists.
* With no devices: `No devices found, please add at least one device`, `Exiting in 10 seconds...`, then `Exited`.
* Upstream prints nothing when it starts; a new run is recognised from Docker's `StartedAt` (Docker mode) or from the
  `Exited` line of the previous run (file mode).
* Docker keeps earlier runs' output in `docker logs` across container restarts (json-file driver); the web UI
  attributes such lines to the run they came from by comparing their timestamp with the current run's start.
* The Docker logs API's `since` parameter requires `<unix seconds>.<nanoseconds>`; RFC 3339 is rejected (HTTP 400).
