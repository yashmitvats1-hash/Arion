"""Durable scheduler/work registry (ADR-025, Phase A).

The registry is a SQLite-backed, bounded-metadata record of scheduler work:

- typed domain objects + a store protocol (the engine/CLI never touch SQLite
  directly for scheduler state);
- explicit states QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED/ABANDONED with
  legal transitions enforced by typed errors (fail closed);
- bounded metadata only: ids, task/goal/step references, scheduler + worker
  identity, timestamps, lease/deadline, bounded error text. NEVER threads,
  callables, stack traces, capability outputs, model output, prompts, file
  contents, or secrets.
"""

from __future__ import annotations

import pytest

from arion.state.scheduler_work import (
    SchedulerRegistryError,
    SchedulerStateError,
    SchedulerWork,
    SchedulerWorkStatus,
    legal_transition,
)
from arion.state.store import SQLiteStorage


def _reg(db_path: str) -> SQLiteStorage:
    return SQLiteStorage(db_path)


def _mk(reg, scheduler_id="sched-1", task_id="task_a", step_index=0,
        goal_id="goal_1", now="2026-01-01T00:00:00+00:00") -> SchedulerWork:
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=step_index,
                      scheduler_id=scheduler_id, now=now)


# --------------------------------------------------------------------------- #
# lifecycle + transitions
# --------------------------------------------------------------------------- #


def test_create_makes_queued_row(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg)
    assert work.work_id.startswith("sw_")
    assert work.status == SchedulerWorkStatus.QUEUED
    assert work.task_id == "task_a"
    assert work.goal_id == "goal_1"
    assert work.step_index == 0
    assert work.scheduler_id == "sched-1"
    assert work.worker_id is None
    assert work.started_at is None
    assert work.completed_at is None
    assert work.lease_expires_at is None
    assert work.error is None
    # durable: readable back from a fresh storage handle
    again = _reg(db_path)
    loaded = again.get_work(work.work_id)
    assert loaded is not None
    assert loaded.status == SchedulerWorkStatus.QUEUED


def test_queued_to_running_sets_worker_and_lease(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg)
    running = reg.mark_running(
        work.work_id, worker_id="worker-7", lease_seconds=60.0,
        now="2026-01-01T00:01:00+00:00")
    assert running.status == SchedulerWorkStatus.RUNNING
    assert running.worker_id == "worker-7"
    assert running.started_at == "2026-01-01T00:01:00+00:00"
    assert running.lease_expires_at == "2026-01-01T00:02:00+00:00"
    assert reg.get_work(work.work_id).status == SchedulerWorkStatus.RUNNING


def test_running_to_terminal_states(db_path: str):
    reg = _reg(db_path)
    for status in (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED,
                   SchedulerWorkStatus.ABANDONED):
        work = _mk(reg)
        reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0,
                         now="2026-01-01T00:01:00+00:00")
        terminal = reg.mark_terminal(
            work.work_id, status, error="boom" if status == SchedulerWorkStatus.FAILED else None,
            now="2026-01-01T00:02:00+00:00", owner_worker_id="w1")
        assert terminal.status == status
        assert terminal.completed_at == "2026-01-01T00:02:00+00:00"
        if status == SchedulerWorkStatus.FAILED:
            assert terminal.error == "boom"


def test_queued_cancel_and_abandon(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg)
    cancelled = reg.mark_terminal(work.work_id, SchedulerWorkStatus.CANCELLED)
    assert cancelled.status == SchedulerWorkStatus.CANCELLED

    work2 = _mk(reg, task_id="task_b")
    abandoned = reg.mark_terminal(work2.work_id, SchedulerWorkStatus.ABANDONED)
    assert abandoned.status == SchedulerWorkStatus.ABANDONED


