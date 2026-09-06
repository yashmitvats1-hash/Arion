"""M6-A.1 regression tests: Category B hardening of the M6-A approval API.

Covers exactly three things, all tied to the seam ADR-059 D15 deferred:

  1. `APIConfigError` identity - approval_api and api_authz must name the
     SAME class, and `cli._api_command` must therefore catch registry
     configuration errors instead of leaking a traceback.
  2. Exact route matching - multi-segment paths no longer collapse to their
     last segment, while the genuinely contractual edge cases (empty id ->
     400, unknown route -> 404) are preserved byte for byte.
  3. Shared authorization - the approval surface authenticates through
     `api_authz`, so ADMIN (which implies APPROVER) is accepted and unknown
     credentials still receive 401.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from arion.interfaces import api_authz, cli
from arion.interfaces.api_authz import APIConfigError, Privilege, TokenRegistry
from arion.interfaces.approval_api import (
    APIConfigError as ApprovalAPIConfigError,
    ApprovalAPIHandler,
    load_api_tokens,
)
from arion.orchestration.authz import Actor


# ---------------------------------------------------------------------------
# 1. APIConfigError identity + CLI behaviour
# ---------------------------------------------------------------------------


def test_api_config_error_is_a_single_shared_class():
    """The two modules previously defined unrelated exception classes.

    `cli.py` imported the approval_api one and used it to guard
    `TokenRegistry.from_env()`, which raises the api_authz one - so a
    malformed ARION_API_ADMIN_TOKENS escaped as an unhandled traceback.
    """
    assert ApprovalAPIConfigError is APIConfigError


def test_malformed_admin_tokens_is_a_clean_cli_error(monkeypatch, capsys):
    """Regression: this used to raise instead of returning exit code 1."""
    monkeypatch.setenv("ARION_API_TOKENS", "tok:user:alice")
    monkeypatch.setenv("ARION_API_ADMIN_TOKENS", "this-is-malformed")

    class Args:
        host = "127.0.0.1"
        port = 0

    rc = cli._api_command(Args(), engine=object())

    assert rc == 1
    assert "API configuration error" in capsys.readouterr().out


def test_malformed_approver_tokens_is_a_clean_cli_error(monkeypatch, capsys):
    monkeypatch.setenv("ARION_API_TOKENS", "not-a-valid-triple")
    monkeypatch.delenv("ARION_API_ADMIN_TOKENS", raising=False)

    class Args:
        host = "127.0.0.1"
        port = 0

    assert cli._api_command(Args(), engine=object()) == 1
    assert "API configuration error" in capsys.readouterr().out


def test_api_command_defined_before_main_block():
    """`_api_command` used to sit below `if __name__ == "__main__"`."""
    source = open(cli.__file__).read()
    assert source.index("def _api_command") < source.index(
        'if __name__ == "__main__":'
    )


# ---------------------------------------------------------------------------
# load_api_tokens delegates to the shared grammar
# ---------------------------------------------------------------------------


def test_load_api_tokens_uses_shared_grammar(monkeypatch):
    monkeypatch.setenv("ARION_API_TOKENS", "s1:user:alice,s2:agent:automation")
    tokens = load_api_tokens()
    assert tokens == {
        "s1": Actor(kind="user", name="alice"),
        "s2": Actor(kind="agent", name="automation"),
    }


def test_load_api_tokens_rejects_duplicates(monkeypatch):
    monkeypatch.setenv("ARION_API_TOKENS", "s1:user:alice,s1:user:bob")
    with pytest.raises(APIConfigError):
        load_api_tokens()


# ---------------------------------------------------------------------------
# 2 & 3. Routing + authorization, against a live server
# ---------------------------------------------------------------------------


class _StubEngine:
    """Engine stub whose approval_store is absent.

    A 503 therefore proves the request REACHED the approval-store branch,
    i.e. the path matched as an id; 404 proves it did not.
    """

    approval_store = None


@pytest.fixture
def server():
    class Handler(ApprovalAPIHandler):
        pass

    Handler.engine = _StubEngine()
    Handler.registry = TokenRegistry(
        approver_tokens={"approver-tok": Actor(kind="user", name="alice")},
        admin_tokens={"admin-tok": Actor(kind="user", name="root")},
    )
    Handler._derived_registry = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def request(method, path, token="approver-tok", body=None):
        req = urllib.request.Request(base + path, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        if method == "POST":
            data = json.dumps(body or {"outcome": "approved"}).encode()
            req.data = data
            req.add_header("Content-Length", str(len(data)))
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    yield request
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    "path,expected",
    [
        # THE FIX: multi-segment paths no longer collapse to their last
        # segment. Both of these previously matched as an approval id
        # ("c" and "passwd" respectively).
        ("/api/v1/approvals/a/b/c", 404),
        ("/api/v1/approvals/../../etc/passwd", 404),
        # Preserved: reaches the store branch (matched as an id).
        ("/api/v1/approvals", 503),
        ("/api/v1/approvals/abc", 503),
        # Preserved: segments are not URL-decoded, so these stay single ids.
        ("/api/v1/approvals/%2e%2e%2f", 503),
        ("/api/v1/approvals/abc%2Fdef", 503),
        # Preserved contractual quirk: empty id is 400, not 404.
        ("/api/v1/approvals/", 400),
        ("/api/v1/approvals/abc/", 400),
        # Preserved: unknown routes.
        ("/api/v1/approvalsXYZ", 404),
        ("/health/", 404),
        ("/HEALTH", 404),
    ],
)
def test_get_routing_is_exact(server, path, expected):
    assert server("GET", path) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/approvals/a/b/resolve", 404),
        ("/api/v1/approvals//resolve", 400),
        ("/api/v1/approvals/abc/resolve/", 404),
        ("/api/v1/approvals/abc/RESOLVE", 404),
    ],
)
def test_post_routing_is_exact(server, path, expected):
    assert server("POST", path) == expected


def test_health_remains_unauthenticated(server):
    assert server("GET", "/health", token=None) == 200


@pytest.mark.parametrize("token", [None, "bogus-token"])
def test_unknown_credentials_are_401(server, token):
    assert server("GET", "/api/v1/approvals", token=token) == 401


def test_admin_token_satisfies_approver_routes(server):
    """ADMIN implies APPROVER (ADR-059 D13), so 503 not 401/403."""
    assert server("GET", "/api/v1/approvals", token="admin-tok") == 503


def test_legacy_tokens_mapping_still_authenticates():
    """The `Handler.tokens` compatibility bridge must keep working.

    ADR-059 D15 requires tests/test_approval_api.py to remain unmodified,
    and it configures the handler with a plain dict.
    """

    class Handler(ApprovalAPIHandler):
        pass

    Handler.engine = _StubEngine()
    Handler.tokens = {"legacy-tok": Actor(kind="user", name="alice")}
    Handler._derived_registry = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(token):
        req = urllib.request.Request(base + "/api/v1/approvals")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    try:
        assert get("legacy-tok") == 503  # authenticated, reached the store
        assert get("wrong-tok") == 401
        # And the routing fix applies on this wiring too.
        req = urllib.request.Request(base + "/api/v1/approvals/a/b/c")
        req.add_header("Authorization", "Bearer legacy-tok")
        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            code = exc.code
        assert code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
