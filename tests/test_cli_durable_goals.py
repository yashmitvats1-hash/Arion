"""CLI durable-goal tests (ADR-017).

- `arion run` drives the durable goal loop (submits a goal, runs it) and
  reports goal_id + goal status + task outcome clearly.
- `arion goals approve|deny` resolve approval-pending goals through the
  engine seam.
"""

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
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _run(argv, capsys):
    rc = cli_main(argv)
    out = capsys.readouterr().out
    return rc, out


def _task_id(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("task_id: "):
            return line.split("task_id: ")[1].strip()
    raise AssertionError(f"no task_id line in output:\n{out}")


def _goal_id(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("goal_id: "):
            return line.split("goal_id: ")[1].strip()
    raise AssertionError(f"no goal_id line in output:\n{out}")


def test_cli_run_uses_durable_goal_loop(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    rc, out = _run(["run", "summarize this repository", "--db", db], capsys)
    assert rc == 0
    assert "goal executed" in out
    gid = _goal_id(out)
    assert gid.startswith("goal_")
    assert "status: completed" in out
    task_id = _task_id(out)
    assert task_id.startswith("task_")
    assert "[ok] step 0" in out and "[ok] step 1" in out

    # the goal row persisted with a plan version (durable lifecycle)
    rc, out = _run(["goals", "show", gid, "--db", db], capsys)
    assert rc == 0
    assert "status=completed" in out
    assert "plan versions: 1" in out


def _seed_awaiting_goal(db_path, sandbox):
    """Create a goal whose task is awaiting approval (repo.review medium risk)."""
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

        def execute(self, action, params):
            return {"review": "ok", "path": params.get("path")}

    registry.register(ReviewCapability())

    class TwoStepPlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            from arion.state.models import PlanStep, VerificationPolicy

            return [
                PlanStep(index=0, intent="list", capability="filesystem.read", action="list",
                         scope="filesystem:read", params={"path": "."},
                         verification=VerificationPolicy("non_empty")),
                PlanStep(index=1, intent="review", capability="repo.review", action="review",
                         scope="review:run", params={"path": "README.md"},
                         verification=VerificationPolicy("schema_keys", {"keys": ["review"]})),
            ]

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
    goal = engine.submit_goal("review this repository")
    engine.run_goal(goal.id)
    return goal.id


def test_cli_goals_approve_and_deny(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid = _seed_awaiting_goal(db, tmp_path / "repo")

    # pending state is clearly represented
    rc, out = _run(["goals", "show", gid, "--db", db], capsys)
    assert rc == 0
    assert "status=blocked" in out
    assert "approval_pending" in out

    rc, out = _run(["goals", "approve", gid, "--db", db], capsys)
    assert rc == 0
    assert "approved" in out.lower()

    rc, out = _run(["goals", "show", gid, "--db", db], capsys)
    assert "status=active" in out


def test_cli_goals_deny(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid = _seed_awaiting_goal(db, tmp_path / "repo")

    rc, out = _run(["goals", "deny", gid, "--db", db], capsys)
    assert rc == 0
    assert "denied" in out.lower()

    rc, out = _run(["goals", "show", gid, "--db", db], capsys)
    assert "status=active" in out  # approval resolved; task failed durably


def test_cli_goals_approve_json(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    gid = _seed_awaiting_goal(db, tmp_path / "repo")
    rc, out = _run(["goals", "approve", gid, "--json", "--db", db], capsys)
    assert rc == 0
    import json

    data = json.loads(out)
    assert data["status"] == "running"
