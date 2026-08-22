"""Execution-independent resource presentation (ADR-037).

Exact identifiers stay in execution/recovery authority.  This module produces a
bounded display value and stable correlation fingerprint for audit, approval,
memory, cognition, and other destinations that must not execute the resource.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_RESOURCE_DISPLAY_CHARS = 300


def _one_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _bound(value: str, limit: int = MAX_RESOURCE_DISPLAY_CHARS) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _fingerprint(kind: str | None, exact: str) -> str:
    material = f"{kind or ''}\0{exact}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()


def _url_display(exact: str) -> str:
    try:
        parts = urlsplit(exact)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return "url:[invalid resource]"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parts.port
        netloc = host + (f":{port}" if port is not None else "")
        display = urlunsplit((scheme, netloc, parts.path or "", "", ""))
        if parts.query:
            display += "?[query omitted]"
        if parts.fragment:
            display += "#[fragment omitted]"
        return display
    except (TypeError, ValueError):
        return "url:[invalid resource]"


@dataclass(frozen=True)
class ResourcePresentation:
    """Safe non-executable representation of one exact resource identifier."""

    kind: str | None
    display: str | None
    fingerprint: str | None
    redacted: bool

    def metadata(self) -> dict[str, object]:
        return {
            "resource": self.display,
            "resource_fingerprint": self.fingerprint,
            "resource_redacted": self.redacted,
        }


def present_resource(
    kind: str | None,
    exact: str | None,
) -> ResourcePresentation:
    """Build bounded display/correlation metadata without retaining ``exact``."""
    if exact is None:
        return ResourcePresentation(
            kind=kind,
            display=None,
            fingerprint=None,
            redacted=False,
        )
    if not isinstance(exact, str):
        raise TypeError("resource identifier must be a string or None")
    if kind == "url":
        display = _url_display(exact)
    else:
        display = _one_line(exact)
    display = _bound(display)
    return ResourcePresentation(
        kind=kind,
        display=display,
        fingerprint=_fingerprint(kind, exact),
        redacted=display != exact,
    )


def present_resource_reason(
    reason: str,
    kind: str | None,
    exact: str | None,
    *,
    max_chars: int = 500,
) -> str:
    """Replace an exact resource embedded in a reason with its display value."""
    presentation = present_resource(kind, exact)
    value = _one_line(str(reason))
    if exact and presentation.display is not None:
        value = value.replace(exact, presentation.display)
    return _bound(value, max_chars)
