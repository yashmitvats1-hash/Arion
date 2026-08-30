import json
import os
import threading
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

from arion.interfaces.approval_api import ApprovalAPIHandler, load_api_tokens, APIConfigError
from arion.orchestration.engine import ArionEngine
from arion.orchestration.authz import Actor, ResourcePolicy, RelativePathBoundary
from arion.state.store import SQLiteStorage
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.observability.events import EventLogger
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.write import FilesystemWriteCapability
from arion.orchestration.authz import PendingApprovalHandler
from arion.state.approvals import ApprovalStatus

class DummyPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        from arion.state.models import PlanStep, VerificationPolicy
        return [
            PlanStep(index=0, intent="write file", capability="filesystem.write", action="write",
                     scope="filesystem:write",
                     params={"path": "out.txt", "content": "secret_data"},
                     verification=VerificationPolicy("schema_keys", {"keys": ["path"]})),
        ]
    def required_capabilities(self, goal_description):
        return {"filesystem.write"}

def _setup_engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DummyPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(
            allowed_scopes={"filesystem:read", "filesystem:write"},
            risk_approve={"high", "medium"}, risk_deny=set(),
            boundaries={"filesystem:path": RelativePathBoundary()}
        ),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage

class APIServer:
    def __init__(self, engine, tokens):
        self.engine = engine
        self.tokens = tokens
        self._httpd = None

    def start(self):
        class Handler(ApprovalAPIHandler):
            pass
        Handler.engine = self.engine
        Handler.tokens = self.tokens

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def request(self, method, path, headers=None, data=None):
        url = self.base_url + path
        req = urllib.request.Request(url, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req.data = body
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(len(body)))
            
        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                return resp.status, json.loads(resp_body) if resp_body else None
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            return e.code, json.loads(resp_body) if resp_body else None

@pytest.fixture
def api_env(tmp_path, sandbox):
    db_path = tmp_path / "api.db"
    engine, gm, storage = _setup_engine(db_path, sandbox)
    tokens = {
        "test-token-alice": Actor(kind="user", name="alice"),
        "test-token-bob": Actor(kind="user", name="bob"),
    }
    server = APIServer(engine, tokens)
    server.start()
    yield engine, server
    server.stop()
    storage.close()

def test_load_api_tokens(monkeypatch):
    monkeypatch.setenv("ARION_API_TOKENS", "secret1:user:alice, secret2:agent:bob")
    tokens = load_api_tokens()
    assert len(tokens) == 2
    assert tokens["secret1"] == Actor(kind="user", name="alice")
    assert tokens["secret2"] == Actor(kind="agent", name="bob")
    
    monkeypatch.setenv("ARION_API_TOKENS", "")
    assert load_api_tokens() == {}

def test_load_api_tokens_malformed(monkeypatch):
    monkeypatch.setenv("ARION_API_TOKENS", "bad_format")
    with pytest.raises(APIConfigError):
        load_api_tokens()
        
    monkeypatch.setenv("ARION_API_TOKENS", "dup:u:a,dup:u:b")
    with pytest.raises(APIConfigError):
        load_api_tokens()

def test_health(api_env):
    _, server = api_env
    status, body = server.request("GET", "/health")
    assert status == 200
    assert body["status"] == "ok"

def test_auth_rejection(api_env):
    _, server = api_env
    # Missing token
    status, _ = server.request("GET", "/api/v1/approvals")
    assert status == 401
    
    # Wrong scheme
    status, _ = server.request("GET", "/api/v1/approvals", headers={"Authorization": "Basic test-token-alice"})
    assert status == 401
    
    # Unknown token
    status, _ = server.request("GET", "/api/v1/approvals", headers={"Authorization": "Bearer bad-token"})
    assert status == 401

