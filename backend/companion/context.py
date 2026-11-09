"""Per-request facts derived safely: client IP, scheme, origin.

Forwarded headers are honoured only when the direct peer is in ``TRUSTED_PROXIES``.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import Request

from .settings import Settings


@dataclass
class RequestInfo:
    client_ip: str
    scheme: str
    host: str
    origin: str
    via_trusted_proxy: bool

    @property
    def is_secure(self) -> bool:
        return self.scheme == "https"


def _peer_is_trusted(peer: str | None, settings: Settings) -> bool:
    if not peer or not settings.trusted_proxies:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in settings.trusted_proxies)


def request_info(request: Request) -> RequestInfo:
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else None
    trusted = _peer_is_trusted(peer, settings)
    scheme = request.url.scheme
    host = request.headers.get("host", "")
    client_ip = peer or "unknown"
    if trusted:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # The right-most address was appended by our trusted proxy; walk left past other trusted hops.
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            for hop in reversed(hops):
                client_ip = hop
                if not _peer_is_trusted(hop, settings):
                    break
        xfp = request.headers.get("x-forwarded-proto")
        if xfp in {"http", "https"}:
            scheme = xfp
        xfh = request.headers.get("x-forwarded-host")
        if xfh:
            host = xfh.split(",")[0].strip()
    origin = f"{scheme}://{host}" if host else ""
    return RequestInfo(client_ip=client_ip, scheme=scheme, host=host, origin=origin, via_trusted_proxy=trusted)
