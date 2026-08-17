"""CLI recovery surface tests (ADR-020, Phase G).

`arion recovery list|show|acknowledge <id>` against the domain/store
interfaces (never raw SQLite), with --json support, bounded secret-free
output, and fail-closed behavior.
"""

import json

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
from arion.state.recovery import RecoveryStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class WritePlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="write notes", capability="filesystem.write", action="write",
                     scope="filesystem:write",
                     params={"path": "notes.txt", "content": "hello", "overwrite": False},
                     verification=VerificationPolicy("write_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


class FailingWrite(FilesystemWriteCapability):
    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        raise CapabilityError("disk full")


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FailingWrite(sandbox))
    events = EventLogger(sinks=[storage])
    planner = WritePlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
    )
    return engine, gm, storage


def _seed_recovery(tmp_path):
    """Create one REQUIRED recovery record and return (db, sandbox, recovery_id)."""
    sb = tmp_path / "csandbox"
    sb.mkdir(parents=True, exist_ok=True)
    db = str(tmp_path / "arion.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine.run_goal(gid)
    rec = engine.recovery_store.list_recoveries()[0]
    engine.storage.close()
    return db, sb, rec.recovery_id


def test_cli_recovery_list_and_show(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, rid = _seed_recovery(tmp_path)

    rc, out = _run(["recovery", "list", "--json", "--db", db], capsys)
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["status"] == "required"
    assert data[0]["capability"] == "filesystem.write"
    assert data[0]["action"] == "write"
    assert "content" not in json.dumps(data)  # bounded, secret-free

    rc, out = _run(["recovery", "show", rid, "--db", db], capsys)
    assert rc == 0
    assert rid in out and "required" in out

    rc, out = _run(["recovery", "show", rid, "--json", "--db", db], capsys)
    d = json.loads(out)
    assert d["recovery_id"] == rid and d["status"] == "required"
    assert "content" not in out


def test_cli_recovery_acknowledge(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, rid = _seed_recovery(tmp_path)

    rc, out = _run(["recovery", "acknowledge", rid, "--actor", "user:alice", "--json", "--db", db], capsys)
    assert rc == 0
    d = json.loads(out)
    assert d["status"] == "acknowledged"
    assert d["acknowledged_by"] == "user:alice"

    rc, out = _run(["recovery", "list", "--db", db], capsys)
    assert "acknowledged" in out

    # acknowledging again fails closed
    rc, out = _run(["recovery", "acknowledge", rid, "--db", db], capsys)
    assert rc == 1
    assert "already" in out.lower()


def test_cli_recovery_unknown_id_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, rid = _seed_recovery(tmp_path)
    rc, out = _run(["recovery", "show", "recovery_nope", "--db", db], capsys)
    assert rc == 1
    rc, out = _run(["recovery", "acknowledge", "recovery_nope", "--db", db], capsys)
    assert rc == 1
    assert "unknown" in out.lower()


def _run(argv, capsys):
    rc = cli_main(argv)
    out = capsys.readouterr().out
    return rc, out
