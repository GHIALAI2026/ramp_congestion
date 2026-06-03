"""
LAN access control — page-level restriction without login.

Model:
  - Admin  = request originates from the server machine itself (the console,
             hitting http://localhost:8002 directly — loopback, no proxy).
  - Viewer = any other client on the LAN, which reaches the app through the
             Nginx reverse proxy.

Viewers can read everything (GET) and acknowledge alerts. All other
mutating endpoints (camera/zone/zone_group create/update/delete) are
blocked at the middleware layer with HTTP 403.

Reverse proxy handling
----------------------
The app is bound to 127.0.0.1:8002, so the immediate TCP peer is ALWAYS
loopback — either the Nginx proxy (for LAN operators) or the server console
itself. We therefore cannot tell them apart by the socket peer alone; we use
the proxy's forwarded client-IP header:

  - A request proxied by Nginx carries X-Real-IP / X-Forwarded-For set to the
    real LAN client. That real IP is not loopback → Viewer.
  - The server console hitting localhost directly has NO such header → Admin.

We only trust those headers when the connecting peer is a configured trusted
proxy (settings.trusted_proxies, default loopback). We read X-Real-IP, which
Nginx sets to $remote_addr and which a client cannot forge through the proxy.
We deliberately do NOT trust the left-most X-Forwarded-For entry, since that
value is client-supplied and spoofable; we fall back to the right-most XFF
entry (the hop our trusted proxy appended) only if X-Real-IP is absent.

If the app is ever bound to a non-loopback interface, or a second proxy hop is
added, revisit trusted_proxies and the XFF parsing below.
"""

from __future__ import annotations

import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from cloud.config import settings

_ADMIN_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Endpoints viewers are explicitly allowed to call even though they mutate.
# Acknowledge is a normal operator action and should work from any LAN client.
_VIEWER_WRITE_ALLOWLIST = [
    re.compile(r"^/api/alerts/[^/]+/acknowledge/?$"),
]

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _real_client_ip(request: Request) -> str | None:
    """Resolve the true client IP, honouring the trusted proxy's headers."""
    client = request.client
    peer = client.host if client else None

    # Only consult forwarded headers when the request actually came from a
    # trusted proxy; otherwise the headers are attacker-controlled.
    if peer in settings.trusted_proxies:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Right-most entry is the address our trusted proxy observed.
            return xff.split(",")[-1].strip()
        # No forwarded headers → request originated locally (console), not via
        # the proxy. Fall through to the peer (loopback) → admin.
    return peer


def is_admin_request(request: Request) -> bool:
    return _real_client_ip(request) in _ADMIN_HOSTS


def _is_viewer_allowed_write(path: str) -> bool:
    return any(p.match(path) for p in _VIEWER_WRITE_ALLOWLIST)


class AdminOnlyMiddleware(BaseHTTPMiddleware):
    """Reject mutating requests from non-admin (non-console) clients."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _MUTATING_METHODS and not is_admin_request(request):
            if not _is_viewer_allowed_write(request.url.path):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "admin_only",
                        "detail": "This action is restricted to the server console.",
                    },
                )
        return await call_next(request)
