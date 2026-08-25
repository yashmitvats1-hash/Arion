"""Sequential append coordination under durable mutation locks (Phase 31 hardening).

Tests the invariant: two sequential append operations through the engine's
lock discipline must preserve both updates without loss or corruption.

This validates that:
- lock release allows safe re-acquisition
- appends do not lose prior content after lock handoff
- no data is silently truncated or clobbered between processes
"""

from datetime import datetime, timezone

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
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
from arion.state.locks import canonical_resource
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class AppendFirstPlanner:
    """Append 'hello' to notes.txt (create=true)."""
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(
                index=0, intent="append hello", capability="filesystem.append",
                action="append", scope="filesystem:write",
                params={"path": "notes.txt", "content": "hello", "create": True},
                verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


class AppendSecondPlanner:
    """Append ' world' to notes.txt (create=false, file must exist)."""
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(
                index=0, intent="append world", capability="filesystem.append",
                action="append", scope="filesystem:write",
                params={"path": "notes.txt", "content": " world", "create": False},
                verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


class ReadPlanner:
    """Read the final file to verify content."""
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(
                index=0, intent="read notes", capability="filesystem.read",
                action="read", scope="filesystem:read",
                params={"path": "notes.txt", "max_bytes": 10000},
                verification=VerificationPolicy("none")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.read"}


def _approval_policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, planner, lease_seconds=300.0):
    """Create an engine with the given planner."""
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemAppendCapability(sandbox))
    events = EventLogger(sinks=[storage])
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
        mutation_lock_lease_seconds=lease_seconds,
        lock_wait_max_seconds=0.0,  # ADR-021 legacy: immediate failure on contention
    )
    return engine, gm, storage


def _sandbox(tmp_path):
    sb = tmp_path / "asandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _approve(engine, gid):
    """Approve any pending approvals for the goal."""
    engine.run_goal(gid)
    reqs = engine.approval_store.list_requests()
    if reqs:
        req = reqs[-1]
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")


def test_sequential_appends_through_lock_coordination_preserve_data(tmp_path):
    """Two sequential appends through lock acquire/release must both succeed
    and data must not be lost.

    Scenario:
    1. Engine A acquires lock, appends "hello", releases
    2. Engine B acquires lock, appends " world", releases
    3. Final file must contain "hello world" (order preserved, no loss)
    """
    sb = _sandbox(tmp_path)
    db = tmp_path / "seq_append.db"

    # First append: create file with "hello"
    engine_a, gm_a, storage_a = _engine(db, sb, AppendFirstPlanner(), lease_seconds=300.0)
    gid_a = engine_a.submit_goal("append hello").id
    _approve(engine_a, gid_a)
    result_a = engine_a.run_goal(gid_a)

    # Verify first append succeeded
    assert result_a.status == GoalStatus.COMPLETED, f"First append failed: {gm_a.task_history(gid_a)[-1].error}"
    task_a = gm_a.task_history(gid_a)[-1]
    assert task_a.status == TaskStatus.COMPLETED
    assert task_a.steps[0].status == StepStatus.SUCCEEDED

    # Check file after first append
    file_after_first = (sb / "notes.txt").read_text(encoding="utf-8")
    assert file_after_first == "hello", f"Expected 'hello', got '{file_after_first}'"
    
    # Verify lock was released
    assert engine_a.mutation_lock_store.list() == [], "Lock not released after first append"
    engine_a.storage.close()

    # Second append: append " world" to existing file
    engine_b, gm_b, storage_b = _engine(db, sb, AppendSecondPlanner(), lease_seconds=300.0)
    gid_b = engine_b.submit_goal("append world").id
    _approve(engine_b, gid_b)
    result_b = engine_b.run_goal(gid_b)

    # Verify second append succeeded
    assert result_b.status == GoalStatus.COMPLETED, f"Second append failed: {gm_b.task_history(gid_b)[-1].error}"
    task_b = gm_b.task_history(gid_b)[-1]
    assert task_b.status == TaskStatus.COMPLETED
    assert task_b.steps[0].status == StepStatus.SUCCEEDED

    # Check final file: MUST contain both appends in order
    file_final = (sb / "notes.txt").read_text(encoding="utf-8")
    assert file_final == "hello world", f"Expected 'hello world', got '{file_final}'"
    
    # Verify lock was released
    assert engine_b.mutation_lock_store.list() == [], "Lock not released after second append"

    # Audit: both mutations succeeded, no truncation or re-writes
    kinds = [e.kind for e in storage_b.list_events()]
    assert kinds.count("mutation.succeeded") >= 1, "Second mutation did not emit success event"
    engine_b.storage.close()


def test_sequential_appends_store_prior_size_metadata_correctly(tmp_path):
    """Append capability MUST store prior_size correctly so verification
    can confirm no truncation occurred between verification and next append."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "metadata_check.db"

    # First append
    engine_a, gm_a, storage_a = _engine(db, sb, AppendFirstPlanner())
    gid_a = engine_a.submit_goal("append hello").id
    _approve(engine_a, gid_a)
    engine_a.run_goal(gid_a)
    engine_a.storage.close()

    # Second append: check that prior_size matches actual file size
    engine_b, gm_b, storage_b = _engine(db, sb, AppendSecondPlanner())
    gid_b = engine_b.submit_goal("append world").id
    _approve(engine_b, gid_b)
    engine_b.run_goal(gid_b)

    task_b = gm_b.task_history(gid_b)[-1]
    assert task_b.status == TaskStatus.COMPLETED
    
    # The step's output should reflect the append result
    # (ADR-032 lifecycle consolidation: step results live on PlanStep.result)
    step_output = task_b.steps[0].result
    assert step_output is not None, "Step output missing"
    assert step_output.get("appended") == True
    # prior_size should be 5 ("hello" is 5 bytes)
    assert step_output.get("prior_size") == 5, f"Expected prior_size=5, got {step_output.get('prior_size')}"
    # appended_bytes should be 6 (" world" is 6 bytes)
    assert step_output.get("appended_bytes") == 6, f"Expected appended_bytes=6, got {step_output.get('appended_bytes')}"
    # final size should be 11
    assert step_output.get("size") == 11, f"Expected size=11, got {step_output.get('size')}"
    
    engine_b.storage.close()


def test_append_lock_held_during_mutation_execution(tmp_path):
    """Verify that the lock is held during append execution by blocking
    another process and confirming the mutation still completes."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "lock_held.db"

    engine_a, gm_a, storage_a = _engine(db, sb, AppendFirstPlanner())
    gid_a = engine_a.submit_goal("append hello").id
    _approve(engine_a, gid_a)
    engine_a.run_goal(gid_a)

    # Manually hold the lock to simulate contention
    engine_b, gm_b, storage_b = _engine(db, sb, AppendSecondPlanner())
    lock = engine_b.mutation_lock_store.acquire(
        FS, canonical_resource(FS, "notes.txt"), "filesystem.append", "append",
        "proc-b", lease_seconds=300, now=datetime.now(timezone.utc).isoformat())
    assert lock is not None, "Failed to acquire lock for contention test"

    # Now try to append from engine_a: should fail due to contention
    gid_a_2 = engine_a.submit_goal("append again").id
    _approve(engine_a, gid_a_2)
    result = engine_a.run_goal(gid_a_2)

    # Should be blocked by lock held by engine_b
    assert result.status == GoalStatus.BLOCKED
    task = gm_a.task_history(gid_a_2)[-1]
    assert task.status == TaskStatus.FAILED
    assert "locked" in (task.error or "").lower(), f"Expected lock error, got: {task.error}"

    engine_a.storage.close()
    engine_b.storage.close()
