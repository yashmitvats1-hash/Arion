"""Dedicated outbound webhook HTTP transport (ADR-059 D12).

This is deliberately NOT `arion.capabilities.http`. That module is an agent
capability: model-reachable, GET-only, governed by the permission and
authorization seams. This one is infrastructure: POST-only, never plannable,
never permission-checked, and subject to a stricter network policy. Sharing
one implementation would mean any relaxation made for agent browsing
silently widened the SSRF surface of an unattended background sender.

Policy enforced here (ADR-059 D12):

  * HTTPS only. There is no plaintext exception, not even for loopback.
  * No redirect following at all (a 3xx is a failed attempt).
  * Origin allowlist checked at BOTH subscription create and every attempt.
  * Literal IP destinations rejected; resolved addresses screened against
    private/loopback/link-local/reserved ranges.
  * Response body read is bounded; the response is never parsed or acted on.
  * One whole attempt is bounded by a monotonic deadline (D12.2).

Known and accepted limitation (ADR-059 D12.4): screening resolved addresses
before connecting does not eliminate DNS rebinding, because the socket layer
resolves again. This narrows the window; it does not close it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from arion.observability.error_boundary import sanitize_error_text

#: Signature header names (ADR-059 D11.1).
SIGNATURE_HEADER = "X-Arion-Signature"
SIGNATURE_VERSION_HEADER = "X-Arion-Signature-Version"
DELIVERY_HEADER = "X-Arion-Delivery-Id"
EVENT_KIND_HEADER = "X-Arion-Event-Kind"
TIMESTAMP_HEADER = "X-Arion-Timestamp"

_USER_AGENT = "Arion-Webhook/1"


class WebhookTransportError(Exception):
    """Any failure to complete one delivery attempt."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class WebhookResponse:
    """Outcome of one attempt. Bodies are captured only for diagnostics."""

    status_code: int
    body_snippet: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@runtime_checkable
class WebhookTransport(Protocol):
    """Seam that lets every delivery path be tested without real sockets."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
        max_response_bytes: int,
    ) -> WebhookResponse: ...


def compute_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 over EXACTLY the bytes transmitted (ADR-059 D11.1)."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_headers(
    *,
    delivery_id: str,
    event_kind: str,
    secret: str,
    secret_version: int,
    body: bytes,
    timestamp: str,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        DELIVERY_HEADER: delivery_id,
        EVENT_KIND_HEADER: event_kind,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_VERSION_HEADER: str(secret_version),
        SIGNATURE_HEADER: compute_signature(secret, body),
    }


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #


def origin_of(url: str) -> str:
    """Normalized scheme://host[:port] used for allowlist comparison."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    origin = f"{parts.scheme.lower()}://{host}"
    if parts.port is not None:
        origin = f"{origin}:{parts.port}"
    return origin


def _is_forbidden_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(url: str, allowed_origins: frozenset[str] | set[str]) -> str:
    """Validate a destination URL against the full D12 policy.

    Returns the normalized origin. Raises ValueError with a specific,
    non-leaking reason on any violation. Called at subscription create AND
    before every attempt, because the allowlist can change between them.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    candidate = url.strip()

    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https":
        raise ValueError("webhook url must use https (ADR-059 D12: no plaintext exception)")
    if not parts.hostname:
        raise ValueError("webhook url must include a host")
    if parts.username or parts.password:
        raise ValueError("webhook url must not embed credentials")
    if parts.fragment:
        raise ValueError("webhook url must not include a fragment")

    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("webhook url must use a hostname, not a literal IP address")

    origin = origin_of(candidate)
    if origin not in {o.lower() for o in allowed_origins}:
        raise ValueError(
            "webhook url origin is not in ARION_WEBHOOK_ALLOWED_ORIGINS"
        )
    return origin


def screen_destination(host: str) -> None:
    """Reject hosts that resolve into private/loopback/reserved space."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise WebhookTransportError(
            f"dns resolution failed: {sanitize_error_text(str(exc))}", retryable=True
        ) from exc
    if not infos:
        raise WebhookTransportError("dns resolution returned no addresses", retryable=True)
    for info in infos:
        addr = info[4][0]
        if _is_forbidden_address(addr):
            raise WebhookTransportError(
                "webhook host resolves to a non-public address", retryable=False
            )


# --------------------------------------------------------------------------- #
# implementations
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable redirect following entirely (ADR-059 D12.1)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


#: Body is consumed in bounded slices so the remaining attempt budget can be
#: re-applied before every blocking read. Small enough that a slow-drip
#: sender cannot buy meaningful extra time per syscall; large enough that a
#: normal 8 KiB response completes in a couple of reads.
_READ_CHUNK_BYTES = 4096


def _socket_of(response: object) -> socket.socket | None:
    """Best-effort access to the socket underlying an HTTP response.

    Used only to re-arm the per-operation timeout with the REMAINING attempt
    budget. CPython exposes this as `response.fp.raw._sock`; the lookup is
    defensive because it is an implementation detail, and the caller stays
    correct (deadline still enforced between reads) if it returns None.
    """
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if isinstance(sock, socket.socket):
        return sock
    sock = getattr(fp, "_sock", None)
    if isinstance(sock, socket.socket):
        return sock
    return None


