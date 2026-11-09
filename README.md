# iSponsorBlockTV Web UI

A web front end for an existing [iSponsorBlockTV](https://github.com/dmunozv04/iSponsorBlockTV) installation.
Add it to your Docker stack to sign in securely, pair TVs, edit settings, and see what iSponsorBlockTV is doing,
on a phone or a desktop, without a terminal.

* **Does not modify iSponsorBlockTV.** It reads the same `config.json` and iSponsorBlockTV's own log output.
  Stopping or removing the web UI never affects skipping.
* **Authentication from first start.** One administrator, created with a one-time setup token. Argon2id passwords,
  optional passkeys, “remember this device for 30 days”, session management, recovery code.
* **Pairing and configuration in the browser**, with atomic writes, conflict detection, backups and a verified
  “save and apply” (restart of the iSponsorBlockTV container through a gate that can do nothing else).
* **Statistics with honest labels**: skips iSponsorBlockTV *requested*, SponsorBlock segment durations (not measured
  time saved), ad skips and mutes kept separate, collection gaps shown.
* **Structured logging and diagnostics**: levels, temporary debug mode, rotation/compression/retention, redaction,
  log viewer and a sanitized diagnostic bundle.

## Documentation

| Topic | File |
|---|---|
| Install on a NAS / Docker, permissions, upgrade, backup, restore | [docs/installation.md](docs/installation.md) |
| HTTPS, reverse proxies, passkeys | [docs/https-and-passkeys.md](docs/https-and-passkeys.md) |
| What can and cannot be observed from iSponsorBlockTV (capability table) | [docs/upstream-capabilities.md](docs/upstream-capabilities.md) |
| Metric definitions and data retention | [docs/metrics.md](docs/metrics.md) |
| Logging architecture, levels, retention, diagnostics | [docs/logging.md](docs/logging.md) |
| Security model | [docs/security.md](docs/security.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Test results (live, simulated, unverified) | [docs/test-results.md](docs/test-results.md) |
| Configuration reference (environment variables) | [docs/configuration.md](docs/configuration.md) |

## Quick start

1. Copy the `docker-gate` and `webui` services from [deploy/docker-compose.yml](deploy/docker-compose.yml) into your stack.
2. Point both `/upstream-data` and iSponsorBlockTV's `/app/data` at the same folder, and make it writable for the web UI's user.
3. Start the stack, open `http://<nas>:8099`, and enter the setup token printed in the web UI container's log
   (or the `SETUP_TOKEN` you set).
4. Follow **Getting started** to pair your first TV.

## Development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements.txt pytest pytest-asyncio httpx cbor2
cd frontend && npm ci && npm run build        # outputs to backend/webui/static
cd ../backend && python -m pytest             # backend tests
cd ../e2e && npm i && ./run-e2e.sh            # browser tests (needs /usr/bin/chromium)
docker build -t isponsorblocktv-webui .
```

License: GPL-3.0-or-later (same family as iSponsorBlockTV; no upstream code is included).
