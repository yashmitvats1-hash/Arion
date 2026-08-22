"""CLI approval interface tests (ADR-018, Phase B).

- arion approvals list|show|approve|deny [--json]
- operates against the same persistent DB as the engine:
  process A -> approval requested -> exits
  process B -> approvals list -> approve -> exits
  process C -> resume task -> completion
"""

import json

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import PendingApprovalHandler, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _run(argv, capsys):
    rc = cli_main(argv)
    out = capsys.readouterr().out
    return rc, out


def _build_engine(db_path, sandbox):
    """A wired engine (repo.review registered). Process A and C use this."""
    sandbox.mkdir(parents=True, exist_ok=True)
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))

    class ReviewCapability:
        name = "repo.review"
        description = "review"
        actions = [ActionSpec(name="review", description="review", required_scope="review:run",
                              risk="medium", side_effects="read_only", reversible=True,
                              idempotent=True, retry_safe=True,
                              resource_kind=FS, resource_param="path",
                              param_schema={"path": {"type": "string", "required": True}},
                              default_verification={"policy": "schema_keys", "args": {"keys": ["review"]}})]

        def __init__(self):
            self.calls = []

        def execute(self, action, params):
            self.calls.append(dict(params))
            return {"review": "ok", "path": params.get("path")}

    review = ReviewCapability()
    registry.register(review)

    class TwoStepPlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            return [
                PlanStep(index=0, intent="list", capability="filesystem.read", action="list",
                         scope="filesystem:read", params={"path": "."},
                         verification=VerificationPolicy("non_empty")),
                PlanStep(index=1, intent="review", capability="repo.review", action="review",
                         scope="review:run", params={"path": "README.md"},
                         verification=VerificationPolicy("schema_keys", {"keys": ["review"]})),
            ]

        def required_capabilities(self, goal_description):
            return {"filesystem.read", "repo.review"}

    events = EventLogger(sinks=[storage])
    planner = TwoStepPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "review:run"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, review


def _seed_pending(db_path, sandbox, goal_text="review this repository"):
    """Process A: submit goal, run until approval pending, exit."""
    engine, review = _build_engine(db_path, sandbox)
    goal = engine.submit_goal(goal_text)
    engine.run_goal(goal.id)
    req = engine.approval_store.list_requests()[0]
    engine.storage.close()
    return goal.id, req.approval_id, review


def test_cli_approvals_list_show(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid, approval_id, _ = _seed_pending(db, tmp_path / "repo")

    rc, out = _run(["approvals", "list", "--db", db], capsys)
    assert rc == 0
    assert approval_id in out
    assert "pending" in out

    rc, out = _run(["approvals", "list", "--status", "pending", "--db", db], capsys)
    assert rc == 0 and approval_id in out
    rc, out = _run(["approvals", "list", "--status", "approved", "--db", db], capsys)
    assert approval_id not in out

    rc, out = _run(["approvals", "list", "--json", "--db", db], capsys)
    data = json.loads(out)
    assert data[0]["approval_id"] == approval_id
    assert data[0]["status"] == "pending"
    assert data[0]["capability"] == "repo.review"

    rc, out = _run(["approvals", "show", approval_id, "--db", db], capsys)
    assert rc == 0
    assert "repo.review" in out
    assert "README.md" in out

    rc, out = _run(["approvals", "show", approval_id, "--json", "--db", db], capsys)
    data = json.loads(out)
    assert data["approval_id"] == approval_id and data["status"] == "pending"
    assert data["risk"] == "medium" and data["resource"] == "README.md"


def test_cli_approvals_approve_full_flow(tmp_path, capsys, monkeypatch):
    """process A -> request -> exits; process B (CLI) -> list+approve -> exits;
    process C (fresh engine, same DB) -> resume exact step -> completion."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid, approval_id, review = _seed_pending(db, tmp_path / "repo")

    # process B: list + approve via the CLI against the SAME persistent DB
    rc, out = _run(["approvals", "list", "--db", db], capsys)
    assert rc == 0 and approval_id in out
    rc, out = _run(["approvals", "approve", approval_id, "--actor", "user:alice", "--db", db], capsys)
    assert rc == 0
    assert "approved" in out.lower()
    rc, out = _run(["approvals", "show", approval_id, "--json", "--db", db], capsys)
    data = json.loads(out)
    assert data["status"] == "approved"
    assert data["decision_actor"] == "user:alice"

    # process C: a FRESH engine on the same DB resumes the exact step
    engine_c, review_c = _build_engine(db, tmp_path / "repo")
    final = engine_c.run_goal(gid)
    from arion.state.models import GoalStatus
    assert final.status == GoalStatus.COMPLETED
    assert review_c.calls == [{"path": "README.md"}]
    # the approved capability executed exactly once through the queue path
    assert len(review_c.calls) == 1
    engine_c.storage.close()


def test_cli_approvals_deny(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid, approval_id, _ = _seed_pending(db, tmp_path / "repo")

    rc, out = _run(["approvals", "deny", approval_id, "--actor", "user:alice", "--db", db], capsys)
    assert rc == 0
    assert "denied" in out.lower()
    rc, out = _run(["approvals", "show", approval_id, "--json", "--db", db], capsys)
    assert json.loads(out)["status"] == "denied"

    # invalid transition: approve after deny fails closed with clean message
    rc, out = _run(["approvals", "approve", approval_id, "--db", db], capsys)
    assert rc == 1
    assert "conflicts" in out.lower()

    # unknown id
    rc, out = _run(["approvals", "approve", "approval_does_not_exist", "--db", db], capsys)
    assert rc == 1


def test_cli_goals_approve_routes_through_queue(tmp_path, capsys, monkeypatch):
    """`goals approve <goal_id>` still works and resolves the durable record."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid, approval_id, review = _seed_pending(db, tmp_path / "repo")

    rc, out = _run(["goals", "approve", gid, "--db", db], capsys)
    assert rc == 0
    rc, out = _run(["approvals", "show", approval_id, "--json", "--db", db], capsys)
    assert json.loads(out)["status"] == "approved"