@pytest.mark.parametrize("from_status,to_status", [
    (SchedulerWorkStatus.QUEUED, SchedulerWorkStatus.QUEUED),
    (SchedulerWorkStatus.QUEUED, SchedulerWorkStatus.COMPLETED),
    (SchedulerWorkStatus.QUEUED, SchedulerWorkStatus.FAILED),
    (SchedulerWorkStatus.RUNNING, SchedulerWorkStatus.QUEUED),
    (SchedulerWorkStatus.RUNNING, SchedulerWorkStatus.RUNNING),
    (SchedulerWorkStatus.RUNNING, SchedulerWorkStatus.CANCELLED),
    (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.RUNNING),
    (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED),
    (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.ABANDONED),
    (SchedulerWorkStatus.FAILED, SchedulerWorkStatus.COMPLETED),
    (SchedulerWorkStatus.CANCELLED, SchedulerWorkStatus.QUEUED),
    (SchedulerWorkStatus.ABANDONED, SchedulerWorkStatus.RUNNING),
    (SchedulerWorkStatus.ABANDONED, SchedulerWorkStatus.COMPLETED),
    (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.CANCELLED),
    (SchedulerWorkStatus.FAILED, SchedulerWorkStatus.ABANDONED),
])
def test_illegal_transitions_rejected(db_path: str, from_status, to_status):
    assert not legal_transition(from_status, to_status)
    reg = _reg(db_path)
    work = _mk(reg)
    # build the row up to `from_status` via legal moves only
    if from_status == SchedulerWorkStatus.RUNNING:
        reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0)
    elif from_status in (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED):
        reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0)
        reg.mark_terminal(work.work_id, from_status, owner_worker_id="w1")
    elif from_status in (SchedulerWorkStatus.CANCELLED, SchedulerWorkStatus.ABANDONED):
        # cancelled/abandoned rows come from QUEUED (pre-execution)
        reg.mark_terminal(work.work_id, from_status)
    with pytest.raises(SchedulerStateError):
        if to_status == SchedulerWorkStatus.RUNNING:
            reg.mark_running(work.work_id, worker_id="w2", lease_seconds=60.0)
        else:
            reg.mark_terminal(work.work_id, to_status, owner_worker_id="w1")


def test_terminal_states_are_final(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg)
    reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0)
    reg.mark_terminal(work.work_id, SchedulerWorkStatus.COMPLETED, owner_worker_id="w1")
    with pytest.raises(SchedulerStateError):
        reg.mark_terminal(work.work_id, SchedulerWorkStatus.FAILED, owner_worker_id="w1")


def test_unknown_work_id_fails_closed(db_path: str):
    reg = _reg(db_path)
    with pytest.raises(SchedulerStateError):
        reg.mark_running("sw_nope", worker_id="w1", lease_seconds=60.0)
    with pytest.raises(SchedulerStateError):
        reg.mark_terminal("sw_nope", SchedulerWorkStatus.COMPLETED)
    assert reg.get_work("sw_nope") is None


def test_state_error_is_typed_and_carries_actual_state(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg)
    reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0)
    reg.mark_terminal(work.work_id, SchedulerWorkStatus.COMPLETED, owner_worker_id="w1")
    with pytest.raises(SchedulerStateError) as exc:
        reg.mark_running(work.work_id, worker_id="w2", lease_seconds=60.0)
    assert "completed" in str(exc.value)


# --------------------------------------------------------------------------- #
# bounded metadata
# --------------------------------------------------------------------------- #


def test_registry_rows_never_hold_engine_objects(db_path: str):
    """The registry must never persist callables/threads/content: the domain
    object is a plain dataclass and to_dict exposes bounded fields only."""
    reg = _reg(db_path)
    work = _mk(reg)
    reg.mark_running(work.work_id, worker_id="w1", lease_seconds=60.0)
    reg.mark_terminal(work.work_id, SchedulerWorkStatus.FAILED,
                      error="x" * 5000, owner_worker_id="w1")
    d = reg.get_work(work.work_id).to_dict()
    assert set(d.keys()) == {
        "work_id", "task_id", "goal_id", "step_index", "scheduler_id",
        "worker_id", "status", "attempts", "error", "created_at",
        "started_at", "completed_at", "lease_expires_at",
    }
    # bounded error text
    assert len(d["error"]) <= 500


