"""Scheduler restart / crash recovery (ADR-025, Phase E) - tests first.

A restarted scheduler must fail closed and durably:

- death while work is QUEUED -> the dead scheduler's queue is abandoned and
  the tasks re-run (fresh full pipeline, no duplicate of anything durable);
- death while work is RUNNING -> stale leases are reclaimed (ABANDONED), no
  immortal RUNNING worker;
- a crash after mutation-lock acquisition, before mutation execution -> the
  stale lock is reclaimed through the EXISTING lock store and the step runs
  the fresh authorization/recovery path exactly once;
- restart with multiple goals pending preserves approval/mutation-lock/FIFO
  state and never blindly replays a completed mutation;
- one abandoned task is recovered while unrelated goals continue.

Includes one REAL subprocess test (crash-running) that proves the
persistence boundary across processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arion.state.locks import LockWaiterStatus, canonical_resource
from arion.state.models import GoalStatus, StepStatus, TaskStatus
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage
from tests.test_cross_goal_concurrency import (
    _env,
    _sandbox,
    _submit,
    _task_for,
    SlowReadCapability,
    SlowWriteCapability,
    TwoStepPlanner,
    _read_step,
    _write_step,
)

FS = "filesystem:path"
WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_scheduler_restart_worker.py")


def _iso_future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# --------------------------------------------------------------------------- #
# in-process crash/restart semantics
# --------------------------------------------------------------------------- #


def test_death_while_queued_abandons_foreign_queue_and_reruns(tmp_path):
    """Engine A admits a step (QUEUED registry row) and 'dies'. Engine B's
    construction abandons A's QUEUED rows; the task re-runs and completes
    exactly once."""
    db = tmp_path / "q1.db"
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name="q1.db")
    gid = _submit(env, "goal one")
    task = _task_for(env, gid)
    # simulate: engine admitted the step to the registry, then died
    row = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=gid, step_index=0,
        scheduler_id=env.engine.scheduler_id)
    assert row.status == SchedulerWorkStatus.QUEUED
    a_scheduler_id = env.engine.scheduler_id
    env.engine.storage.close()
    # ADR-026: A's registration is still live; its queue is abandoned only
    # once the registration lapses (crash detection). Model the lapse here.
    probe = SQLiteStorage(db)
    probe.unregister_scheduler(a_scheduler_id)
    probe.close()

    # engine B (restart) on the same DB
    read_cap = SlowReadCapability(sleep=0.01)
    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
                max_concurrency=1, read_cap=read_cap, db_name="q1.db")
    dead_row = env2.engine.scheduler_registry.get_work(row.work_id)
    assert dead_row.status == SchedulerWorkStatus.ABANDONED
    results = env2.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    rows = env2.engine.scheduler_registry.list_work(task_id=task.id)
    completed = [r for r in rows if r.status == SchedulerWorkStatus.COMPLETED]
    assert len(completed) == 1  # the fresh dispatch, exactly once
    env2.engine.storage.close()


def test_death_while_running_reclaims_stale_lease_no_immortal_worker(tmp_path):
    """Engine A marked a step RUNNING with a lease and died. Reclaim marks it
    ABANDONED (no immortal RUNNING); the task re-runs on restart."""
    db = tmp_path / "q2.db"
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name="q2.db")
    gid = _submit(env, "goal one")
    task = _task_for(env, gid)
    row = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=gid, step_index=0,
        scheduler_id=env.engine.scheduler_id)
    env.engine.scheduler_registry.mark_running(
        row.work_id, worker_id="worker:dead:1", lease_seconds=1.0)
    env.engine.storage.close()
    time.sleep(1.1)  # let the lease expire

    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
                max_concurrency=1, db_name="q2.db")
    dead_row = env2.engine.scheduler_registry.get_work(row.work_id)
    assert dead_row.status == SchedulerWorkStatus.ABANDONED
    assert env2.engine.scheduler_registry.reclaim_stale() == []
    results = env2.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    rows = env2.engine.scheduler_registry.list_work(task_id=task.id)
    assert [r for r in rows if r.status == SchedulerWorkStatus.RUNNING] == []
    assert sum(1 for r in rows if r.status == SchedulerWorkStatus.COMPLETED) == 1
    env2.engine.storage.close()


def test_crash_after_lock_acquisition_no_duplicate_mutation(tmp_path):
    """A step that acquired the durable mutation lock and then 'died' before
    mutating: the stale lock is reclaimed via the existing lock store, the
    abandoned work is reclaimed, and the re-run performs the mutation exactly
    once (fresh authorization path)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "q3.db"
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_write_step("a.txt")]),
               max_concurrency=1, lock_wait_max_seconds=0.0,
               approve_risk_high=False, db_name="q3.db")
    gid = _submit(env, "write a")
    task = _task_for(env, gid)
    row = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=gid, step_index=0,
        scheduler_id=env.engine.scheduler_id)
    env.engine.scheduler_registry.mark_running(
        row.work_id, worker_id="worker:dead:2", lease_seconds=1.0)
    # the step also held the durable mutation lock when it died
    lock = env.storage.acquire(FS, canonical_resource(FS, "a.txt"),
                               "filesystem.write", "write", "proc:dead:2",
                               1.0)
    env.engine.storage.close()
    time.sleep(1.1)

    write_cap = SlowWriteCapability(sb, sleep=0.01)
    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_write_step("a.txt")]),
                max_concurrency=1, lock_wait_max_seconds=0.0,
                write_cap=write_cap, approve_risk_high=False, db_name="q3.db")
    # both authorities are reclaimed through their existing stores
    assert env2.engine.scheduler_registry.get_work(row.work_id).status == \
        SchedulerWorkStatus.ABANDONED
    env2.engine.reclaim_stale_locks(now=_iso_future(60))
    assert env2.storage.get(lock.lock_id) is None
    results = env2.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"  # written exactly once
    assert write_cap.calls == ["a.txt"]
    env2.engine.storage.close()


