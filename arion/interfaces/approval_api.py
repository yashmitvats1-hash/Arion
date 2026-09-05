import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from arion.orchestration.engine import ArionEngine
from arion.orchestration.authz import Actor, ApprovalOutcome
from arion.interfaces.api_authz import (
    APIConfigError,
    ENV_API_TOKENS,
    Privilege,
    TokenRegistry,
    _parse_token_map,
    authorize,
)

_MAX_BODY_BYTES = 8192

# Exact route matching (M6-A.1 / ADR-059 D15 Category B).
#
# The original implementation matched with `startswith` + `split("/")[-1]`,
# which silently collapsed multi-segment paths: `/api/v1/approvals/a/b/c`
# resolved to id "c", and `/api/v1/approvals/../../etc/passwd` to id
# "passwd". A path segment can never contain "/", so the id group excludes
# it and such paths now 404. Segments are NOT URL-decoded: ids are matched
# as raw path segments exactly as before.
_SEGMENT = r"[^/]+"
_RE_APPROVAL_SHOW = re.compile(rf"^/api/v1/approvals/({_SEGMENT})$")
_RE_APPROVAL_RESOLVE = re.compile(rf"^/api/v1/approvals/({_SEGMENT})/resolve$")

# Preserved contractual quirk: a trailing-slash / empty id on the approvals
# path is a 400 ("missing approval id"), not a 404. This covers both
# `/api/v1/approvals/` and `/api/v1/approvals/<id>/`, which the old
# `split("/")[-1]` logic resolved to an empty id. Deeper multi-segment
# paths are NOT preserved - they are the routing defect being fixed.
_RE_APPROVAL_EMPTY_ID = re.compile(rf"^/api/v1/approvals/({_SEGMENT}/)?$")
_RE_RESOLVE_EMPTY_ID = re.compile(r"^/api/v1/approvals//resolve$")


def load_api_tokens() -> dict[str, Actor]:
    """Parse ARION_API_TOKENS env var into a token -> Actor map.

    Syntax: token:kind:name,token2:kind2:name2
    Example: secret123:user:alice,secret456:agent:automation

    M6-A.1: parsing now delegates to the shared `api_authz` grammar rather
    than a second private copy. The signature, return type and raising
    behaviour are unchanged; `APIConfigError` is now the SAME class as
    `api_authz.APIConfigError` (previously two unrelated classes, which made
    `cli.py`'s `except APIConfigError` miss registry errors entirely).
    """
    import os

    return _parse_token_map(
        os.environ.get(ENV_API_TOKENS, ""), variable=ENV_API_TOKENS
    )


class ApprovalAPIHandler(BaseHTTPRequestHandler):
    # These must be set on the class or instance before handling requests.
    engine: ArionEngine
    tokens: dict[str, Actor]
    # Preferred wiring (M6-A.1). When unset, a registry is synthesized from
    # the legacy `tokens` mapping - see `_auth_registry`.
    registry: TokenRegistry | None = None
    # Cache of the registry synthesized from `tokens`, stored as
    # (snapshot_of_tokens, registry) so a reassigned `tokens` is detected.
    _derived_registry: tuple[dict[str, Actor], TokenRegistry] | None = None

    def log_message(self, format: str, *args: Any) -> None:
        # Override to prevent logging Authorization headers or sensitive paths
        pass

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _auth_registry(self) -> TokenRegistry:
        """Return the authoritative TokenRegistry for this request.

        Compatibility bridge (M6-A.1): existing embedders - including
        `tests/test_approval_api.py`, which ADR-059 D15 requires to remain
        unmodified - configure the handler by assigning a plain
        `Handler.tokens = {token: Actor}` mapping. That legacy configuration
        is TRANSLATED into a TokenRegistry here; it is never a second
        authorization implementation. All authentication and privilege
        decisions below go through `api_authz` regardless of which wiring
        was used.
        """
        registry = getattr(self, "registry", None)
        if registry is not None:
            return registry
        # Cache the synthesized registry on the class so it is built once,
        # not per request.
        cls = type(self)
        tokens = getattr(self, "tokens", None) or {}
        derived = cls.__dict__.get("_derived_registry")
        if derived is None or derived[0] != tokens:
            derived = (dict(tokens), TokenRegistry(approver_tokens=tokens))
            cls._derived_registry = derived
        return derived[1]

    def _authenticate(self) -> Actor | None:
        """Authenticate and require APPROVER via the single enforcement point.

        Approval routes require only APPROVER. Because ADMIN implies
        APPROVER (ADR-059 D13), an admin credential is also accepted. Every
        authenticated caller therefore holds at least APPROVER, so 403 is
        unreachable on this surface and unauthenticated callers continue to
        receive 401 exactly as before.
        """
        decision = authorize(
            self._auth_registry(),
            self.headers.get("Authorization"),
            Privilege.APPROVER,
        )
        if not decision.ok or decision.context is None:
            return None
        return decision.context.actor

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            
            if path == "/health":
                return self._send_json(200, {"status": "ok"})
                
            actor = self._authenticate()
            if not actor:
                return self._send_error(401, "unauthorized")
                
            if path == "/api/v1/approvals":
                qs = urllib.parse.parse_qs(parsed.query)
                status_filter = qs.get("status", [None])[0]
                store = self.engine.approval_store
                if store is None:
                    return self._send_error(503, "approval store unavailable")
                reqs = store.list_requests(status=status_filter)
                return self._send_json(200, {"approvals": [r.to_dict() for r in reqs]})
                
            if _RE_APPROVAL_EMPTY_ID.match(path):
                return self._send_error(400, "missing approval id")

            show_match = _RE_APPROVAL_SHOW.match(path)
            if show_match:
                approval_id = show_match.group(1)
                store = self.engine.approval_store
                if store is None:
                    return self._send_error(503, "approval store unavailable")
                req = store.get_request(approval_id)
                if req is None:
                    return self._send_error(404, "approval not found")
                return self._send_json(200, req.to_dict())
                
            return self._send_error(404, "not found")
            
        except Exception as e:
            self._send_error(500, "internal server error")

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            
            actor = self._authenticate()
            if not actor:
                return self._send_error(401, "unauthorized")
                
            if _RE_RESOLVE_EMPTY_ID.match(path):
                return self._send_error(400, "missing approval id")

            resolve_match = _RE_APPROVAL_RESOLVE.match(path)
            if resolve_match:
                approval_id = resolve_match.group(1)

                length_str = self.headers.get("Content-Length")
                if not length_str:
                    return self._send_error(411, "length required")
                try:
                    length = int(length_str)
                except ValueError:
                    return self._send_error(400, "invalid length")
                    
                if length > _MAX_BODY_BYTES:
                    return self._send_error(413, "payload too large")
                    
                body = self.rfile.read(length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._send_error(400, "malformed json")
                    
                if not isinstance(data, dict):
                    return self._send_error(400, "expected json object")
                    
                outcome_str = data.get("outcome")
                if outcome_str == "approved":
                    outcome = ApprovalOutcome.APPROVED
                elif outcome_str == "denied":
                    outcome = ApprovalOutcome.DENIED
                else:
                    return self._send_error(400, "invalid outcome")
                    
                try:
                    resolved = self.engine.resolve_approval_request(
                        approval_id=approval_id,
                        outcome=outcome,
                        actor=actor.id
                    )
                    return self._send_json(200, resolved.to_dict())
                except Exception as e:
                    # e.g. ApprovalError
                    return self._send_error(400, str(e))
                    
            return self._send_error(404, "not found")
            
        except Exception as e:
            self._send_error(500, "internal server error")
