"""filesystem.append execution + restart semantics (ADR-020, Phase D).

- successful append: deterministic postcondition (append_verified);
- failure (binary/dir target or injected): attempted once, mutation failure
  recorded, recovery required, restart does not retry, task never silently
  becomes successful;
- approval restart: process A queue -> process B approve -> restart engine ->
  live re-authorization -> append exactly once -> verify;
- stale approval (security-relevant dimension changed): append does not run.
"""

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
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
from arion.state.approvals import ApprovalStatus
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class AppendPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="append notes", capability="filesystem.append",
                     action="append", scope="filesystem:write",
                     params={"path": "notes.txt", "content": " world", "create": False},
                     verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


class FailingAppend(FilesystemAppendCapability):
    """Injected non-retry-safe failure: mutate, then fail."""

    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        super().execute(action, dict(params))
        raise CapabilityError("fsync failed after append")


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, append_cap=None, ttl_seconds=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    registry.register(append_cap or FilesystemAppendCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = AppendPlanner()
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
        policy=_policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm, approval_ttl_seconds=ttl_seconds,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "asandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    return sb


def _approve(engine, gid):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")


def test_append_success_verified_deterministically(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "ok.db", sb)
    gid = engine.submit_goal("append notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    task = gm.task_history(gid)[-1]
    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.succeeded" in kinds and "verification.passed" in kinds
    assert kinds.count("mutation.attempted") == 1
    engine.storage.close()


def test_append_failure_recovery_required_restart_no_retry(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "fail.db"
    fail_cap = FailingAppend(sb)
    engine_a, gm_a, storage_a, _ = _engine(db, sb, append_cap=fail_cap)
    gid = engine_a.submit_goal("append notes").id
    _approve(engine_a, gid)
    engine_a.run_goal(gid)
    task_a = gm_a.task_history(gid)[-1]
    assert task_a.status == TaskStatus.FAILED
    assert "recovery required" in (task_a.error or "")
    assert fail_cap.calls == 1  # attempted once, never retried
    # partial append happened before the failure
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"
    kinds = [e.kind for e in storage_a.list_events()]
    assert all(k in kinds for k in ("mutation.attempted", "mutation.failed", "mutation.requires_recovery"))
    engine_a.storage.close()

    # restart: recovery gate holds; the task is terminal; no retry
    engine_b, gm_b, storage_b, _ = _engine(db, sb)
    rec = engine_b.recovery_store.list_recoveries()[0]
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED
    old = storage_b.load_task(task_a.id)
    assert old.status == TaskStatus.FAILED and "recovery required" in (old.error or "")
    kinds_b = [e.kind for e in storage_b.list_events() if e.task_id == task_a.id]
    assert kinds_b.count("mutation.attempted") == 1
    # task never silently becomes successful after restart
    assert old.status == TaskStatus.FAILED
    engine_b.storage.close()


def test_append_approval_survives_restart_exactly_once(tmp_path):
    """Process A queues -> exits; process B approves -> fresh engine resumes ->
    live re-authorization -> append exactly once -> verified."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "restart.db"

    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    gid = engine_a.submit_goal("append notes").id
    engine_a.run_goal(gid)
    req = engine_a.approval_store.list_requests()[0]
    assert req.status == ApprovalStatus.PENDING
    engine_a.storage.close()  # process exits

    engine_b, gm_b, storage_b, _ = _engine(db, sb)  # independent approval
    engine_b.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_b.storage.close()

    engine_c, gm_c, storage_c, _ = _engine(db, sb)  # restart
    final = engine_c.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"  # exactly once
    kinds = [e.kind for e in storage_c.list_events()]
    assert kinds.count("mutation.attempted") == 1
    assert "task.approval.resumed" in kinds and "verification.passed" in kinds
    engine_c.storage.close()


def test_append_stale_approval_after_security_dimension_change(tmp_path):
    """Change one security-relevant dimension (create) after approval: the
    append does NOT execute on resume."""
    sb = _sandbox(tmp_path)
    (sb / "notes.txt").unlink()
    engine, gm, storage, _ = _engine(tmp_path / "stale.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    task = gm.task_history(gid)[-1]
    task.steps[0].params["create"] = True
    engine.storage.save_task(task)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # fresh approval demanded
    assert not (sb / "notes.txt").exists()  # no append
    assert "mutation.attempted" not in [e.kind for e in storage.list_events()]
    engine.storage.close()


def test_append_never_executes_twice_on_repeated_resume(tmp_path):
    """Repeated run_goal on a completed append goal: no duplicate append."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "twice.db", sb)
    gid = engine.submit_goal("append notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    engine.run_goal(gid)
    engine.run_goal(gid)
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"
    assert [e.kind for e in storage.list_events()].count("mutation.attempted") == 1
    engine.storage.close()
