"""ADR-038: approval decision and task state form one durable transition."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from threading import Barrier, Thread

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.approvals import (
    ApprovalError,
    ApprovalRequest,
    ApprovalStatus,
)
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy, new_id
from arion.state.store import SQLiteStorage


class _MediumCapability:
    name = "approval.medium"
    description = "medium approval action"
    actions = [ActionSpec(
        name="run",
        description="run",
        required_scope="approval:run",
        risk="medium",
        side_effects="read_only",
        retry_safe=True,
        param_schema={},
    )]

    def __init__(self):
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        return {"approved": True}


class _Planner:
    def required_capabilities(self, goal_description):
        return {"approval.medium"}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="run approved action",
            capability="approval.medium",
            action="run",
            scope="approval:run",
            params={},
            verification=VerificationPolicy("non_empty"),
            max_attempts=1,
        )]


def _engine(db: Path, *, ttl=None, goal_manager=False, events=None):
    storage = SQLiteStorage(db)
    capability = _MediumCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    planner = _Planner()
    logger = events or EventLogger(sinks=[storage])
    manager = None
    cognitive = None
    if goal_manager:
        cognitive = SQLiteCognitiveStore(db)
        manager = GoalManager(
            storage=storage,
            cognitive_store=cognitive,
            events=logger,
            strategy_selector=StrategySelector(),
            progress_evaluator=DeterministicProgressEvaluator(),
        )
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=logger,
        policy=ResourcePolicy(allowed_scopes={"approval:run"}),
        approval_handler=PendingApprovalHandler(),
        approval_ttl_seconds=ttl,
        goal_manager=manager,
    )
    return engine, storage, capability, cognitive


def _pending(db: Path, **kwargs):
    engine, storage, capability, cognitive = _engine(db, **kwargs)
    if engine.goal_manager is None:
        task = engine.execute_goal("approval test")
        goal_id = task.goal_id
    else:
        goal = engine.submit_goal("approval test")
        goal_id = goal.id
        engine.run_goal(goal_id)
        task = next(
            task for task in engine.goal_manager.task_history(goal_id)
            if task.status is TaskStatus.AWAITING_APPROVAL
        )
    request = storage.list_requests(status="pending")[0]
    return engine, storage, capability, cognitive, task, request, goal_id


def _close(*values) -> None:
    for value in values:
        if value is None:
            continue
        try:
            value.shutdown() if hasattr(value, "shutdown") else value.close()
        except Exception:
            pass


def test_concurrent_conflicting_decisions_have_one_matching_winner(
    tmp_path: Path,
) -> None:
    db = tmp_path / "race.db"
    seed, seed_store, _, cognitive, task, request, _ = _pending(db)
    _close(seed, seed_store, cognitive)
    approve, approve_store, approve_cap, _ = _engine(db)
    deny, deny_store, deny_cap, _ = _engine(db)
    barrier = Barrier(2)
    approve_resolve = approve._resolve_request
    deny_resolve = deny._resolve_request

    def approve_at_barrier(req, outcome, actor):
        barrier.wait()
        return approve_resolve(req, outcome, actor)

    def deny_at_barrier(req, outcome, actor):
        barrier.wait()
        return deny_resolve(req, outcome, actor)

    approve._resolve_request = approve_at_barrier
    deny._resolve_request = deny_at_barrier
    errors: list[Exception] = []

    def decide(engine, outcome):
        try:
            engine.resolve_approval_request(request.approval_id, outcome)
        except Exception as exc:
            errors.append(exc)

    left = Thread(target=decide, args=(approve, ApprovalOutcome.APPROVED))
    right = Thread(target=decide, args=(deny, ApprovalOutcome.DENIED))
    left.start(); right.start(); left.join(); right.join()

    durable_request = approve_store.get_request(request.approval_id)
    durable_task = approve_store.load_task(task.id)
    mirror = next(
        record for record in durable_task.approvals
        if record.get("approval_id") == request.approval_id
    )

    assert len(errors) == 1
    assert isinstance(errors[0], ApprovalError)
    if durable_request.status is ApprovalStatus.APPROVED:
        assert durable_task.status is TaskStatus.RUNNING
        assert mirror["outcome"] == "approved"
    else:
        assert durable_request.status is ApprovalStatus.DENIED
        assert durable_task.status is TaskStatus.FAILED
        assert mirror["outcome"] == "denied"
    _close(approve, approve_store, deny, deny_store)


def test_same_decision_retry_is_idempotent_conflict_is_rejected(tmp_path: Path) -> None:
    engine, storage, _, cognitive, task, request, _ = _pending(
        tmp_path / "repeat.db"
    )
    first = engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED, actor="user:first"
    )
    second = engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED, actor="user:retry"
    )

    assert first.status is ApprovalStatus.APPROVED
    assert second.status is ApprovalStatus.APPROVED
    assert storage.load_task(task.id).status is TaskStatus.RUNNING
    with pytest.raises(ApprovalError, match="conflicts"):
        engine.resolve_approval_request(
            request.approval_id, ApprovalOutcome.DENIED
        )
    _close(engine, storage, cognitive)


def test_failure_after_atomic_commit_leaves_consistent_resumable_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "post-commit.db"
    engine, storage, _, cognitive, task, request, _ = _pending(db)

    class BrokenSink:
        def emit(self, event):
            raise RuntimeError("post-commit observability failure")

    engine.events.add_sink(BrokenSink(), required=True)
    with pytest.raises(RuntimeError, match="observability"):
        engine.resolve_approval_request(
            request.approval_id, ApprovalOutcome.APPROVED
        )

    assert storage.get_request(request.approval_id).status is ApprovalStatus.APPROVED
    assert storage.load_task(task.id).status is TaskStatus.RUNNING
    _close(engine, storage, cognitive)

    resumed, resumed_store, resumed_capability, _ = _engine(db)
    result = resumed.run_task(task.id)
    assert result.status is TaskStatus.COMPLETED
    assert resumed_capability.calls == 1
    _close(resumed, resumed_store)


def test_pending_creation_failure_fails_closed_without_orphan_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, storage, capability, cognitive = _engine(tmp_path / "create.db")

    def fail_create(request):
        raise RuntimeError("approval store unavailable")

    monkeypatch.setattr(storage, "create_request", fail_create)
    task = engine.execute_goal("approval persistence fails")

    assert task.status is TaskStatus.FAILED
    assert task.steps[0].status.value == "failed"
    assert "approval persistence failed" in (task.error or "")
    assert storage.list_requests() == []
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_duplicate_pending_creation_adopts_one_canonical_request(
    tmp_path: Path,
) -> None:
    db = tmp_path / "dedupe.db"
    left = SQLiteStorage(db)
    right = SQLiteStorage(db)
    fingerprint = {"scope": "approval:run", "resource_fingerprint": "abc"}

    def request(identifier):
        return ApprovalRequest(
            approval_id=identifier,
            task_id="task-one",
            step_index=0,
            goal_id="goal-one",
            capability="approval.medium",
            action="run",
            scope="approval:run",
            risk="medium",
            side_effects="read_only",
            resource_kind=None,
            resource=None,
            summary="approve",
            fingerprint=dict(fingerprint),
        )

    barrier = Barrier(2)
    adopted: list[str] = []

    def create(store, candidate):
        barrier.wait()
        adopted.append(store.create_request(candidate).approval_id)

    first = Thread(target=create, args=(left, request(new_id("approval"))))
    second = Thread(target=create, args=(right, request(new_id("approval"))))
    first.start(); second.start(); first.join(); second.join()

    rows = left.list_requests(status="pending")
    assert len(rows) == 1
    assert adopted == [rows[0].approval_id, rows[0].approval_id]
    left.close(); right.close()


def test_elapsed_ttl_cannot_be_approved_without_sweep(tmp_path: Path) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "ttl.db", ttl=0.01
    )
    time.sleep(0.03)

    with pytest.raises(ApprovalError, match="expired"):
        engine.resolve_approval_request(
            request.approval_id, ApprovalOutcome.APPROVED
        )

    assert storage.get_request(request.approval_id).status is ApprovalStatus.EXPIRED
    assert storage.load_task(task.id).status is TaskStatus.FAILED
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_cancelled_goal_cannot_be_revived_by_pending_approval(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, goal_id = _pending(
        tmp_path / "cancel.db", goal_manager=True
    )
    engine.goal_manager.cancel(goal_id)

    with pytest.raises(ApprovalError, match="terminal goal"):
        engine.resolve_approval_request(
            request.approval_id, ApprovalOutcome.APPROVED
        )

    assert storage.get_request(request.approval_id).status is ApprovalStatus.DENIED
    assert storage.load_task(task.id).status is TaskStatus.FAILED
    assert capability.calls == 0
    assert engine.run_task(task.id).status is TaskStatus.FAILED
    _close(engine, storage, cognitive)


def test_run_after_cancel_reconciles_pending_request_without_execution(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, goal_id = _pending(
        tmp_path / "cancel-run.db", goal_manager=True
    )
    engine.goal_manager.cancel(goal_id)

    resumed = engine.run_task(task.id)

    assert resumed.status is TaskStatus.FAILED
    assert storage.get_request(request.approval_id).status is ApprovalStatus.DENIED
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_cancel_after_approval_before_resume_blocks_execution(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, goal_id = _pending(
        tmp_path / "cancel-approved.db", goal_manager=True
    )
    engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED
    )
    engine.goal_manager.cancel(goal_id)

    resumed = engine.run_task(task.id)

    assert storage.get_request(request.approval_id).status is ApprovalStatus.APPROVED
    assert resumed.status is TaskStatus.FAILED
    assert "terminal goal" in (resumed.error or "")
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_legacy_denied_row_cannot_execute_through_approved_mirror(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "legacy-denied.db"
    )
    request.status = ApprovalStatus.DENIED
    request.decision_actor = "legacy-denier"
    storage.update_request(request)
    persisted = storage.load_task(task.id)
    persisted.status = TaskStatus.RUNNING
    persisted.approvals[-1]["outcome"] = "approved"
    storage.save_task(persisted)

    resumed = engine.run_task(task.id)

    assert capability.calls == 0
    assert resumed.status is TaskStatus.AWAITING_APPROVAL
    assert storage.get_request(request.approval_id).status is ApprovalStatus.DENIED
    assert len(storage.list_requests()) == 2  # fresh approval, never old denial
    _close(engine, storage, cognitive)


def test_legacy_approved_awaiting_split_reconciles_on_same_retry(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "legacy.db"
    )
    # Pre-ADR-038 crash shape: decision row committed, task still awaiting.
    request.status = ApprovalStatus.APPROVED
    request.decision_actor = "legacy-approver"
    storage.update_request(request)

    resolved = engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED
    )
    resumed = engine.run_task(task.id)

    assert resolved.status is ApprovalStatus.APPROVED
    assert resumed.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    _close(engine, storage, cognitive)