def test_restart_with_multiple_goals_no_duplicate_mutation(tmp_path):
    """Goal A completed, goal B pending, then 'restart': A's completed
    mutation is never replayed; B completes; each mutation exactly once."""
    sb = _sandbox(tmp_path)
    write_cap_a = SlowWriteCapability(sb, sleep=0.01)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "A" in d else [_write_step("b.txt")])),
        max_concurrency=2, write_cap=write_cap_a, lock_wait_max_seconds=0.0,
        approve_risk_high=False, db_name="q4.db")
    g_a = _submit(env, "goal A")
    g_b = _submit(env, "goal B")
    # run A to completion, then 'crash' before B runs
    env.engine.run_goals([g_a])
    env.engine.storage.close()

    write_cap_b = SlowWriteCapability(sb, sleep=0.01)
    env2 = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "A" in d else [_write_step("b.txt")])),
        max_concurrency=2, write_cap=write_cap_b, lock_wait_max_seconds=0.0,
        approve_risk_high=False, db_name="q4.db")
    results = env2.engine.run_goals([g_a, g_b])
    assert results[g_a].status == GoalStatus.COMPLETED
    assert results[g_b].status == GoalStatus.COMPLETED
    # a.txt was written exactly once (by the first engine), b.txt exactly once
    # (by the second); a completed mutation is never replayed after restart
    assert write_cap_a.calls == ["a.txt"]
    assert write_cap_b.calls == ["b.txt"]
    attempts = [e for e in env2.storage.list_events() if e.kind == "mutation.attempted"]
    assert len(attempts) == 2
    env2.engine.storage.close()


def test_one_abandoned_task_recovered_while_other_goal_continues(tmp_path):
    """Task A's work is abandoned (stale lease); on restart A is re-run while
    goal B's fresh work executes - recovery of one never blocks the other."""
    db = tmp_path / "q5.db"
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name="q5.db")
    g_a = _submit(env, "goal A")
    g_b = _submit(env, "goal B")
    task_a = _task_for(env, g_a)
    row = env.engine.scheduler_registry.create(
        task_id=task_a.id, goal_id=g_a, step_index=0,
        scheduler_id=env.engine.scheduler_id)
    env.engine.scheduler_registry.mark_running(
        row.work_id, worker_id="worker:dead:3", lease_seconds=1.0)
    env.engine.storage.close()
    time.sleep(1.1)

    read_cap = SlowReadCapability(sleep=0.05)
    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
                max_concurrency=2, read_cap=read_cap, db_name="q5.db")
    assert env2.engine.scheduler_registry.get_work(row.work_id).status == \
        SchedulerWorkStatus.ABANDONED
    results = env2.engine.run_goals([g_a, g_b])
    assert results[g_a].status == GoalStatus.COMPLETED
    assert results[g_b].status == GoalStatus.COMPLETED
    assert read_cap.started.count("a.txt") == 2  # A re-ran + B ran
    env2.engine.storage.close()


def test_approval_state_survives_restart(tmp_path):
    """A task parked at AWAITING_APPROVAL keeps its durable approval across a
    restart; resolving it resumes exactly the same step."""
    sb = _sandbox(tmp_path)
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_write_step("a.txt")]),
               max_concurrency=1, approve_risk_high=True, db_name="q6.db")
    gid = _submit(env, "write a")
    env.engine.run_goals([gid])
    task = _task_for(env, gid)
    assert task.status == TaskStatus.AWAITING_APPROVAL
    pending = [r for r in env.engine.approval_store.list_requests()
               if r.status.value == "pending"]
    assert len(pending) == 1
    approval_id = pending[0].approval_id
    env.engine.storage.close()

    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_write_step("a.txt")]),
                max_concurrency=1, approve_risk_high=True, db_name="q6.db")
    env2.engine.resolve_approval_request(approval_id, "approved", actor="user:alice")
    results = env2.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"
    env2.engine.storage.close()


