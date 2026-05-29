"""Utilities for normalizing RTSP source URLs.

Some camera setup notes were pasted into the URL field as trailing labels like
``IR-Z03`` or ``OR-Z01``. Those suffixes are not part of the actual RTSP path
and can cause decoders to fail during SETUP.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_TRAILING_LABEL_RE = re.compile(r"\s+(?:IR|OR)-[A-Za-z0-9_-]+$", re.IGNORECASE)


def normalize_rtsp_url(source_url: str | None) -> str:
    """Strip UI-only label suffixes and percent-encode the RTSP path safely."""
    url = (source_url or "").strip()
    if not url.lower().startswith("rtsp://"):
        return url

    parts = urlsplit(url)
    hostname = parts.hostname or ""
    netloc = parts.netloc
    if hostname.lower() == "localhost":
        auth = ""
        if parts.username:
            auth = parts.username
            if parts.password:
                auth = f"{auth}:{parts.password}"
            auth = f"{auth}@"
        host = "127.0.0.1"
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"{auth}{host}"
    clean_path = unquote(parts.path or "")
    clean_path = _TRAILING_LABEL_RE.sub("", clean_path).strip()
    encoded_path = quote(clean_path, safe="/-._~!$&'()*+,;=:@")
    return urlunsplit((parts.scheme, netloc, encoded_path, parts.query, parts.fragment))
