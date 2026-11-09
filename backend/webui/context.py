"""Per-request facts derived safely: client IP, scheme, origin.

* **Scheme** needs no configuration. Behind a TLS-terminating proxy the browser still tells us the
  address it is on: every fetch carries ``Origin`` (non-GET) or ``Referer`` (same-origin GET). If that
  says ``https`` for this same host, the browser used HTTPS. A client can only lie about its *own*
  connection this way, which grants nothing: cookies marked Secure on a plain-HTTP response are simply
  not stored by the browser, and passkeys use the configured public origin, not this value.
* **Client IP** (used for sign-in throttling and the session list) is taken from ``X-Forwarded-For``
  only when the direct peer is a trusted proxy: ``TRUSTED_PROXIES`` or one approved in the UI.
  Untrusted peers that send forwarded headers are remembered so the UI can offer "Trust this proxy".
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded", "x-real-ip")


@dataclass
class RequestInfo:
    client_ip: str
    scheme: str
    host: str
    origin: str
    via_trusted_proxy: bool
    peer: str = ""
    scheme_source: str = "connection"   # connection | proxy | browser

    @property
    def is_secure(self) -> bool:
        return self.scheme == "https"


def trusted_networks(request: Request) -> list:
    st = request.app.state
    return list(st.settings.trusted_proxies) + list(getattr(st, "runtime_trusted_proxies", []) or [])


def _in_networks(ip: str | None, networks: list) -> bool:
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _browser_scheme(request: Request, host: str) -> str | None:
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value or value == "null":
            continue
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.netloc and parts.netloc.lower() == host.lower():
            return parts.scheme
    return None


def request_info(request: Request) -> RequestInfo:
    networks = trusted_networks(request)
    peer = request.client.host if request.client else None
    trusted = _in_networks(peer, networks)
    scheme = request.url.scheme
    scheme_source = "connection"
    host = request.headers.get("host", "")
    client_ip = peer or "unknown"
    if trusted:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # The right-most address was appended by our trusted proxy; walk left past other trusted hops.
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            for hop in reversed(hops):
                client_ip = hop
                if not _in_networks(hop, networks):
                    break
        xfp = request.headers.get("x-forwarded-proto")
        if xfp in {"http", "https"}:
            scheme = xfp
            scheme_source = "proxy"
        xfh = request.headers.get("x-forwarded-host")
        if xfh:
            host = xfh.split(",")[0].strip()
    elif peer and any(h in request.headers for h in FORWARD_HEADERS):
        # Remember an untrusted proxy so the Access page can offer to trust it.
        request.app.state.proxy_hint = {"peer": peer, "at": time.time(),
                                        "forwarded_proto": request.headers.get("x-forwarded-proto")}
    if scheme == "http":
        browser = _browser_scheme(request, host)
        if browser == "https":
            scheme = "https"
            scheme_source = "browser"
    origin = f"{scheme}://{host}" if host else ""
    return RequestInfo(client_ip=client_ip, scheme=scheme, host=host, origin=origin, via_trusted_proxy=trusted,
                       peer=peer or "", scheme_source=scheme_source)
