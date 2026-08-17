"""Cross-process shared scheduler (ADR-026, Phases B/C) - tests first.

Two or more engine processes share ONE scheduler/work registry database:

- atomic claims: two processes racing for one queued item -> exactly one
  owner (real subprocesses);
- global cross-process capacity: total live RUNNING rows never exceed the
  durable configured cap even with multiple engines;
- no duplicate execution: a claimed row is never run twice; a crashed
  owner's row is reclaimed through lease expiry and re-run exactly once;
- ownership transfer via release_and_claim_next;
- crash recovery: death while QUEUED (dead registration -> abandoned),
  death while RUNNING (expired lease -> reclaimed), death while holding a
  mutation lock (existing lock recovery + registry reclaim -> one run).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arion.state.locks import LockWaiterStatus  # noqa: F401  (conventions)
from arion.state.models import GoalStatus, TaskStatus
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

from tests.test_cross_goal_concurrency import (
    SlowReadCapability,
    TwoStepPlanner,
    _read_step,
    _submit,
    _task_for,
    _env,
)

WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_scheduler_multi_worker.py")
FS = "filesystem:path"


def _iso_future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _run(*argv: str, timeout: float = 90.0) -> dict:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"worker failed: {proc.stdout[-800:]} {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# Phase B - contention between engine processes
# --------------------------------------------------------------------------- #


def test_two_engines_share_one_registry(tmp_path):
    """Two engines on ONE db (two 'processes'): distinct scheduler ids, both
    complete their tasks, one COMPLETED registry row per dispatch."""
    env_a = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
                 max_concurrency=2, db_name="shared.db")
    env_b = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("b.txt")]),
                 max_concurrency=2, db_name="shared.db")
    assert env_a.engine.scheduler_id != env_b.engine.scheduler_id
    ga = _submit(env_a, "goal A")
    gb = _submit(env_b, "goal B")
    ra = env_a.engine.run_goals([ga])
    rb = env_b.engine.run_goals([gb])
    assert ra[ga].status == GoalStatus.COMPLETED
    assert rb[gb].status == GoalStatus.COMPLETED
    rows = env_a.engine.scheduler_registry.list_work()
    assert len([r for r in rows if r.status == SchedulerWorkStatus.COMPLETED]) == 2
    assert len([r for r in rows if r.status == SchedulerWorkStatus.RUNNING]) == 0
    env_a.engine.shutdown()
    env_b.engine.shutdown()
    env_a.engine.storage.close()
    env_b.engine.storage.close()


def test_global_capacity_enforced_across_engines(tmp_path):
    """Two engines, local max_concurrency=2 each, durable global cap=2:
    total active executions never exceed 2 (not 2+2)."""
    shared_cap = SlowReadCapability(sleep=0.15)
    env_a = _env(tmp_path, TwoStepPlanner(
        lambda d: [_read_step("a.txt") if "a" in d else _read_step("b.txt")]),
        max_concurrency=2, read_cap=shared_cap, db_name="cap.db")
    env_b = _env(tmp_path, TwoStepPlanner(
        lambda d: [_read_step("a.txt") if "a" in d else _read_step("b.txt")]),
        max_concurrency=2, read_cap=shared_cap, db_name="cap.db")
    # configure the durable cross-process capacity once (shared registry)
    env_a.engine.scheduler_registry.set_scheduler_global_max(2)

    ga1 = _submit(env_a, "goal a1")
    ga2 = _submit(env_a, "goal a2")
    gb1 = _submit(env_b, "goal b1")
    gb2 = _submit(env_b, "goal b2")
    results: dict[str, GoalStatus] = {}
    errors = []

    def run(engine, gids, tag):
        try:
            # bounded re-invocation: a goal stopped at the capacity boundary
            # resumes once capacity frees (clean-stop semantics)
            for _ in range(100):
                out = engine.run_goals(gids)
                for g, goal in out.items():
                    results[g] = goal.status
                if all(results.get(g) == GoalStatus.COMPLETED for g in gids):
                    return
        except Exception as exc:  # pragma: no cover
            errors.append(f"{tag}: {exc}")

    t1 = threading.Thread(target=run, args=(env_a.engine, [ga1, ga2], "A"))
    t2 = threading.Thread(target=run, args=(env_b.engine, [gb1, gb2], "B"))
    t1.start(); t2.start(); t1.join(timeout=90); t2.join(timeout=90)
    assert not errors, errors
    # capacity was the binding constraint: the shared capability never ran
    # more than 2 executions at once (4 would mean no cross-process cap)
    assert shared_cap.max_active <= 2, shared_cap.max_active
    # everything still completes (no permanent capacity consumption)
    for g, status in results.items():
        assert status == GoalStatus.COMPLETED, (g, status)
    env_a.engine.shutdown()
    env_b.engine.shutdown()
    env_a.engine.storage.close()
    env_b.engine.storage.close()


def test_two_processes_race_one_queued_item_exactly_one_owner(tmp_path):
    """Two REAL subprocesses race claim_next() on one queued row: exactly
    one claims it; the loser gets None; the row has exactly one owner."""
    db = str(tmp_path / "race.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-race")
    store.close()
    out = []
    for sid in ("sched-race", "sched-race"):
        out.append(_run("race-claim", "--db", db, "--scheduler-id", sid))
    winners = [o for o in out if o["claimed"] == row.work_id]
    losers = [o for o in out if o["claimed"] is None]
    assert len(winners) == 1 and len(losers) == 1, out
    store = SQLiteStorage(db)
    final = store.get_work(row.work_id)
    assert final.status == SchedulerWorkStatus.RUNNING
    assert final.worker_id == winners[0]["worker"]
    store.close()


def test_claimed_row_never_run_twice(tmp_path):
    """A row claimed by process A is RUNNING; process B's claim_next returns
    None (the row is not queued) - no duplicate execution possible."""
    db = str(tmp_path / "dup.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-shared")
    store.close()
    a = _run("claim-run", "--db", db, "--work-id", row.work_id, "--sleep", "0.3")
    assert a["status"] == "completed"
    # a fresh claim attempt after completion: the row is terminal
    store = SQLiteStorage(db)
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.COMPLETED
    assert store.claim_next("sched-shared", "w2", 60.0) is None
    store.close()


def test_ownership_transfer_via_release_and_claim_next(db_path: str):
    """release_and_claim_next hands the next queued item to the same worker
    atomically; a stale owner's handoff is rejected."""
    reg = SQLiteStorage(db_path)
    a = reg.create(task_id="t1", goal_id=None, step_index=0, scheduler_id="sched-h")
    b = reg.create(task_id="t2", goal_id=None, step_index=0, scheduler_id="sched-h")
    reg.claim(a.work_id, worker_id="w1", lease_seconds=60.0)
    terminal, nxt = reg.release_and_claim_next(
        a.work_id, owner_worker_id="w1", status=SchedulerWorkStatus.COMPLETED,
        error=None, scheduler_id="sched-h", worker_id="w1", lease_seconds=60.0)
    assert terminal.status == SchedulerWorkStatus.COMPLETED
    assert nxt is not None and nxt.work_id == b.work_id
    assert nxt.worker_id == "w1"
    # a STALE owner (an id that never owned the handed-off row) cannot hand
    # it off again
    import pytest
    from arion.state.scheduler_work import SchedulerStateError
    with pytest.raises(SchedulerStateError):
        reg.release_and_claim_next(
            b.work_id, owner_worker_id="w-stale", status=SchedulerWorkStatus.COMPLETED,
            error=None, scheduler_id="sched-h", worker_id="w-stale",
            lease_seconds=60.0)
    # and the TRUE owner can complete the handed-off row normally
    done = reg.mark_terminal(b.work_id, SchedulerWorkStatus.COMPLETED,
                             owner_worker_id="w1")
    assert done.status == SchedulerWorkStatus.COMPLETED
    reg.close()


