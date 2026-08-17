"""UrlBoundary + http.get capability tests (ADR-018, Phase C).

UrlBoundary (policy layer):
- explicit allowed-host/origin configuration;
- fail-closed when no 'url' boundary exists;
- rejects malformed URLs, embedded credentials, non-HTTP(S) schemes;
- enforces the host/origin allowlist.

HttpGetCapability (capability layer):
- injectable HTTP transport (no external network in the suite);
- redirects cannot escape the configured origin;
- bounded response size + bounded timeout;
- policy validates the boundary; the capability performs the request.
"""

import pytest

from arion.capabilities.http import (
    CapabilityError,
    FakeTransport,
    HttpGetCapability,
    HttpResponse,
    StdlibHttpTransport,
    UrlBoundary,
)
from arion.orchestration.authz import AuthorizationRequest, ResourcePolicy


# ---------------------------------------------------------------------------
# UrlBoundary semantics
# ---------------------------------------------------------------------------


def test_url_boundary_allow_deny():
    b = UrlBoundary(allowed_origins={"https://allowed.example.com", "http://api.example.org:8080"})
    assert b.allows("https://allowed.example.com/data.json")
    assert b.allows("https://allowed.example.com/deep/path?q=1")
    assert b.allows("http://api.example.org:8080/v1/x")
    # host allowlist enforced
    assert not b.allows("https://evil.example.com/x")
    assert not b.allows("http://allowed.example.com/x")  # scheme differs (https only)
    assert not b.allows("https://allowed.example.org/x")  # host differs


def test_url_boundary_host_case_and_default_port_normalized():
    b = UrlBoundary(allowed_origins={"HTTPS://EXAMPLE.COM:443"})
    assert b.allows("https://example.com/x")
    assert b.allows("https://Example.COM/x")  # host case-insensitive
    b2 = UrlBoundary(allowed_origins={"http://example.com:80"})
    assert b2.allows("http://example.com/x")  # default port normalized


def test_url_boundary_rejects_malformed_urls():
    b = UrlBoundary(allowed_origins={"https://allowed.example.com"})
    assert not b.allows("not a url")
    assert not b.allows("")
    assert not b.allows("https://")           # no host
    assert not b.allows("https:///path")      # no host


def test_url_boundary_rejects_credentials():
    b = UrlBoundary(allowed_origins={"https://allowed.example.com"})
    assert not b.allows("https://user:pass@allowed.example.com/x")
    assert not b.allows("https://token@allowed.example.com/x")


def test_url_boundary_rejects_non_http_schemes():
    b = UrlBoundary(allowed_origins={"https://allowed.example.com"})
    assert not b.allows("ftp://allowed.example.com/x")
    assert not b.allows("file:///etc/passwd")
    assert not b.allows("javascript:alert(1)")


def test_missing_url_boundary_fails_closed():
    policy = ResourcePolicy(allowed_scopes={"http:get"})  # no boundaries
    request = AuthorizationRequest(
        actor=__import__("arion.orchestration.authz", fromlist=["Actor"]).Actor.agent("system"),
        task_id="t", step_index=0, capability="http.get", action="get", scope="http:get",
        params={"url": "https://allowed.example.com/x"}, resource="https://allowed.example.com/x",
        resource_kind="url", risk="low", side_effects="read_only",
    )
    decision = policy.decide(request)
    assert decision.outcome.value == "deny"
    assert "no resource boundary" in decision.reason


def test_url_boundary_enforced_by_policy():
    policy = ResourcePolicy(
        allowed_scopes={"http:get"},
        boundaries={"url": UrlBoundary(allowed_origins={"https://allowed.example.com"})},
    )

    def req(url):
        return AuthorizationRequest(
            actor=__import__("arion.orchestration.authz", fromlist=["Actor"]).Actor.agent("system"),
            task_id="t", step_index=0, capability="http.get", action="get", scope="http:get",
            params={"url": url}, resource=url, resource_kind="url",
            risk="low", side_effects="read_only",
        )

    assert policy.decide(req("https://allowed.example.com/data")).outcome.value == "allow"
    assert policy.decide(req("https://evil.example.com/data")).outcome.value == "deny"
    assert policy.decide(req("https://user:pass@allowed.example.com/x")).outcome.value == "deny"
    assert policy.decide(req("ftp://allowed.example.com/x")).outcome.value == "deny"


