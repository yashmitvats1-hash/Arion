"""Real-socket tests for `StdlibWebhookTransport` (ADR-059 D12).

These tests deliberately do NOT use `FakeWebhookTransport`. They stand up
actual listening sockets and drive the real stdlib transport, because the
properties under test - the whole-attempt wall-clock deadline, the bounded
body read, redirect rejection - live entirely in the real implementation and
are invisible to a fake.

The slow-drip test in particular is a regression guard for a defect that
shipped once: the transport computed a monotonic deadline but applied it only
to `opener.open()`, so `response.read()` inherited a PER-OPERATION socket
timeout. A sender that dribbled bytes just under that per-operation timeout
kept an attempt alive far past `TIMEOUT_SECONDS` (measured: 30 s against a
2 s configured timeout). Such an attempt can outlive its delivery lease and
cause an otherwise unnecessary duplicate.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from arion.notifications.transport import (
    StdlibWebhookTransport,
    WebhookTransportError,
    validate_webhook_url,
)

# Generous enough to absorb scheduler/syscall jitter and slow CI, while still
# failing decisively against the old implementation (which overran by ~15x).
TIMEOUT_TOLERANCE_SECONDS = 3.0


class RawServer:
    """A minimal raw-socket HTTP server with scripted, byte-level behaviour.

    `http.server` buffers and manages responses for you, which is exactly
    what these tests must control, so the response is written by hand.
    """

    def __init__(self, handler) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._handler = handler
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.5)
                conn, _ = self._sock.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            self._handler(conn, self._stop)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @property
    def url(self) -> str:
        # http:// is used purely so the loopback test server is reachable.
        # HTTPS-only policy lives in validate_webhook_url() and is enforced
        # before the transport is ever called; it is asserted separately in
        # test_https_only_policy_is_unchanged.
        return f"http://127.0.0.1:{self.port}/"

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass


@pytest.fixture()
def transport() -> StdlibWebhookTransport:
    # Address screening is bypassed ONLY so the tests can reach a loopback
    # server; screening itself is covered by the SSRF tests below and in
    # tests/test_webhook_notifications.py.
    return StdlibWebhookTransport(screen_addresses=False)


def _serve_ok(conn, _stop):
    body = b"ok"
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    )


# --------------------------------------------------------------------------- #
# 1. slow-drip timeout (the P1 regression guard)
# --------------------------------------------------------------------------- #


def test_slow_drip_body_cannot_outlive_the_attempt_deadline(transport):
    """A sender dribbling bytes must not extend the attempt past the budget.

    Each chunk arrives every 0.4 s while the configured timeout is 2.0 s, so
    NO INDIVIDUAL read ever exceeds a per-operation timeout. Only a genuine
    whole-attempt deadline can stop this. The old implementation ran until
    the sender finished (~20 s here); the fixed one stops at ~2 s.
    """
    chunk_interval = 0.4
    total_chunks = 50  # would take ~20s to deliver in full

    def handler(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n")
        for _ in range(total_chunks):
            if stop.is_set():
                return
            time.sleep(chunk_interval)
            try:
                conn.sendall(b"X" * 10)
            except OSError:
                return

    server = RawServer(handler)
    try:
        timeout = 2.0
        started = time.monotonic()
        with pytest.raises(WebhookTransportError) as exc:
            transport.post(server.url, b"{}", {}, timeout, 8192)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert exc.value.retryable is True
    assert elapsed < timeout + TIMEOUT_TOLERANCE_SECONDS, (
        f"attempt ran {elapsed:.1f}s against a {timeout}s budget; the "
        f"whole-attempt deadline is not being enforced"
    )
    # And it must be a real bound, not an accidental instant failure.
    assert elapsed >= timeout * 0.5


def test_stalled_body_does_not_wait_indefinitely(transport):
    """Headers arrive, then the sender stops forever mid-body."""

    def handler(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        conn.sendall(b"partial")
        stop.wait(30)  # never completes the body

    server = RawServer(handler)
    try:
        timeout = 1.5
        started = time.monotonic()
        with pytest.raises(WebhookTransportError):
            transport.post(server.url, b"{}", {}, timeout, 8192)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert elapsed < timeout + TIMEOUT_TOLERANCE_SECONDS


def test_slow_drip_on_error_response_is_also_bounded(transport):
    """A 5xx body can drip too; the deadline must cover that path as well."""

    def handler(conn, stop):
        conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\n"
                     b"Content-Length: 5000\r\n\r\n")
        for _ in range(50):
            if stop.is_set():
                return
            time.sleep(0.4)
            try:
                conn.sendall(b"X" * 10)
            except OSError:
                return

    server = RawServer(handler)
    try:
        timeout = 2.0
        started = time.monotonic()
        try:
            transport.post(server.url, b"{}", {}, timeout, 8192)
        except WebhookTransportError:
            pass  # either outcome is acceptable; the BOUND is the assertion
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert elapsed < timeout + TIMEOUT_TOLERANCE_SECONDS


# --------------------------------------------------------------------------- #
# 2. normal response still succeeds
# --------------------------------------------------------------------------- #


def test_normal_response_within_deadline_succeeds(transport):
    server = RawServer(_serve_ok)
    try:
        started = time.monotonic()
        response = transport.post(server.url, b"{}", {}, 10.0, 8192)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert response.status_code == 200
    assert response.ok is True
    assert elapsed < 5.0


def test_request_body_and_headers_reach_the_server(transport):
    seen: dict[str, bytes] = {}

    def handler(conn, _stop):
        # the fixture's recv already consumed the request; re-serve simply
        conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

    class Capturing(RawServer):
        def _handle(self, conn):  # type: ignore[override]
            try:
                # Headers and body may arrive in separate TCP segments, so
                # keep reading until the full declared body is present.
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\r\n\r\n" in buf:
                        head, _, rest = buf.partition(b"\r\n\r\n")
                        length = 0
                        for line in head.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":")[1].strip())
                        if len(rest) >= length:
                            break
                seen["raw"] = buf
                handler(conn, self._stop)
            finally:
                conn.close()

    server = Capturing(handler)
    try:
        response = transport.post(
            server.url, b'{"hello":"world"}',
            {"X-Arion-Delivery-Id": "whd_test"}, 10.0, 8192,
        )
    finally:
        server.close()

    assert response.status_code == 204
    assert b"POST" in seen["raw"]
    assert b'{"hello":"world"}' in seen["raw"]
    assert b"whd_test" in seen["raw"]


def test_slow_but_within_budget_response_succeeds(transport):
    """A response slower than nothing but inside the budget must NOT fail."""

    def handler(conn, _stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
        time.sleep(0.5)
        conn.sendall(b"done")

    server = RawServer(handler)
    try:
        response = transport.post(server.url, b"{}", {}, 10.0, 8192)
    finally:
        server.close()

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 3. bounded response body
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("max_bytes", [512, 4096, 8192])
def test_response_body_read_is_bounded(transport, max_bytes):
    """An enormous response must never be fully consumed into memory."""
    payload_size = 5_000_000

    def handler(conn, _stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n"
                     % payload_size)
        try:
            conn.sendall(b"X" * payload_size)
        except OSError:
            return  # transport hung up early, which is the point

    server = RawServer(handler)
    try:
        started = time.monotonic()
        response = transport.post(server.url, b"{}", {}, 10.0, max_bytes)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert response.status_code == 200
    # The diagnostic snippet is separately truncated to 200 chars.
    assert len(response.body_snippet) <= 200
    assert elapsed < 10.0


def test_zero_max_response_bytes_reads_nothing(transport):
    server = RawServer(_serve_ok)
    try:
        response = transport.post(server.url, b"{}", {}, 10.0, 0)
    finally:
        server.close()
    assert response.status_code == 200
    assert response.body_snippet == ""


# --------------------------------------------------------------------------- #
# 4. redirect rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code,reason", [
    (301, b"Moved Permanently"),
    (302, b"Found"),
    (303, b"See Other"),
    (307, b"Temporary Redirect"),
    (308, b"Permanent Redirect"),
])
def test_redirects_are_never_followed(transport, code, reason):
    """A signed body must not be replayed to an unauthorized origin."""
    attempts: list[int] = []

    def handler(conn, _stop):
        attempts.append(1)
        conn.sendall(
            b"HTTP/1.1 %d %s\r\nLocation: https://evil.example.com/x\r\n"
            b"Content-Length: 0\r\n\r\n" % (code, reason)
        )

    server = RawServer(handler)
    try:
        response = transport.post(server.url, b"{}", {}, 10.0, 8192)
    finally:
        server.close()

    # Returned as a classifiable 3xx outcome, and the transport made exactly
    # one request - it did not chase the Location header.
    assert response.status_code == code
    assert response.ok is False
    assert len(attempts) == 1


# --------------------------------------------------------------------------- #
# 5. HTTPS / SSRF policy is unchanged
# --------------------------------------------------------------------------- #


ALLOWED = "https://hooks.example.com"


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",
    "http://localhost:9000/x",
    "https://127.0.0.1/x",
    "https://192.168.1.10/x",
    "https://evil.example.com/x",
    "https://user:pw@hooks.example.com/x",
    "ftp://hooks.example.com/x",
    "",
])
def test_https_only_policy_is_unchanged(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url, {ALLOWED})


def test_allowlisted_https_url_still_accepted():
    assert validate_webhook_url(f"{ALLOWED}/endpoint", {ALLOWED}) == ALLOWED


def test_address_screening_still_rejects_loopback():
    """With screening ON (the production default), loopback is refused."""
    screening_transport = StdlibWebhookTransport()
    server = RawServer(_serve_ok)
    try:
        with pytest.raises(WebhookTransportError) as exc:
            screening_transport.post(server.url, b"{}", {}, 5.0, 8192)
    finally:
        server.close()

    # Non-retryable: a private destination will not become public on retry.
    assert exc.value.retryable is False
    assert "non-public" in str(exc.value)


def test_connection_refused_is_retryable_and_sanitized(transport):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing is listening now

    with pytest.raises(WebhookTransportError) as exc:
        transport.post(f"http://127.0.0.1:{port}/", b"{}", {}, 5.0, 8192)

    assert exc.value.retryable is True
    assert "transport error" in str(exc.value)