def _read_bounded(response: object, max_bytes: int, remaining) -> bytes:
    """Read at most `max_bytes` under the caller's whole-attempt deadline.

    ADR-059 D12.2. Two things happen before EVERY blocking read:

      1. `remaining()` is evaluated, which raises once the deadline passes -
         so the body phase can never silently reset the attempt budget; and
      2. the socket timeout is re-armed to exactly that remaining budget, so
         a sender that stops mid-body cannot block past the deadline.

    Without (2) a slow-drip sender defeats the bound: each individual chunk
    arrives inside the per-operation timeout, so no single read ever times
    out while the attempt runs arbitrarily long. That was the P1 defect.
    """
    limit = max(0, int(max_bytes))
    if limit == 0:
        return b""

    sock = _socket_of(response)
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        budget = remaining()
        if sock is not None:
            try:
                sock.settimeout(budget)
            except OSError:
                pass
        want = min(_READ_CHUNK_BYTES, limit - total)
        try:
            # read1() returns as soon as SOME data is available rather than
            # blocking for a full chunk, which keeps the deadline check
            # tight; read() would block until `want` bytes or EOF.
            piece = response.read1(want)  # type: ignore[attr-defined]
        except AttributeError:
            piece = response.read(want)  # type: ignore[attr-defined]
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)


class StdlibWebhookTransport:
    """urllib-based POST transport with a monotonic whole-attempt deadline.

    ADR-059 D12.2 [FACT]: the stdlib socket timeout is PER BLOCKING
    OPERATION, not per request. A whole-attempt bound is therefore built by
    computing a monotonic deadline ONCE and re-deriving the REMAINING budget
    before every blocking phase of the attempt:

      1. DNS/address validation  - charged against the budget after it runs
      2. TCP connect             - opener.open(timeout=remaining())
      3. TLS negotiation         - same socket timeout as connect
      4. request transmission    - same socket timeout as connect
      5. response acquisition    - same socket timeout as connect
      6. response-body reads     - re-armed per slice in `_read_bounded`

    Step 6 is the one that matters most and the one originally missing: a
    body read that inherits a per-operation timeout lets a slow-drip sender
    run an attempt arbitrarily long, because every individual chunk arrives
    inside the per-operation limit. Re-arming the socket with the remaining
    budget before each bounded slice removes that.

    [FACT] Residual overrun is bounded by the return latency of the single
    in-flight syscall at the moment the deadline passes - Python cannot
    interrupt a blocking read already in progress. The bound is therefore
    "deadline plus at most one syscall's return latency", not a hard
    real-time guarantee, which is why the lease floor keeps a fixed margin
    over the timeout (config.LEASE_TIMEOUT_MARGIN_SECONDS). This is
    documented rather than overclaimed.
    """

    def __init__(self, *, screen_addresses: bool = True) -> None:
        self._screen = screen_addresses
        self._opener = urllib.request.build_opener(_NoRedirect())

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
        max_response_bytes: int,
    ) -> WebhookResponse:
        deadline = time.monotonic() + float(timeout)

        def remaining(minimum: float = 0.05) -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WebhookTransportError("attempt deadline exceeded", retryable=True)
            return max(minimum, left)

        if self._screen:
            host = urlsplit(url).hostname or ""
            screen_destination(host)
            # DNS/address validation (D12.2 step 1) is inside the attempt, so
            # the time it consumed is charged against the budget before the
            # connection is opened. [FACT] socket.getaddrinfo() itself takes
            # no timeout argument and is bounded only by the OS resolver;
            # charging it here is what keeps a slow resolver from granting a
            # full fresh timeout to the connect/transmit phases.
            remaining()

        request = urllib.request.Request(url, data=body, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)

        try:
            with self._opener.open(request, timeout=remaining()) as response:
                status = int(getattr(response, "status", 0) or 0)
                # Body consumption is inside the same deadline (D12.2 step 6).
                raw = _read_bounded(response, max_response_bytes, remaining)
        except urllib.error.HTTPError as exc:
            # A non-2xx status is a real, classifiable outcome, not a
            # transport failure: hand it back so retry classification can
            # distinguish 4xx from 5xx. The error body is still read under
            # the deadline - an error response can drip just like a 200.
            try:
                raw = _read_bounded(exc, max_response_bytes, remaining)
            except WebhookTransportError:
                raw = b""
            except Exception:
                raw = b""
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            return WebhookResponse(int(exc.code), _snippet(raw))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise WebhookTransportError(
                f"transport error: {sanitize_error_text(str(reason))}", retryable=True
            ) from exc
        except TimeoutError as exc:
            raise WebhookTransportError("attempt timed out", retryable=True) from exc
        except WebhookTransportError:
            # Deadline breach detected inside the body read: already the
            # right error with the right classification, so propagate it
            # rather than re-wrapping it as a generic transport error.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise WebhookTransportError(
                f"transport error: {sanitize_error_text(str(exc))}", retryable=True
            ) from exc

        return WebhookResponse(status, _snippet(raw))


def _snippet(raw: bytes, limit: int = 200) -> str:
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return ""
    return sanitize_error_text(text[:limit])


class FakeWebhookTransport:
    """Deterministic in-memory transport for tests (never opens a socket)."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        error: WebhookTransportError | None = None,
        delay: float = 0.0,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
        max_response_bytes: int,
    ) -> WebhookResponse:
        self.calls.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return WebhookResponse(self.status_code, "")