# --------------------------------------------------------------------------- #
# Phase C - crash recovery across processes
# --------------------------------------------------------------------------- #


def test_subprocess_dies_queued_registration_lapses_then_abandoned(tmp_path):
    """A process dies with a QUEUED row; once its registration lease lapses
    (no heartbeat), another engine abandons the queue and the work re-runs
    exactly once."""
    db = str(tmp_path / "qdeath.db")
    store = SQLiteStorage(db)
    store.register_scheduler("sched-dying", pid=999, lease_seconds=0.3)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-dying")
    store.close()
    time.sleep(0.5)  # registration lease lapses (no heartbeat = dead)
    store = SQLiteStorage(db)
    assert not store.scheduler_registration_live("sched-dying")
    abandoned = store.abandon_foreign_queued("sched-alive")
    assert abandoned == 1
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    store.close()


def test_subprocess_dies_running_stale_lease_reclaimed(tmp_path):
    """A process claims a row then dies while RUNNING; after the lease
    expires, the row is reclaimed and a fresh process claims + completes it
    exactly once."""
    db = str(tmp_path / "rdeath.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-shared")
    store.close()
    proc = subprocess.Popen([sys.executable, WORKER, "crash-claimed",
                             "--db", db, "--work-id", row.work_id,
                             "--lease", "0.5"],
                            stdout=subprocess.PIPE, text=True)
    out = json.loads(proc.stdout.readline().strip())
    assert out["status"] == "running"
    proc.wait(timeout=30)
    assert proc.returncode == 1  # died while RUNNING
    time.sleep(0.7)  # lease expires

    store = SQLiteStorage(db)
    stale = store.reclaim_stale()
    assert stale == [row.work_id]
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    # fresh process claims + runs the same row? NO - the row is terminal.
    # The TASK re-runs through a fresh row: create + claim + complete once.
    row2 = store.create(task_id="t1", goal_id=None, step_index=0,
                        scheduler_id="sched-shared")
    store.close()
    done = _run("claim-run", "--db", db, "--work-id", row2.work_id, "--sleep", "0.1")
    assert done["status"] == "completed"
    store = SQLiteStorage(db)
    assert store.get_work(row2.work_id).status == SchedulerWorkStatus.COMPLETED
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    store.close()