# ---------------------------------------------------------------------------
# HttpGetCapability: injectable transport
# ---------------------------------------------------------------------------


def _capability(routes=None, allowed_origins=None):
    routes = routes or {}
    return HttpGetCapability(
        transport=FakeTransport(routes),
        allowed_origins=allowed_origins or {"https://allowed.example.com"},
    )


def test_http_get_success():
    cap = _capability(routes={
        "https://allowed.example.com/data": HttpResponse(status=200, headers={"content-type": "text/plain"}, body="hello"),
    })
    out = cap.execute("get", {"url": "https://allowed.example.com/data"})
    assert out["status"] == 200
    assert out["body"] == "hello"
    assert out["url"] == "https://allowed.example.com/data"


def test_http_get_rejects_non_http_url_at_capability():
    cap = _capability()
    with pytest.raises(CapabilityError, match="scheme"):
        cap.execute("get", {"url": "ftp://allowed.example.com/x"})
    with pytest.raises(CapabilityError, match="credentials"):
        cap.execute("get", {"url": "https://user:pass@allowed.example.com/x"})
    with pytest.raises(CapabilityError, match="malformed"):
        cap.execute("get", {"url": "not-a-url"})


def test_http_redirect_escape_denied():
    """A redirect from the allowed origin to an outside host is DENIED and the
    target is never fetched."""
    routes = {
        "https://allowed.example.com/start": HttpResponse(
            status=302, headers={"location": "https://evil.example.com/payload"}, body=""),
        "https://evil.example.com/payload": HttpResponse(status=200, headers={}, body="pwned"),
    }
    cap = _capability(routes=routes)
    with pytest.raises(CapabilityError, match="redirect escaped"):
        cap.execute("get", {"url": "https://allowed.example.com/start"})
    # the escaped target was never requested
    assert "https://evil.example.com/payload" not in cap.transport.calls


def test_http_same_origin_redirect_allowed():
    routes = {
        "https://allowed.example.com/start": HttpResponse(
            status=301, headers={"location": "/moved"}, body=""),
        "https://allowed.example.com/moved": HttpResponse(status=200, headers={}, body="ok"),
    }
    cap = _capability(routes=routes)
    out = cap.execute("get", {"url": "https://allowed.example.com/start"})
    assert out["status"] == 200 and out["body"] == "ok"
    assert "https://allowed.example.com/moved" in cap.transport.calls


def test_http_redirect_without_allowlist_fails_closed():
    """With no configured allowed_origins, ANY redirect is denied (same-origin
    redirects still allowed)."""
    routes = {
        "https://allowed.example.com/start": HttpResponse(
            status=302, headers={"location": "https://other.example.com/x"}, body=""),
    }
    cap = HttpGetCapability(transport=FakeTransport(routes), allowed_origins=None)
    with pytest.raises(CapabilityError, match="redirect"):
        cap.execute("get", {"url": "https://allowed.example.com/start"})


def test_http_response_size_bounded():
    cap = _capability(routes={
        "https://allowed.example.com/big": HttpResponse(status=200, headers={}, body="x" * 5000),
    })
    cap.max_bytes = 1000
    with pytest.raises(CapabilityError, match="exceeded"):
        cap.execute("get", {"url": "https://allowed.example.com/big"})


def test_http_timeout_bounded():
    class SlowTransport:
        calls = []

        def get(self, url, timeout, max_bytes):
            self.calls.append(url)
            raise TimeoutError(f"timed out after {timeout}s")

    cap = HttpGetCapability(transport=SlowTransport(), timeout=1.0)
    with pytest.raises(CapabilityError, match="timed out"):
        cap.execute("get", {"url": "https://allowed.example.com/x"})


def test_stdlib_transport_declares_containment():
    """The real transport exists (no network calls made here) and enforces the
    same interface contract; bounds are declared."""
    t = StdlibHttpTransport()
    assert hasattr(t, "get")
    assert t.timeout > 0 and t.max_bytes > 0


def test_http_get_action_spec_metadata():
    cap = _capability()
    spec = cap.actions[0]
    assert spec.name == "get"
    assert spec.required_scope == "http:get"
    assert spec.resource_kind == "url" and spec.resource_param == "url"
    assert spec.side_effects == "read_only" and spec.risk == "low"
    assert spec.param_schema["url"]["required"] is True
    assert spec.default_verification == {"policy": "schema_keys", "args": {"keys": ["status", "body"]}}
