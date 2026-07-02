"""Per-client rate limiting for the public + auth surface.

Behind the Cloudflare Tunnel the socket peer is always the tunnel itself, so a
naive remote-address key would lump every visitor together. We key on
Cloudflare's real-visitor header (``CF-Connecting-IP``), then ``X-Forwarded-For``,
then the socket address for direct/local access.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from kith.config import get_settings


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=client_ip,
    enabled=get_settings().rate_limit_enabled,
    storage_uri="memory://",
    headers_enabled=True,
)