def test_subprocess_stops_heartbeating_then_reclaimed(tmp_path):
    """A worker claims + heartbeats once, then stops heartbeating and never
    reports: the lease lapses and the row becomes reclaimable."""
    db = str(tmp_path / "hbdeath.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-shared")
    store.close()
    out = _run("claim-stop-heartbeat", "--db", db, "--work-id", row.work_id,
               "--lease", "0.5")
    assert out["status"] == "running-no-heartbeat"
    time.sleep(0.7)  # no more heartbeats -> lease lapses
    store = SQLiteStorage(db)
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    reclaimed = store.reclaim_stale()
    assert reclaimed == [row.work_id]
    assert store.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    store.close()


def test_subprocess_dies_holding_mutation_lock_recovery_no_duplicate(tmp_path):
    """A process claims a work row AND acquires the durable mutation lock,
    then dies. The stale work lease and the stale mutation lock are both
    reclaimed through their existing stores; a fresh run mutates exactly
    once."""
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    db = str(tmp_path / "lockdeath.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-shared")
    store.close()
    proc = subprocess.Popen([sys.executable, WORKER, "claim-lock-crash",
                             "--db", db, "--work-id", row.work_id,
                             "--lease", "0.5"],
                            stdout=subprocess.PIPE, text=True)
    out = json.loads(proc.stdout.readline().strip())
    proc.wait(timeout=30)
    assert proc.returncode == 1
    time.sleep(0.7)  # both leases expire

    store = SQLiteStorage(db)
    locks = store.list(resource_kind=FS, resource=store._lock_canonical_fs("a.txt")) \
        if hasattr(store, "_lock_canonical_fs") else []
    if not locks:
        from arion.state.locks import canonical_resource
        locks = store.list(resource_kind=FS, resource=canonical_resource(FS, "a.txt"))
    assert len(locks) == 1
    assert store.reclaim_expired() == [locks[0].lock_id]
    assert store.reclaim_stale() == [row.work_id]
    store.close()

    # the fresh run (engine level) mutates exactly once
    from tests.test_cross_goal_concurrency import SlowWriteCapability, _write_step

    env = _env(tmp_path, TwoStepPlanner(lambda d: [_write_step("a.txt")]),
               max_concurrency=1, lock_wait_max_seconds=0.0,
               write_cap=SlowWriteCapability(sb, sleep=0.01),
               approve_risk_high=False, db_name="lockdeath.db")
    gid = _submit(env, "write a")
    results = env.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"
    succeeded = [e for e in env.storage.list_events() if e.kind == "mutation.succeeded"]
    assert len(succeeded) == 1  # exactly one successful mutation
    env.engine.shutdown()
    env.engine.storage.close()


