# Installation, upgrade, backup and restore

## What you add

| Service | Purpose | Ports | Needs |
|---|---|---|---|
| `companion` | Web UI and API, collector, database | `8099` (only published port) | its own data volume; the iSponsorBlockTV data folder (read/write) |
| `docker-gate` | Owns the Docker socket. Allows only *inspect*, *logs* and *restart* of the one container named in `GATE_TARGET_CONTAINER` | none (internal network) | `/var/run/docker.sock` |

Your existing `iSponsorBlockTV` service does not change. Adding Docker `logging:` limits to it is recommended
(see below) but optional.

Without the gate, the companion still works: it then needs another log source (see *Running without Docker access*)
and you restart iSponsorBlockTV yourself after saving; the app detects the restart and confirms.

## Step by step (Synology / QNAP / Unraid / TrueNAS / plain Docker)

1. **Create a folder** for companion data, e.g. `/volume1/docker/isbtv-companion`.
2. **Find iSponsorBlockTV's data folder** — the host path mounted at `/app/data` in its container. It contains (or will contain) `config.json`.
3. **Permissions.** The companion runs as UID/GID `1000:1000` by default (`user:` in compose). That user must be able to
   *write the folder* (not just the file): saves are atomic (`write temp file → rename`).
   * Synology: give the user that owns the folder (often UID 1026, GID 100) and set `user: "1026:100"`.
   * Or: `chown 1000:1000 <folder>` / add write permission for that UID.
   * The companion's **Getting started** page reports exactly which UID it runs as and whether it can write.
   The companion keeps the existing file's mode and (when permitted) owner when it replaces `config.json`.
4. **Paste the services** from `deploy/docker-compose.yml`, replacing the paths, the image name and `GATE_TOKEN`
   (any long random string, identical in both services). Keep `UPSTREAM_CONTAINER`/`GATE_TARGET_CONTAINER`
   equal to iSponsorBlockTV's `container_name`.
5. **Start the stack.** Open the companion container's log in your NAS UI. Copy the token shown under
   “first-time setup” (valid 60 minutes; a new one is printed when it expires). Or set `SETUP_TOKEN` yourself.
6. **Open `http://<nas-ip>:8099`**, create the administrator, store the recovery code.
7. **Getting started** checks the deployment and pairs the first TV. Existing configurations are loaded as-is; nothing
   is re-paired.

## Logging limits for the iSponsorBlockTV container (recommended)

```yaml
  iSponsorBlockTV:
    logging:
      driver: json-file          # json-file, local or journald: the companion can read these
      options: { max-size: "10m", max-file: "5" }
```

* `none`, `syslog`, `fluentd`, `gelf`, … cannot be read back; the companion shows “history unavailable”.
* Docker owns the retention of stdout logs; if Docker rotates away lines the companion has not read yet
  (companion stopped for a long time), the gap is recorded and shown.
* iSponsorBlockTV's default INFO level is enough. **Do not** add `--debug` for the companion's sake.

## Running without Docker access (file mode)

If you cannot run the gate, make iSponsorBlockTV also write its output to a file in its data folder by overriding its
entrypoint in compose (a deployment setting, not a code change). This wrapper forwards SIGTERM so iSponsorBlockTV
still shuts down cleanly, and its output still reaches `docker logs`:

```yaml
  iSponsorBlockTV:
    entrypoint:
      - sh
      - -c
      - >-
        mkfifo /tmp/out; tee -a /app/data/isponsorblocktv.log < /tmp/out &
        python3 -u main.pyc > /tmp/out 2>&1 & PY=$$!;
        trap "kill -TERM $$PY" TERM INT; wait $$PY; wait $$PY; wait
```

(`$$` is compose's escape for a literal `$`.) Then set on the companion `LOG_SOURCE: file`,
`UPSTREAM_LOG_FILE: /upstream-data/isponsorblocktv.log`, `UPSTREAM_TZ: <iSponsorBlockTV container timezone, usually UTC>`,
and remove `DOCKER_HOST`. The file is not rotated by Docker: rotate or truncate it with your NAS tools; the companion
follows truncation and rotation. After saving, the companion asks you to restart iSponsorBlockTV and confirms once it
sees upstream's `Exited` line followed by every configured TV loading.

Verified with iSponsorBlockTV 2.11.0: `docker stop` completes immediately and the shutdown lines are captured. A plain
`sh -c "... | tee"` wrapper is **not** recommended: the shell ignores SIGTERM, Docker kills the container after its
timeout, and the shutdown lines are lost.

## Upgrades

* Companion: pull the new image and recreate the container. Database migrations run automatically at start and are
  logged (`db.migration.*`). A failed migration stops the app with a CRITICAL log entry and leaves the database untouched
  (each migration runs in one transaction). Take a backup first (below).
* iSponsorBlockTV: upgrade as usual. Check **Diagnostics → Integration capabilities** afterwards; if a future version
  changes its log messages, unknown lines show up as `other` in the iSponsorBlockTV log viewer and the collector
  keeps running. Supported: 2.x, verified with 2.11.0.

## Backup

Everything lives in the companion data folder:

| Path | Content |
|---|---|
| `companion.db` (+ `-wal`, `-shm`) | accounts, sessions, passkeys, history, statistics, settings |
| `config-backups/` | copies of `config.json` taken before every change (newest 20 by default) |
| `logs/` | companion log files, only if `LOG_FILE_ENABLED=true` |
| `tls/` | self-signed certificate, only if `TLS_SELF_SIGNED=true` |

Safest: stop the companion container, copy the folder, start it. Online alternative:
`docker exec isbtv-companion python -c "import sqlite3;s=sqlite3.connect('/data/companion.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"`.
The database contains password hashes and passkey public keys (no plaintext secrets); still treat it as private.

## Restore

* Companion: stop it, replace the data folder (or just `companion.db`) with the backup, start it. Remembered sessions
  in the backup become valid again; use **Sign out all devices** if unsure.
* `config.json`: **Configuration → Backups → Restore**. The current file is backed up first. Screen IDs in old backups
  may have been revoked by YouTube since; re-pair if a TV stops working.

## Lost the password

Use **Forgot password? Use your recovery code** on the sign-in page. All sessions are signed out and a new recovery
code is issued. Without the recovery code: stop the companion, delete `companion.db`, start it, and set up again
(history is lost; iSponsorBlockTV's configuration is untouched).

## Uninstall

Remove the `companion` and `docker-gate` services. iSponsorBlockTV keeps running with its current `config.json`.