def test_approvals_list_and_show(api_env):
    engine, server = api_env
    
    # Submit goal to create pending approval
    goal = engine.submit_goal("write to out.txt")
    engine.run_goal(goal.id)
    
    headers = {"Authorization": "Bearer test-token-alice"}
    
    # List approvals
    status, body = server.request("GET", "/api/v1/approvals", headers=headers)
    assert status == 200
    assert len(body["approvals"]) == 1
    approval = body["approvals"][0]
    app_id = approval["approval_id"]
    assert approval["capability"] == "filesystem.write"
    
    # Show approval
    status, body = server.request("GET", f"/api/v1/approvals/{app_id}", headers=headers)
    assert status == 200
    assert body["approval_id"] == app_id
    # Assert secret 'content' is not in the approval body directly or in fingerprint
    assert "content" not in body
    assert "content" not in body.get("fingerprint", {}).get("security_relevant_params", {})
    assert "content" in body.get("params_keys", [])

def test_resolve_approval(api_env):
    engine, server = api_env
    goal = engine.submit_goal("write to out.txt")
    engine.run_goal(goal.id)
    
    reqs = engine.approval_store.list_requests()
    app_id = reqs[0].approval_id
    
    headers = {"Authorization": "Bearer test-token-alice"}
    data = {"outcome": "approved"}
    
    # Actor shouldn't be overridden by payload if maliciously sent
    data["actor"] = "malicious"
    
    status, body = server.request("POST", f"/api/v1/approvals/{app_id}/resolve", headers=headers, data=data)
    assert status == 200
    assert body["status"] == "approved"
    assert body["decision_actor"] == "user:alice"  # Extracted from token, not payload

    # Verify task resumed and completed
    goal = engine.run_goal(goal.id)
    from arion.state.models import GoalStatus
    assert goal.status == GoalStatus.COMPLETED

def test_resolve_approval_denied(api_env):
    engine, server = api_env
    goal = engine.submit_goal("write to out.txt")
    engine.run_goal(goal.id)
    
    reqs = engine.approval_store.list_requests()
    app_id = reqs[0].approval_id
    
    headers = {"Authorization": "Bearer test-token-bob"}
    data = {"outcome": "denied"}
    
    status, body = server.request("POST", f"/api/v1/approvals/{app_id}/resolve", headers=headers, data=data)
    assert status == 200
    assert body["status"] == "denied"
    assert body["decision_actor"] == "user:bob"

def test_duplicate_resolution_idempotency(api_env):
    engine, server = api_env
    goal = engine.submit_goal("write to out.txt")
    engine.run_goal(goal.id)
    
    app_id = engine.approval_store.list_requests()[0].approval_id
    headers = {"Authorization": "Bearer test-token-alice"}
    
    status, body = server.request("POST", f"/api/v1/approvals/{app_id}/resolve", headers=headers, data={"outcome": "approved"})
    assert status == 200
    
    # Duplicate same outcome -> idempotent, succeeds returning state
    status, body = server.request("POST", f"/api/v1/approvals/{app_id}/resolve", headers=headers, data={"outcome": "approved"})
    assert status == 200
    
    # Conflict outcome -> fails 400
    status, body = server.request("POST", f"/api/v1/approvals/{app_id}/resolve", headers=headers, data={"outcome": "denied"})
    assert status == 400
    assert "conflicts with committed" in body["error"]

def test_api_hardening_malformed_json(api_env):
    engine, server = api_env
    headers = {"Authorization": "Bearer test-token-alice", "Content-Type": "application/json"}
    
    # Raw request with bad json
    req = urllib.request.Request(f"{server.base_url}/api/v1/approvals/abc/resolve", method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    body = b"{bad json"
    req.add_header("Content-Length", str(len(body)))
    req.data = body
    
    try:
        urllib.request.urlopen(req)
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_api_hardening_oversized_payload(api_env):
    engine, server = api_env
    headers = {"Authorization": "Bearer test-token-alice"}
    
    # Huge JSON body
    req = urllib.request.Request(f"{server.base_url}/api/v1/approvals/abc/resolve", method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    body = (b'{"outcome": "approved", "junk": "' + b'a' * 10000 + b'"}')
    req.add_header("Content-Length", str(len(body)))
    req.data = body
    
    try:
        urllib.request.urlopen(req)
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 413
