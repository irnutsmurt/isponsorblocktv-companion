# Troubleshooting

Start with **Diagnostics**: it shows web UI, collection, container and database health, and every warning has a
“Technical details” section. Errors on every page say what failed, what it affects, and what to do.

| Symptom | Cause and fix |
|---|---|
| “Setup token is invalid or expired” | Copy the newest token from the web UI container log (a new one is printed every 60 minutes), or set `SETUP_TOKEN`. |
| “The web UI cannot write to …” | The web UI's UID cannot create files in iSponsorBlockTV's data folder. Grant write access to the folder or set `user:` to the folder owner. Getting started shows the UID. |
| “config.json is not valid” | The file was edited by hand or truncated. The web UI will not overwrite it. Restore a backup on the Configuration page, or fix the JSON. |
| “config.json changed outside this page” | Someone or something else edited the file. Load the latest version; your unsaved edits are discarded only when you choose to. |
| “No container named … is visible” | `UPSTREAM_CONTAINER` / `GATE_TARGET_CONTAINER` must equal iSponsorBlockTV's `container_name`. |
| “The Docker API gate denied this request” | Restart is disabled on the gate (`GATE_ALLOW_RESTART=false`) or the target name differs. Restart iSponsorBlockTV yourself; the app still confirms. |
| “… uses the 'none' logging driver” | Use `json-file`, `local` or `journald` for iSponsorBlockTV, or file mode. |
| Statistics say “Not collected yet” | Collection has not received any iSponsorBlockTV output. Check the collection status on Diagnostics. |
| A TV shows “Session ended” | The YouTube app on the TV closed or the TV slept. iSponsorBlockTV reconnects by itself when the app opens. |
| A TV shows “Unknown” | Not enough evidence (e.g. iSponsorBlockTV restarted and has not reported this TV yet). Silence is not treated as offline. **Check status** asks YouTube directly. |
| YouTube status “Screen ID not recognised” | YouTube no longer knows this TV's screen ID (revoked, legacy 26-character ID, or the TV was reset). **Re-pair** it. |
| “Saved, but applying failed” | The new config.json is on disk; iSponsorBlockTV did not confirm it. The details name the step (restart timeout, gate denied, a TV that did not load). Retry, or restart iSponsorBlockTV from the NAS. The previous configuration is in Backups; it is never restored automatically because older screen IDs may be invalid. |
| “iSponsorBlockTV reported a fatal startup problem” | No devices, or the legacy `atvs` key. Fix it on the Configuration page. (A whitelist without an API key makes upstream crash with a traceback, which appears in the iSponsorBlockTV log; the web UI refuses to save that combination.) |
| Pairing: “YouTube did not recognise this code” | TV codes expire within minutes. Open Settings → Link with TV code again and use the new code. |
| Pairing times out | The web UI needs outbound HTTPS to www.youtube.com. |
| Passkey button missing or refused | See [https-and-passkeys.md](https-and-passkeys.md): HTTPS, configured public address, and the same hostname are required. |
| Locked out | Recovery code on the sign-in page. Without it: stop the web UI, delete `webui.db`, start, set up again. |

When asking for help, attach the diagnostic bundle (Diagnostics → Download bundle) after reviewing it.