def test_created_row_is_durable_across_reopen(db_path: str):
    reg = _reg(db_path)
    work = _mk(reg, task_id="task_x")
    reg.mark_running(work.work_id, worker_id="w1", lease_seconds=10.0,
                     now="2026-01-01T00:01:00+00:00")
    reg.mark_terminal(work.work_id, SchedulerWorkStatus.COMPLETED,
                      now="2026-01-01T00:02:00+00:00", owner_worker_id="w1")
    again = _reg(db_path)
    loaded = again.get_work(work.work_id)
    assert loaded.status == SchedulerWorkStatus.COMPLETED
    assert loaded.started_at == "2026-01-01T00:01:00+00:00"


# --------------------------------------------------------------------------- #
# listing / reclaim
# --------------------------------------------------------------------------- #


def test_list_filters(db_path: str):
    reg = _reg(db_path)
    a = _mk(reg, task_id="task_a", scheduler_id="sched-1")
    b = _mk(reg, task_id="task_b", scheduler_id="sched-1")
    _mk(reg, task_id="task_c", scheduler_id="sched-2")
    reg.mark_running(a.work_id, worker_id="w1", lease_seconds=60.0)
    assert [w.work_id for w in reg.list_work(status=SchedulerWorkStatus.QUEUED,
                                        scheduler_id="sched-1")] == [b.work_id]
    assert [w.work_id for w in reg.list_work(status=SchedulerWorkStatus.RUNNING)] == [a.work_id]
    assert len(reg.list_work(scheduler_id="sched-2")) == 1
    assert len(reg.list_work(task_id="task_a")) == 1
    assert len(reg.list_work()) == 3


def test_reclaim_stale_abandons_expired_running(db_path: str):
    reg = _reg(db_path)
    stale = _mk(reg, task_id="task_a")
    live = _mk(reg, task_id="task_b")
    reg.mark_running(stale.work_id, worker_id="w1", lease_seconds=60.0,
                     now="2026-01-01T00:00:00+00:00")
    reg.mark_running(live.work_id, worker_id="w2", lease_seconds=60.0,
                     now="2026-01-01T00:05:00+00:00")
    reclaimed = reg.reclaim_stale(now="2026-01-01T00:02:00+00:00")
    assert reclaimed == [stale.work_id]
    assert reg.get_work(stale.work_id).status == SchedulerWorkStatus.ABANDONED
    assert reg.get_work(live.work_id).status == SchedulerWorkStatus.RUNNING
    # idempotent: nothing left to reclaim
    assert reg.reclaim_stale(now="2026-01-01T00:02:00+00:00") == []


def test_abandon_foreign_queued_only_touches_other_schedulers(db_path: str):
    reg = _reg(db_path)
    mine = _mk(reg, task_id="task_a", scheduler_id="sched-mine")
    theirs = _mk(reg, task_id="task_b", scheduler_id="sched-dead")
    count = reg.abandon_foreign_queued("sched-mine")
    assert count == 1
    assert reg.get_work(mine.work_id).status == SchedulerWorkStatus.QUEUED
    assert reg.get_work(theirs.work_id).status == SchedulerWorkStatus.ABANDONED
    # a second pass is a no-op
    assert reg.abandon_foreign_queued("sched-mine") == 0


def test_domain_object_roundtrip():
    work = SchedulerWork(
        work_id="sw_1", task_id="task_a", goal_id="goal_1", step_index=2,
        scheduler_id="sched-1", worker_id="w1",
        status=SchedulerWorkStatus.RUNNING, attempts=3, error="boom",
        created_at="t0", started_at="t1", completed_at=None,
        lease_expires_at="t2",
    )
    d = work.to_dict()
    assert d["status"] == "running"
    assert d["step_index"] == 2
    again = SchedulerWork.from_dict(d)
    assert again == work


def test_registry_failure_is_typed(db_path: str):
    """A registry-level failure (not a state transition) raises the typed
    registry error so callers can fail closed without catching SQLite."""
    reg = _reg(db_path)
    with pytest.raises(SchedulerRegistryError):
        reg.create(task_id="", goal_id=None, step_index=-5, scheduler_id="s")
