import json
import os
import hmac
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from arion.orchestration.engine import ArionEngine
from arion.orchestration.authz import Actor, ApprovalOutcome

_MAX_BODY_BYTES = 8192


class APIConfigError(Exception):
    pass


def load_api_tokens() -> dict[str, Actor]:
    """Parse ARION_API_TOKENS env var into a token -> Actor map.
    
    Syntax: token:kind:name,token2:kind2:name2
    Example: secret123:user:alice,secret456:agent:automation
    """
    raw = os.environ.get("ARION_API_TOKENS", "")
    if not raw.strip():
        return {}
    
    tokens = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        segments = part.split(":", 2)
        if len(segments) != 3:
            raise APIConfigError("Malformed token config (expected token:kind:name)")
        token, kind, name = segments
        if not token or not kind or not name:
            raise APIConfigError("Token, kind, and name must be non-empty")
        if token in tokens:
            raise APIConfigError("Duplicate token defined in configuration")
        tokens[token] = Actor(kind=kind, name=name)
        
    return tokens


class ApprovalAPIHandler(BaseHTTPRequestHandler):
    # These must be set on the class or instance before handling requests.
    engine: ArionEngine
    tokens: dict[str, Actor]

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

    def _authenticate(self) -> Actor | None:
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return None
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        
        provided_token = parts[1]
        # Constant-time comparison
        for valid_token, actor in self.tokens.items():
            if hmac.compare_digest(provided_token, valid_token):
                return actor
        return None

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
                
            if path.startswith("/api/v1/approvals/"):
                approval_id = path.split("/")[-1]
                if not approval_id:
                    return self._send_error(400, "missing approval id")
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
                
            parts = path.split("/")
            if len(parts) == 6 and path.startswith("/api/v1/approvals/") and path.endswith("/resolve"):
                approval_id = parts[4]
                if not approval_id:
                    return self._send_error(400, "missing approval id")
                
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
