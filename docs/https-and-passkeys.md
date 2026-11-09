# HTTPS, reverse proxies and passkeys

The web UI always requires sign-in, but on plain HTTP your password and session cookie cross the network
unencrypted. The UI warns about this on every page. Choose one of these:

## Option A — built-in HTTPS (simplest)

```yaml
    environment:
      TLS_SELF_SIGNED: "true"
      PUBLIC_ORIGIN: https://nas.local:8099      # the exact address you will type
      REQUIRE_HTTPS: "true"                      # refuse sign-in over plain HTTP
```

A certificate for the `PUBLIC_ORIGIN` host is generated in `/data/tls/` (valid 825 days; regenerated at
startup when fewer than 14 days remain). Browsers warn once; accept it or install the certificate on your devices. Note that **many browsers
refuse passkeys on sites with an untrusted certificate**; use option B or C if you want passkeys.

## Option B — your own certificate

```yaml
      TLS_CERT_FILE: /certs/fullchain.pem
      TLS_KEY_FILE: /certs/privkey.pem
      PUBLIC_ORIGIN: https://isbtv.example.home:8099
```

Mount the files read-only. Restart the web UI after renewing them.

## Option C — behind a reverse proxy (Caddy, Nginx Proxy Manager, Traefik, Synology)

Point the proxy at the web UI **any way that suits your setup**: container name (`isbtv-webui:8099`), container
IP, or the NAS address and published port (`192.168.1.10:3099`). Then set just one variable:

```yaml
      PUBLIC_ORIGIN: https://isbtv.example.home:8443   # the exact URL you type, including a non-standard port
```

That's all. **HTTPS is detected automatically**: the browser reports the address it is on with every request, so
the web UI knows the connection is HTTPS without any proxy settings, and it marks the session cookie Secure.

**Optional — show real device IPs.** Without further setup the web UI sees every request as coming from the proxy,
so the session list shows the proxy's address and sign-in throttling is per proxy instead of per device (per-account
throttling still applies). To fix that, open **Account & security → Access → Reverse proxy**. When requests carry
proxy headers from an address the web UI does not trust yet, it shows *“A reverse proxy was detected”* with a
**Trust** button. Click it if that address is your proxy. You can also enter an address or network by hand, or set
`TRUSTED_PROXIES` (comma-separated IPs/CIDRs) in the compose file. Forwarded headers from anything not trusted are
ignored, so other devices on your network cannot fake their address.

Disable response buffering for `/api/stream` (live updates) if your proxy buffers; Caddy does not need this.
Do **not** open router ports or enable UPnP for this app; use a VPN to reach it from outside.

## Cookies

* `__Host-isbtv_session` on HTTPS (Secure, HttpOnly, SameSite=Lax, Path=/); `isbtv_session` on plain HTTP.
* No authentication data is stored in `localStorage`. The theme preference is the only thing stored there.

## Passkeys

Requirements, checked before enrollment with a specific explanation if one is missing:

* HTTPS (or `http://localhost`): browsers only expose WebAuthn in a secure context.
* A configured **public address** (`PUBLIC_ORIGIN` or Account & security → Access). The relying-party ID is its host
  name; the expected origin is exactly that address. Request `Host` headers are never trusted for this.
* You open the app at exactly that address.

**Hostnames are permanent for passkeys.** A passkey created for `isbtv.example.home` does not work at
`192.168.1.10` or after you rename the host. If you change the address, sign in with your password and create new
passkeys; remove the old ones on the Account page.

Server behaviour (py_webauthn): user verification required; challenges are random, stored server-side, expire after
5 minutes, and are consumed before verification (a replayed response fails); origin, RP ID, signature and credential
ownership are verified; sign counts are stored. Only public keys are stored. Password login and the recovery code keep
working. Registering or removing a passkey requires having entered your password in the last 10 minutes.