def test_fifo_survives_restart(tmp_path):
    """Two waiters queued on one resource keep their durable FIFO positions
    across a restart; a restarted engine acquires in queue order."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "q7.db"
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "w" in d else [_read_step("b.txt")])),
        max_concurrency=2, lock_wait_max_seconds=60.0, approve_risk_high=False,
        db_name="q7.db")
    holder = env.storage.acquire(FS, canonical_resource(FS, "a.txt"),
                                 "filesystem.write", "write", "proc-holder", 3600.0)
    g1 = _submit(env, "w one")
    g2 = _submit(env, "w two")
    # both park: durable waiters enqueued, no worker consumed
    env.engine.run_goals([g1, g2])
    t1, t2 = _task_for(env, g1), _task_for(env, g2)
    w1 = env.storage.get_waiter(t1.lock_wait["waiter_id"])
    w2 = env.storage.get_waiter(t2.lock_wait["waiter_id"])
    assert w1.status == LockWaiterStatus.QUEUED and w2.status == LockWaiterStatus.QUEUED
    assert w1.seq != w2.seq  # distinct durable FIFO positions
    # remember which task holds the head position
    head_task = t1.id if w1.seq < w2.seq else t2.id
    env.engine.storage.close()

    # restart; release the foreign lock; the parked tasks resume in FIFO order
    write_cap = SlowWriteCapability(sb, sleep=0.01)
    env2 = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "w" in d else [_read_step("b.txt")])),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=60.0,
        approve_risk_high=False, db_name="q7.db")
    env2.storage.release(holder.lock_id, "proc-holder")
    results = env2.engine.run_goals([g1, g2])
    assert results[g1].status == GoalStatus.COMPLETED
    assert results[g2].status == GoalStatus.COMPLETED
    acquired = [e for e in env2.storage.list_events() if e.kind == "mutation.lock.acquired"]
    assert len(acquired) == 2
    # the durable head (lowest seq) acquired first - FIFO survived the restart
    assert acquired[0].task_id == head_task
    assert write_cap.calls == ["a.txt", "a.txt"]
    env2.engine.storage.close()


def test_abandoned_work_requires_fresh_authorization(tmp_path):
    """After a restart, the re-run of abandoned work performs its own live
    authorization (permission.checked) - never reuses a pre-crash decision."""
    db = tmp_path / "q8.db"
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name="q8.db")
    gid = _submit(env, "goal one")
    task = _task_for(env, gid)
    row = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=gid, step_index=0,
        scheduler_id=env.engine.scheduler_id)
    env.engine.scheduler_registry.mark_running(
        row.work_id, worker_id="worker:dead:4", lease_seconds=1.0)
    env.engine.storage.close()
    time.sleep(1.1)

    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
                max_concurrency=1, db_name="q8.db")
    env2.engine.run_goals([gid])
    checked = [e for e in env2.storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) == 1  # the fresh post-restart authorization, exactly
    env2.engine.storage.close()


# --------------------------------------------------------------------------- #
# real subprocess: crash while RUNNING
# --------------------------------------------------------------------------- #


def test_subprocess_crash_running_reclaims_and_reruns_exactly_once(tmp_path):
    # the parent engine's sandbox lives at tmp_path/"wsandbox" (see _env);
    # the child uses the SAME directory so the rerun mutates the same file
    sb = tmp_path / "wsandbox"
    sb.mkdir()
    (sb / "a.txt").write_text("a", encoding="utf-8")
    db = str(tmp_path / "sub.db")

    # the child acquires the mutation lock, marks its work RUNNING, then
    # dies inside the capability (before mutating, before persisting)
    proc = subprocess.run([sys.executable, WORKER, "crash-running",
                           "--db", db, "--sandbox", str(sb), "--lease", "1.0"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, f"worker should have died: {proc.stdout[-800:]}"
    time.sleep(1.1)  # let the leases expire

    st = SQLiteStorage(db)
    locks = st.list(resource_kind=FS, resource=canonical_resource(FS, "a.txt"))
    assert len(locks) == 1  # the dead process left its durable lock behind
    assert st.reclaim_expired(now=_iso_future(60)) == [locks[0].lock_id]
    running = st.list_work(status=SchedulerWorkStatus.RUNNING)
    assert len(running) == 1  # the dead process's RUNNING row
    assert st.reclaim_stale(now=_iso_future(60)) == [running[0].work_id]
    st.close()

    # fresh engine: run the goal to completion - exactly one mutation
    from tests.test_cross_goal_concurrency import _env as _env2
    from tests.test_cross_goal_concurrency import _write_step as _ws

    env = _env2(tmp_path, TwoStepPlanner(lambda d: [_ws("a.txt")]),
                max_concurrency=1, lock_wait_max_seconds=0.0,
                approve_risk_high=False, db_name="sub.db")
    # the task persisted by the child is loaded by goal_id
    goals = env.gm.list_goals() if hasattr(env.gm, "list_goals") else []
    assert len(goals) == 1
    results = env.engine.run_goals([goals[0].id])
    assert results[goals[0].id].status == GoalStatus.COMPLETED
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"
    # the child died mid-attempt (its attempt never succeeded); the rerun is
    # the only SUCCEEDED mutation - no duplicate, no blind replay
    succeeded = [e for e in env.storage.list_events() if e.kind == "mutation.succeeded"]
    assert len(succeeded) == 1
    env.engine.storage.close()
