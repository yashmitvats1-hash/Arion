"""ADR-039: active mutation leases renew and orphan waiters are adopted."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.locks import LockWaiterStatus, MutationLockError
from arion.state.models import PlanStep, Task, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
T0 = "2026-01-01T00:00:00+00:00"


def _plus(value: str, seconds: float) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def test_lock_renewal_requires_live_exact_owner(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "renew.db")
    lock = storage.acquire(
        FS, "notes.txt", "filesystem.write", "write", "owner-a",
        lease_seconds=10, now=T0,
    )

    renewed = storage.renew(
        lock.lock_id, "owner-a", lease_seconds=10, now=_plus(T0, 5)
    )

    assert renewed.owner_id == "owner-a"
    assert renewed.expires_at == _plus(T0, 15)
    with pytest.raises(MutationLockError, match="owner"):
        storage.renew(
            lock.lock_id, "owner-b", lease_seconds=10, now=_plus(T0, 6)
        )
    with pytest.raises(MutationLockError, match="expired"):
        storage.renew(
            lock.lock_id, "owner-a", lease_seconds=10, now=_plus(T0, 16)
        )
    storage.close()


def test_duplicate_waiter_creation_adopts_original_position(tmp_path: Path) -> None:
    db = tmp_path / "waiter.db"
    left = SQLiteStorage(db)
    right = SQLiteStorage(db)
    task = Task(id="task-one", goal_id="goal", description="wait")
    left.save_task(task)
    barrier = threading.Barrier(2)
    adopted = []

    def enqueue(store):
        barrier.wait()
        adopted.append(store.enqueue_waiter(
            FS, "notes.txt", task.id, task.goal_id, 0,
            deadline=_plus(T0, 100), now=T0,
        ))

    a = threading.Thread(target=enqueue, args=(left,))
    b = threading.Thread(target=enqueue, args=(right,))
    a.start(); b.start(); a.join(); b.join()

    rows = left.list_waiters(status="queued")
    assert len(rows) == 1
    assert adopted[0].waiter_id == adopted[1].waiter_id == rows[0].waiter_id
    assert adopted[0].seq == adopted[1].seq == 1
    left.close(); right.close()


class _Tracker:
    lock = threading.Lock()
    active = 0
    max_active = 0
    first_started = threading.Event()
    release_first = threading.Event()


class _SlowCapability:
    name = "lease.write"
    description = "slow mutation"
    actions = [ActionSpec(
        name="write",
        description="write",
        required_scope="lease:write",
        side_effects="mutating",
        reversible=False,
        idempotent=False,
        retry_safe=False,
        resource_kind=FS,
        resource_param="path",
        param_schema={"path": {"type": "string", "required": True}},
    )]

    def __init__(self, label: str):
        self.label = label
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        with _Tracker.lock:
            _Tracker.active += 1
            _Tracker.max_active = max(_Tracker.max_active, _Tracker.active)
        if self.label == "first":
            _Tracker.first_started.set()
            _Tracker.release_first.wait(timeout=5)
        with _Tracker.lock:
            _Tracker.active -= 1
        return {"written": True, "size": 0}


class _Planner:
    def required_capabilities(self, goal_description):
        return {"lease.write"}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="mutate",
            capability="lease.write",
            action="write",
            scope="lease:write",
            params={"path": "same.txt"},
            verification=VerificationPolicy("non_empty"),
            max_attempts=1,
        )]


def _engine(db: Path, capability, *, lease: float, wait: float = 0):
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(capability)
    planner = _Planner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            allowed_scopes={"lease:write"},
            boundaries={FS: RelativePathBoundary()},
        ),
        mutation_lock_lease_seconds=lease,
        lock_wait_max_seconds=wait,
        scheduler_reclaim_on_start=False,
    )
    return engine, storage


def test_active_long_mutation_renews_lease_and_prevents_overlap(
    tmp_path: Path,
) -> None:
    _Tracker.active = 0
    _Tracker.max_active = 0
    _Tracker.first_started = threading.Event()
    _Tracker.release_first = threading.Event()
    db = tmp_path / "long.db"
    first_capability = _SlowCapability("first")
    second_capability = _SlowCapability("second")
    first, first_store = _engine(db, first_capability, lease=0.15)
    second, second_store = _engine(db, second_capability, lease=1.0)
    result = {}

    worker = threading.Thread(
        target=lambda: result.setdefault("first", first.execute_goal("first"))
    )
    worker.start()
    assert _Tracker.first_started.wait(timeout=3)
    time.sleep(0.3)  # more than the original lease; heartbeat must retain it
    result["second"] = second.execute_goal("second")

    assert second_capability.calls == 0
    assert result["second"].status is TaskStatus.FAILED
    assert _Tracker.max_active == 1
    _Tracker.release_first.set()
    worker.join(timeout=5)
    assert result["first"].status is TaskStatus.COMPLETED
    first.shutdown(); second.shutdown()
    first_store.close(); second_store.close()


def test_goal_cancellation_while_waiting_cancels_waiter_without_mutation(
    tmp_path: Path,
) -> None:
    from tests.test_lock_waiting import (
        FakeTime, _approve, _engine as waiting_engine, _sandbox,
    )

    sandbox = _sandbox(tmp_path)
    db = tmp_path / "cancel-wait.db"
    clock = FakeTime()

    class CancelSleeper:
        def __init__(self):
            self.engine = None
            self.goal_id = None
            self.cancelled = False

        def sleep(self, seconds):
            if not self.cancelled:
                self.cancelled = True
                self.engine.goal_manager.cancel(self.goal_id)
            clock.sleep(seconds)

    sleeper = CancelSleeper()
    engine, manager, storage, registry = waiting_engine(
        db, sandbox, max_wait=60, backoff_base=.1, backoff_max=.1,
        clock=clock.now, sleeper=sleeper,
    )
    holder = SQLiteStorage(db)
    holder.acquire(
        FS, "notes.txt", "filesystem.write", "write", "holder",
        lease_seconds=3600, now=clock.now(),
    )
    goal_id = engine.submit_goal("write notes").id
    _approve(engine, goal_id)
    sleeper.engine = engine
    sleeper.goal_id = goal_id

    engine.run_goal(goal_id)

    task = manager.task_history(goal_id)[-1]
    assert manager.get_goal(goal_id).status.value == "cancelled"
    assert task.status is TaskStatus.FAILED
    assert registry.get("filesystem.write").calls == 0
    own = [
        waiter for waiter in storage.list_waiters()
        if waiter.task_id == task.id
    ]
    assert own and all(waiter.status is LockWaiterStatus.CANCELLED for waiter in own)
    engine.shutdown(); storage.close(); holder.close()


def test_final_ownership_loss_requires_recovery(tmp_path: Path) -> None:
    db = tmp_path / "lost.db"
    storage = SQLiteStorage(db)

    class LoseOwnershipCapability(_SlowCapability):
        def execute(self, action, params):
            self.calls += 1
            # Simulate external/admin stale reclamation while the side effect
            # may have happened. Final ownership validation must fail closed.
            storage.reclaim_expired(now="2099-01-01T00:00:00+00:00")
            return {"written": True, "size": 0}

    capability = LoseOwnershipCapability("lost")
    registry = CapabilityRegistry()
    registry.register(capability)
    planner = _Planner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            allowed_scopes={"lease:write"},
            boundaries={FS: RelativePathBoundary()},
        ),
        mutation_lock_lease_seconds=10,
        lock_wait_max_seconds=0,
    )

    task = engine.execute_goal("lose ownership")

    assert capability.calls == 1
    assert task.status is TaskStatus.FAILED
    assert "ownership" in (task.error or "")
    recoveries = storage.list_recoveries(task_id=task.id)
    assert len(recoveries) == 1
    assert recoveries[0].status.value == "required"
    engine.shutdown(); storage.close()
