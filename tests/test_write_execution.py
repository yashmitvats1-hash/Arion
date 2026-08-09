"""Non-retry-safe write execution + verification tests (ADR-019).

- a failed mutation is never blindly retried;
- the task enters a durable, explainable recovery-required/failed state;
- recovery requires an explicit new planning/authorization decision
  (a replan creates a NEW plan version + task; the failed one is terminal);
- restart does not duplicate the mutation;
- audit distinguishes mutation attempted / succeeded / failed /
  requires-recovery;
- write verification confirms the postcondition (size matches payload)
  without performing another mutation; verification failure does not
  silently retry the write.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
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
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _approval_policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


class FailingWriteCapability(FilesystemWriteCapability):
    """Writes fail AFTER mutating (partial write) or before - injected."""

    def __init__(self, sandbox, mode="raise"):
        super().__init__(sandbox)
        self.mode = mode
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        if self.mode == "raise":
            from arion.capabilities.registry import CapabilityError
            raise CapabilityError("disk full")
        if self.mode == "partial":
            # mutate, then fail (non-retry-safe: must not be re-run)
            super().execute(action, dict(params))
            from arion.capabilities.registry import CapabilityError
            raise CapabilityError("fsync failed after write")
        if self.mode == "badverify":
            out = super().execute(action, dict(params))
            out["size"] += 1  # lie about the size -> verification mismatch
            return out
        return super().execute(action, dict(params))


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


def _engine(db_path, sandbox, write_cap=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(write_cap or FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = WritePlanner()
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
        policy=_approval_policy(),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _approve(engine, gid):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)


def test_failed_mutation_not_retried(tmp_path):
    sb = _sandbox(tmp_path)
    fail_cap = FailingWriteCapability(sb, mode="raise")
    engine, gm, storage, registry = _engine(tmp_path / "fr.db", sb, write_cap=fail_cap)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "disk full" in (task.error or "")
    assert fail_cap.calls == 1  # exactly ONE attempt - never retried
    assert "step.retrying" not in [e.kind for e in storage.list_events()]
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.attempted" in kinds
    assert "mutation.failed" in kinds
    assert "mutation.requires_recovery" in kinds
    engine.storage.close()


def test_mutation_failure_durable_and_restart_no_duplicate(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "rr.db"
    fail_cap = FailingWriteCapability(sb, mode="partial")  # mutated THEN failed
    engine_a, gm_a, storage_a, _ = _engine(db, sb, write_cap=fail_cap)
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    engine_a.run_goal(gid)
    task_a = gm_a.task_history(gid)[-1]
    assert task_a.status == TaskStatus.FAILED
    assert "recovery required" in (task_a.error or "")
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"  # partial write happened
    engine_a.storage.close()

    # fresh process: the failed task is terminal and is NEVER re-run; the goal
    # is durably gated on the recovery (ADR-020) - no new plan/approval until
    # an EXPLICIT recovery transition, then a FRESH approval is required
    engine_b, gm_b, storage_b, _ = _engine(db, sb)
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # recovery_required gate
    assert any(b.get("type") == "recovery_required" for b in final.blockers)
    rec = engine_b.recovery_store.list_recoveries()[0]
    engine_b.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # fresh approval requested
    new_task = gm_b.task_history(gid)[-1]
    assert new_task.id != task_a.id
    assert new_task.status == TaskStatus.AWAITING_APPROVAL
    # the failed task stays terminally failed with the recovery explanation
    old = storage_b.load_task(task_a.id)
    assert old.status == TaskStatus.FAILED
    assert "recovery required" in (old.error or "")
    # no duplicate mutation: exactly ONE attempt ever for that task
    kinds = [e.kind for e in storage_b.list_events() if e.task_id == task_a.id]
    assert kinds.count("mutation.attempted") == 1
    assert "mutation.failed" in kinds and "mutation.requires_recovery" in kinds
    engine_b.storage.close()


def test_recovery_requires_new_plan_decision(tmp_path):
    """After a failed mutation, recovery happens ONLY through an explicit
    recovery transition followed by a NEW plan version + NEW task with FRESH
    authorization (ADR-020: recovery is a gate, never an authorization)."""
    sb = _sandbox(tmp_path)
    fail_cap = FailingWriteCapability(sb, mode="raise")
    engine, gm, storage, registry = _engine(tmp_path / "rd.db", sb, write_cap=fail_cap)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    assert gm.task_history(gid)[-1].status == TaskStatus.FAILED
    versions_before = len(gm.plan_history(gid))

    # recovery open: run_goal does NOT replan (durable gate)
    assert engine.run_goal(gid).status == GoalStatus.BLOCKED
    assert len(gm.plan_history(gid)) == versions_before
    rec = engine.recovery_store.list_recoveries()[0]

    # explicit recovery transition -> replan -> new plan version + new task
    engine.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    assert engine.run_goal(gid).status == GoalStatus.BLOCKED  # fresh approval queued
    assert len(gm.plan_history(gid)) == versions_before + 1
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.AWAITING_APPROVAL  # fresh authz required
    assert fail_cap.calls == 1  # the write was never re-attempted
    engine.storage.close()


def test_verification_success_confirms_postcondition(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "vs.db", sb)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    task = gm.task_history(gid)[-1]
    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert "verification.passed" in [e.kind for e in storage.list_events()]
    assert "mutation.succeeded" in [e.kind for e in storage.list_events()]
    engine.storage.close()


def test_verification_mismatch_fails_without_retry(tmp_path):
    sb = _sandbox(tmp_path)
    bad_cap = FailingWriteCapability(sb, mode="badverify")
    engine, gm, storage, registry = _engine(tmp_path / "vm.db", sb, write_cap=bad_cap)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "verification failed" in (task.error or "")
    # the write happened ONCE; verification failed; NO retry of the write
    assert bad_cap.calls == 1
    assert "step.retrying" not in [e.kind for e in storage.list_events()]
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


def test_write_audit_events_bounded(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "ae.db", sb)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.attempted" in kinds
    assert "mutation.succeeded" in kinds
    # bounded detail: no content payload in events
    for e in storage.list_events():
        if e.kind.startswith("mutation."):
            assert "content" not in e.detail or "hello" not in str(e.detail)
    engine.storage.close()
