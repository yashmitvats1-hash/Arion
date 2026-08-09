"""Read-only HTTP GET capability + URL resource boundary (ADR-018, Phase C).

Security model (network access is NOT arbitrary):

- The POLICY validates the resource boundary (UrlBoundary: scheme, host
  allowlist, no credentials, no malformed URLs) - fail closed when no 'url'
  boundary is configured.
- The CAPABILITY performs the actual request through an injectable transport.
  It enforces its own containment: only http(s), no credentials, bounded
  response size, bounded timeout, and REDIRECTS can never escape the
  configured origin (or the initial request's origin when no allowlist is set).

No POST/PUT/DELETE, no shell, no arbitrary networking: the transport is
injected (tests use a fake; the stdlib transport is the production default).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from arion.capabilities.registry import ActionSpec, CapabilityError

_MAX_REDIRECTS = 5


# --------------------------------------------------------------------------- #
# Response + transport
# --------------------------------------------------------------------------- #


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


class HttpTransport(Protocol):
    """Injected transport: performs ONE GET (no redirects followed by the
    transport - redirect containment lives in the capability)."""

    def get(self, url: str, timeout: float, max_bytes: int) -> HttpResponse: ...


class StdlibHttpTransport:
    """urllib-based transport. Bounded timeout + bounded response read."""

    def __init__(self, timeout: float = 10.0, max_bytes: int = 1_000_000):
        self.timeout = timeout
        self.max_bytes = max_bytes

    def get(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # never auto-follow; the capability handles redirects

        opener = urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Arion/0.1"})
        try:
            with opener.open(request, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise CapabilityError("response exceeded max_bytes")
                return HttpResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()},
                    body=data.decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            data = exc.read(max_bytes + 1)
            return HttpResponse(status=exc.code, headers={k: v for k, v in exc.headers.items()},
                                body=data.decode("utf-8", errors="replace"))
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(f"http get failed: {exc}") from exc


class FakeTransport:
    """Test transport: canned responses per URL, records every request."""

    def __init__(self, routes: dict[str, HttpResponse] | None = None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def get(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise CapabilityError(f"no route for {url}")
        resp = self.routes[url]
        if len(resp.body.encode("utf-8")) > max_bytes:
            raise CapabilityError("response exceeded max_bytes")
        return resp


# --------------------------------------------------------------------------- #
# URL resource boundary (policy layer)
# --------------------------------------------------------------------------- #


def _origin_of(url: str) -> str | None:
    """Normalized origin 'scheme://host[:port]' (default ports dropped), or
    None when the URL is not a valid http(s) URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if parts.username or parts.password:
        return None
    port = parts.port
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class UrlBoundary:
    """Allowlist-based boundary for resource kind 'url' (ADR-018).

    `allows(url)` is False for: malformed URLs, embedded credentials,
    non-HTTP(S) schemes, hosts outside the configured origin allowlist.
    Origins are normalized (lowercase host, default ports dropped).
    """

    def __init__(self, allowed_origins: set[str] | tuple[str, ...] | list[str]):
        normalized: set[str] = set()
        for origin in allowed_origins:
            o = _origin_of(origin)
            if o is not None:
                normalized.add(o)
        self.allowed_origins = frozenset(normalized)

    def allows(self, resource: str) -> bool:
        origin = _origin_of(resource)
        if origin is None:
            return False
        return origin in self.allowed_origins


# --------------------------------------------------------------------------- #
# The capability
# --------------------------------------------------------------------------- #


class HttpGetCapability:
    """Read-only HTTP GET: fetches one URL, bounded and origin-contained."""

    name = "http.get"
    description = "Read-only HTTP(S) GET with allowlist containment and bounded responses."
    actions = [
        ActionSpec(
            name="get",
            description="Fetch an HTTP(S) URL (bounded size/timeout, origin-contained redirects).",
            required_scope="http:get",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="url",
            resource_param="url",
            param_schema={"url": {"type": "string", "required": True}},
            default_verification={"policy": "schema_keys", "args": {"keys": ["status", "body"]}},
        ),
    ]

    def __init__(self, transport: HttpTransport | None = None,
                 allowed_origins: set[str] | None = None,
                 timeout: float = 10.0, max_bytes: int = 1_000_000):
        self.transport = transport or StdlibHttpTransport(timeout=timeout, max_bytes=max_bytes)
        self.allowed_origins = set(allowed_origins) if allowed_origins else None
        self.timeout = timeout
        self.max_bytes = max_bytes

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action != "get":
            raise CapabilityError(f"unknown action {action!r} for {self.name}")
        url = params.get("url")
        if not isinstance(url, str) or not url:
            raise CapabilityError("get requires string param 'url'")
        self._validate_url(url)
        initial_origin = _origin_of(url)

        current = url
        for _ in range(_MAX_REDIRECTS):
            try:
                resp = self.transport.get(current, self.timeout, self.max_bytes)
            except CapabilityError:
                raise
            except Exception as exc:
                raise CapabilityError(f"http get failed: {exc}") from exc
            location = (resp.headers or {}).get("location")
            if resp.status in (301, 302, 303, 307, 308) and location:
                target = urljoin(current, location)
                self._validate_url(target)
                if not self._redirect_allowed(initial_origin, target):
                    raise CapabilityError(
                        f"redirect escaped allowed origin: {target!r} (initial {initial_origin!r})"
                    )
                current = target
                continue
            break
        else:
            raise CapabilityError(f"too many redirects (>{_MAX_REDIRECTS}) for {url!r}")

        if len((resp.body or "").encode("utf-8")) > self.max_bytes:
            raise CapabilityError("response exceeded max_bytes")
        return {
            "action": "get",
            "capability": self.name,
            "url": current,
            "status": resp.status,
            "headers": dict(resp.headers),
            "body": resp.body,
        }

    # ------------------------------------------------------------------ #
    # containment (capability layer)
    # ------------------------------------------------------------------ #

    def _validate_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.username or parts.password:
            raise CapabilityError("credentials in URL are not allowed")
        scheme = (parts.scheme or "").lower()
        if scheme and scheme not in ("http", "https"):
            raise CapabilityError(f"unsupported scheme {scheme!r} (http/https only)")
        if _origin_of(url) is None:
            raise CapabilityError(f"malformed URL: {url!r}")

    def _redirect_allowed(self, initial_origin: str | None, target: str) -> bool:
        target_origin = _origin_of(target)
        if target_origin is None:
            return False
        if self.allowed_origins and target_origin in self.allowed_origins:
            return True
        # same-origin redirects are always permitted (bounded by the allowlist
        # on the INITIAL request); without a configured allowlist, only
        # same-origin redirects are allowed (fail closed).
        return initial_origin is not None and target_origin == initial_origin
