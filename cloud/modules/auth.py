"""
LAN access control — page-level restriction without login.

Model:
  - Admin  = request originates from the server machine itself (loopback IP).
  - Viewer = any other client on the LAN.

Viewers can read everything (GET) and acknowledge alerts. All other
mutating endpoints (camera/zone/zone_group create/update/delete) are
blocked at the middleware layer with HTTP 403.

Caveats:
  - When opening the dashboard on the server console, use
    http://localhost:8002 or http://127.0.0.1:8002. Hitting the
    machine's own LAN IP makes request.client.host look like a remote
    client and you'll be treated as a viewer.
  - If this service is ever fronted by a reverse proxy, request.client.host
    becomes the proxy's IP (usually loopback) for every client, which
    would make everyone an admin. Add X-Forwarded-For handling here
    before doing that.
"""

from __future__ import annotations

import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_ADMIN_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Endpoints viewers are explicitly allowed to call even though they mutate.
# Acknowledge is a normal operator action and should work from any LAN client.
_VIEWER_WRITE_ALLOWLIST = [
    re.compile(r"^/api/alerts/[^/]+/acknowledge/?$"),
]

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_admin_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    return client.host in _ADMIN_HOSTS


def _is_viewer_allowed_write(path: str) -> bool:
    return any(p.match(path) for p in _VIEWER_WRITE_ALLOWLIST)


class AdminOnlyMiddleware(BaseHTTPMiddleware):
    """Reject mutating requests from non-admin (non-loopback) clients."""

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