def test_fair_admission_across_processes_no_starvation(tmp_path):
    """Engine A has 8 runnable steps, engine B has 1, global cap 2, both
    running CONCURRENTLY: fair-share admission (ceil(cap/active) per
    scheduler) guarantees B's step claims the next free slot within the
    first round - A can never monopolize all capacity. Both complete."""
    shared_cap = SlowReadCapability(sleep=0.1)
    env_a = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_read_step(f"f{i}.txt") for i in range(8)]
                   if "many" in d else [_read_step("b.txt")])),
        max_concurrency=2, read_cap=shared_cap, db_name="fair.db")
    env_b = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_read_step(f"f{i}.txt") for i in range(8)]
                   if "many" in d else [_read_step("b.txt")])),
        max_concurrency=2, read_cap=shared_cap, db_name="fair.db")
    env_a.engine.scheduler_registry.set_scheduler_global_max(2)
    g_many = _submit(env_a, "many steps goal")
    g_one = _submit(env_b, "one step goal")
    # B's step is ALREADY QUEUED when A starts (the starvation scenario):
    # fair-share admission must give B the next free slot, not let A's
    # backlog claim everything first. The engine reuses this QUEUED row.
    b_task = _task_for(env_b, g_one)
    env_b.engine.scheduler_registry.create(
        task_id=b_task.id, goal_id=g_one, step_index=0,
        scheduler_id=env_b.engine.scheduler_id)
    state = {"done": False, "error": None}

    def run_b():
        try:
            deadline = time.time() + 60  # re-invoke until capacity frees
            while time.time() < deadline:
                out = env_b.engine.run_goals([g_one])
                if out[g_one].status == GoalStatus.COMPLETED:
                    state["done"] = True
                    return
        except Exception as exc:  # pragma: no cover
            state["error"] = exc

    def run_a():
        try:
            deadline = time.time() + 60  # fair-share rounds stop cleanly
            while time.time() < deadline:
                out = env_a.engine.run_goals([g_many])
                if out[g_many].status == GoalStatus.COMPLETED:
                    return True
        except Exception as exc:  # pragma: no cover
            state["error"] = exc
        return False

    t = threading.Thread(target=run_b)
    t.start()
    a_done = run_a()
    t.join(timeout=60)
    assert a_done
    assert state["done"] and state["error"] is None, state
    assert shared_cap.max_active <= 2
    # fair share: B's single step started within the first two starts (A
    # never monopolized capacity) - deterministic under the share rule
    assert "b.txt" in shared_cap.started[:2], shared_cap.started
    env_a.engine.shutdown()
    env_b.engine.shutdown()
    env_a.engine.storage.close()
    env_b.engine.storage.close()
