# Test results (2026-11-09)

Three kinds of evidence, kept separate:

* **Live** — real iSponsorBlockTV 2.11.0 container (built unmodified from upstream source), real Docker Engine 29.8,
  the web UI image, its Docker gate, and real YouTube/SponsorBlock endpoints.
* **Simulated** — automated tests with sanitized fixtures, a fake YouTube lounge pairing server, a software WebAuthn
  authenticator, and Chromium viewport/touch emulation.
* **Unverified** — not exercised at all; listed at the end.

## Automated backend tests — 66 passed (`cd backend && pytest`)

| Suite | Tests | Covers |
|---|---|---|
| `test_auth.py` | 20 | every private API, export, config action and SSE stream returns 401 without a session; minimal public surface; bootstrap token required, single use, disabled after restart; 6 concurrent setup attempts → exactly one admin; generated token absent from ring buffer, bundle and later logs; generic login errors; throttling with bounded `Retry-After`; rotating forged `X-Forwarded-For` does not evade throttling; CSRF (missing header, foreign Origin); remembered cookie flags and survival across an app restart; session rotation, revocation, logout; server-side expiry; recent-auth gate; recovery revokes sessions, rotates code, rejects reuse; 503-character password not truncated; Argon2id hash; auth storage failure → 503; security headers; body size limit |
| `test_passkeys.py` | 9 | enrollment + login with a software authenticator, remembered passkey session, challenge replay rejected, wrong origin rejected, wrong RP ID rejected, user verification enforced, page/origin mismatch explained, cancelled ceremony leaves no state, removal needs recent auth and leaves password login working, HTTP origin blocks enrollment |
| `test_ingest.py` | 13 | parser classification and redaction against an upstream-format fixture; upstream timezone conversion; segment merging identical to upstream's own `process_segments` (300 random cases, run against the upstream source file); overlapping segments counted once; full fixture ingestion (3 skips incl. a legitimate repeat, paired mute duration, duplicate unmute collapsed, state transitions); replay dedup; checkpoint survives restart; replay after an upstream restart not double-counted; SponsorBlock attribution (exact / none); timezone day boundaries (America/New_York); retention roll-up preserves totals; file source rotation and truncation; Docker stream demultiplexing; Docker `since` format |
| `test_config.py` | 12 | validation mirrors upstream; unknown keys and other devices preserved; API key and screen IDs never sent to the browser; external change → 409 and file untouched; malformed file never replaced; invalid result rejected with field paths; permission failure reported; atomic write keeps file mode, no temp files left; backups bounded and restorable; add-device flow, duplicate TV blocked, single-use pairing ticket; invalid/rejected codes; last device cannot be removed |
| `test_logging.py` | 12 | structured fields; global and per-component levels; temporary debug expiry restores the level; secrets redacted at DEBUG incl. stack traces; repeated warnings aggregated with counts; size rotation with readable gzip archives and 0600/0700 permissions; daily rotation; retention and total budget; 4 threads × 500 lines during rotation → no loss, no duplicates; unwritable directory and disk-full degrade without raising; console-only token never reaches files or ring buffer |

Bugs found and fixed by these tests: a deadlock between ingest transactions and nested writes; settings read at
import time; the API key and screen IDs leaking through the configuration view; archive name collisions losing log
lines during concurrent rotation.

## Browser tests — 86/86 checks passed (`e2e/run-e2e.sh`, Chromium 130 headless, emulation)

The first-TV workflow ran on an **emulated** Pixel-class phone (390×844, touch, `isMobile`) with taps and a numeric
keypad entry, against a real web UI process in file mode and a **fake** YouTube pairing server:
wrong setup token → explained; account creation; recovery code; first-run overview; deployment checks; invalid TV
code explained; code auto-formatting; “paired” shown separately from “saved”; save without Docker → “restart
needed”, not success; simulated upstream restart → “Saved, applied and verified”; config.json contains the TV;
second TV via Devices with an 80-character name → both TVs present, first unchanged; activity detail stays open
while new entries arrive; unsaved config draft survives reload; external config edit detected and save blocked;
dark theme persists; logout invalidates the server session.

Layout checks (no page-level horizontal scroll; every button, input, select, nav item and summary ≥ 44×44 px) on all
pages at 390×844, 844×390 landscape, 320×640, 820×1180 tablet and 1440×900 desktop. Desktop/tablet also check the
30-day HttpOnly remembered cookie, the skip link as first Tab stop with a visible focus outline, and that
reduced-motion disables animation. Screenshots were reviewed by eye; two visual defects found that way (an unsized icon,
a wrapping button label) were fixed.

## Live integration — real iSponsorBlockTV 2.11.0 + Docker

| Scenario | Result |
|---|---|
| Collect from `docker logs` through the gate; device marked loaded | ✅ |
| Lounge token and screen ID absent from the web UI database and UI | ✅ (`[REDACTED]`, hashed device id) |
| Remembered session survives `docker restart` of the web UI | ✅ |
| Checkpoint resume after webui restart | ✅ after fixing the `since` format (found live) |
| Save and apply: write → gate restart → new run → device loaded → “applied” | ✅ |
| Apply failure path (gate denied / stream lost) shows “Saved, but applying failed” with the reason | ✅ |
| Fresh database over 3 runs of history, then web UI restart: 3 “loaded” events, no duplicates | ✅ after fixing run-independent dedup (found live) |
| iSponsorBlockTV stopped: stats stay visible and marked stale, gap opened, device “Unknown” (not offline); restarted: gap closed | ✅ |
| External edit that empties `devices`, then upstream restart: validation error shown, upstream's fatal reason recognised | ✅ |
| Gate: target inspect/logs allowed; other containers, list, create, exec, stop, images, missing token → 403; env filtered | ✅ |
| Gate hardened (`read_only`, `cap_drop: ALL`, `no-new-privileges`) still restarts and verifies; own healthcheck healthy | ✅ |
| File-mode entrypoint from installation.md: `docker compose down` in 1 s, `Exited` captured, output still in `docker logs` | ✅ |
| Web UI image healthcheck | ✅ healthy |

## Unverified — no claim is made

* **Pairing with a real TV code.** No TV was available; the pairing call is identical to upstream's
  (`pairing/get_screen`), but only a fake server was used end to end. The live stack used a made-up screen ID.
* **Real skips, ad skips/mutes from a real TV.** Parsing is tested against fixtures written from upstream's source
  (exact format strings); no real playback session was observed.
* **SponsorBlock and oEmbed against live servers** inside the full flow (unit-tested with stubs).
* **Passkeys with a real platform authenticator or password manager** (Face ID, Windows Hello, Android, 1Password):
  only the software authenticator was used. Real browsers additionally require trusted HTTPS.
* **Physical phones/tablets, iOS Safari, Firefox, screen readers.** Only Chromium emulation was run; accessibility was
  checked structurally (labels, roles, focus, skip link, target sizes), not with assistive technology.
* **NAS platforms** (Synology DSM, QNAP, Unraid, TrueNAS): not run; instructions are generic Docker.
* **Built-in HTTPS** (`TLS_SELF_SIGNED`) and reverse-proxy deployments: certificate generation code exists, no
  browser session over HTTPS was tested.
* **Discovery** (DIAL/Chromecast) is not offered: it needs host networking and upstream's own code; manual TV-code
  pairing is the supported path.
