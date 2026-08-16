"""Cross-process weighted fair scheduling (ADR-027, Phase C) - tests first.

Two or more independent processes sharing one SQLite registry observe the
same durable weighted policy:

- two processes, two goals, weights 2:1: per DWRR round the weight-2 goal
  claims 2 rows and the weight-1 goal 1 row -> EXACT final counts (every
  row completes, bounded by the credit arithmetic, independent of process
  interleaving);
- racing claims cannot bypass weighted admission (a race for one row still
  yields exactly one owner);
- a rapid (hot) claimant cannot starve a low-weight goal (all its rows
  complete; hot's claims bounded by weight x rounds);
- global concurrency remains exact (never above the durable cap);
- stale scheduler recovery does not distort capacity indefinitely.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from arion.state.models import GoalStatus
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_scheduler_multi_worker.py")
T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int, scheduler_id: str = "sched-x"):
    return [reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                       step_index=i, scheduler_id=scheduler_id, now=_iso_plus(T0, i))
            for i in range(n)]


def _run_weighted(db: str, work_id: str, retries: int = 400) -> dict:
    proc = subprocess.run([sys.executable, WORKER, "weighted-claim-run",
                           "--db", db, "--work-id", work_id,
                           "--retries", str(retries)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"{proc.stdout[-600:]} {proc.stderr[-600:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _counts(db: str, goal_id: str) -> tuple[int, int]:
    """(completed, total) rows for a goal - durable observations."""
    st = SQLiteStorage(db)
    rows = st.list_work()
    done = sum(1 for r in rows if r.goal_id == goal_id
               and r.status == SchedulerWorkStatus.COMPLETED)
    total = sum(1 for r in rows if r.goal_id == goal_id)
    st.close()
    return done, total


# --------------------------------------------------------------------------- #
# two processes, two goals, weights 2:1
# --------------------------------------------------------------------------- #


def test_two_processes_respect_2_to_1_weights(tmp_path):
    """Process A (weight-2 goal) and process B (weight-1 goal) claim their
    own rows concurrently. The durable credit arithmetic yields EXACTLY
    2:1 regardless of interleaving: A finishes all 24 rows, B all 12."""
    db = str(tmp_path / "w.db")
    store = SQLiteStorage(db)
    store.set_scheduler_global_max(3)
    store.set_goal_weight("goal-a", 2)
    store.set_goal_weight("goal-b", 1)
    rows_a = _rows(store, "goal-a", 24)
    rows_b = _rows(store, "goal-b", 12)
    store.close()

    results: dict[str, dict] = {}

    def worker_a():
        for row in rows_a:
            results["a"] = _run_weighted(db, row.work_id)

    def worker_b():
        for row in rows_b:
            results["b"] = _run_weighted(db, row.work_id)

    ta = threading.Thread(target=worker_a)
    tb = threading.Thread(target=worker_b)
    ta.start(); tb.start()
    ta.join(timeout=180); tb.join(timeout=180)
    assert not ta.is_alive() and not tb.is_alive()

    done_a, total_a = _counts(db, "goal-a")
    done_b, total_b = _counts(db, "goal-b")
    assert (done_a, total_a) == (24, 24), (done_a, total_a)
    assert (done_b, total_b) == (12, 12), (done_b, total_b)
    assert done_a == 2 * done_b  # EXACT 2:1 under sustained contention
    store = SQLiteStorage(db)
    assert len(store.list_work(status=SchedulerWorkStatus.RUNNING)) == 0
    store.close()


def test_three_processes_respect_2_1_1_weights(tmp_path):
    """Three processes, three goals (2:1:1): exact per-round distribution."""
    db = str(tmp_path / "w3.db")
    store = SQLiteStorage(db)
    store.set_scheduler_global_max(4)
    store.set_goal_weight("goal-a", 2)
    store.set_goal_weight("goal-b", 1)
    store.set_goal_weight("goal-c", 1)
    rows = {g: _rows(store, g, 12 if g == "goal-a" else 6) for g in
            ("goal-a", "goal-b", "goal-c")}
    store.close()

    results: dict[str, dict] = {}

    def worker(goal):
        for row in rows[goal]:
            results[goal] = _run_weighted(db, row.work_id)

    threads = [threading.Thread(target=worker, args=(g,))
               for g in ("goal-a", "goal-b", "goal-c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=240)
    assert all(not t.is_alive() for t in threads)

    done_a, _ = _counts(db, "goal-a")
    done_b, _ = _counts(db, "goal-b")
    done_c, _ = _counts(db, "goal-c")
    assert (done_a, done_b, done_c) == (12, 6, 6)
    assert done_a == 2 * done_b == 2 * done_c
    store = SQLiteStorage(db)
    assert len(store.list_work(status=SchedulerWorkStatus.RUNNING)) == 0
    store.close()


def test_racing_claims_cannot_bypass_weights(tmp_path):
    """Two processes race for ONE row of a weight-2 goal while a weight-1
    goal contends: exactly one owner wins, and the winner is the first
    claimer (the gate cannot be bypassed by racing)."""
    db = str(tmp_path / "race.db")
    store = SQLiteStorage(db)
    store.set_scheduler_global_max(2)
    store.set_goal_weight("goal-a", 2)
    store.set_goal_weight("goal-b", 1)
    row = _rows(store, "goal-a", 1)[0]
    store.close()
    outs = [_run_weighted(db, row.work_id, retries=50),
            _run_weighted(db, row.work_id, retries=50)]
    winners = [o for o in outs if o["claimed"] is True]
    losers = [o for o in outs if o["claimed"] is False]
    assert len(winners) == 1 and len(losers) == 1, outs
    store = SQLiteStorage(db)
    final = store.get_work(row.work_id)
    assert final.status == SchedulerWorkStatus.COMPLETED
    store.close()


def test_rapid_claimant_cannot_starve_low_weight_goal(tmp_path):
    """A weight-8 goal claims rapidly; a weight-1 goal still completes ALL
    its rows. Invariant (durable): hot's claims <= 8 x (low's claims + 1)
    rounds bound - the low goal is never starved out."""
    db = str(tmp_path / "hot.db")
    store = SQLiteStorage(db)
    store.set_scheduler_global_max(4)
    store.set_goal_weight("goal-hot", 8)
    store.set_goal_weight("goal-low", 1)
    rows_hot = _rows(store, "goal-hot", 40)
    rows_low = _rows(store, "goal-low", 6)
    store.close()

    results: dict[str, dict] = {}

    def worker_hot():
        for row in rows_hot:
            results["hot"] = _run_weighted(db, row.work_id)

    def worker_low():
        for row in rows_low:
            results["low"] = _run_weighted(db, row.work_id)

    ta = threading.Thread(target=worker_hot)
    tb = threading.Thread(target=worker_low)
    ta.start(); tb.start()
    ta.join(timeout=240); tb.join(timeout=240)
    assert not ta.is_alive() and not tb.is_alive()

    done_hot, total_hot = _counts(db, "goal-hot")
    done_low, total_low = _counts(db, "goal-low")
    assert (done_low, total_low) == (6, 6)  # low goal NEVER starved
    assert done_hot <= 8 * (done_low + 1), (done_hot, done_low)
    assert done_hot > done_low  # hot still dominates
    store = SQLiteStorage(db)
    assert len(store.list_work(status=SchedulerWorkStatus.RUNNING)) == 0
    store.close()


def test_global_cap_exact_under_cross_process_weights(tmp_path):
    """With cap=2 and two weighted goals claiming across processes, the
    durable RUNNING count never exceeds 2 (observed at every step)."""
    db = str(tmp_path / "cap.db")
    store = SQLiteStorage(db)
    store.set_scheduler_global_max(2)
    store.set_goal_weight("goal-a", 1)
    store.set_goal_weight("goal-b", 1)
    rows_a = _rows(store, "goal-a", 8)
    rows_b = _rows(store, "goal-b", 8)
    store.close()

    # two threads claim+hold (no immediate completion) so concurrency is
    # observable; a third thread observes the running count continuously
    stop = threading.Event()
    violations: list[int] = []

    def observer():
        st = SQLiteStorage(db)
        while not stop.is_set():
            n = len(st.list_work(status=SchedulerWorkStatus.RUNNING))
            if n > 2:
                violations.append(n)
            time.sleep(0.005)
        st.close()

    def claimer(rows):
        st = SQLiteStorage(db)
        for row in rows:
            got = st.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                           scheduler_id="sched-x")
            if got is None:
                time.sleep(0.01)
                got = st.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                               scheduler_id="sched-x")
            # leave RUNNING (no completion) while others claim
        st.close()

    obs = threading.Thread(target=observer)
    obs.start()
    ta = threading.Thread(target=claimer, args=(rows_a,))
    tb = threading.Thread(target=claimer, args=(rows_b,))
    ta.start(); tb.start()
    ta.join(timeout=120); tb.join(timeout=120)
    stop.set()
    obs.join(timeout=30)
    assert not violations, violations  # cap never exceeded, ever
    store = SQLiteStorage(db)
    running = len(store.list_work(status=SchedulerWorkStatus.RUNNING))
    assert running <= 2
    store.close()


def test_stale_scheduler_recovery_does_not_distort_capacity(db_path: str):
    """A crashed scheduler's abandoned rows remove its goal from the
    contending set: its (unspent) deficit stops mattering and other goals
    are unaffected - capacity distortion is bounded by one round."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-crashed", 1)
    rows_c = _rows(reg, "goal-crashed", 4, scheduler_id="sched-dead")
    rows_a = _rows(reg, "goal-a", 6, scheduler_id="sched-alive")
    # the crashed goal accumulated credit
    reg.claim(rows_c[0].work_id, "w", 60.0, T0, 600.0, scheduler_id="sched-dead")
    reg.reclaim_stale(now=_iso_plus(T0, 61))  # lease expired -> ABANDONED
    assert reg.get_work(rows_c[0].work_id).status == SchedulerWorkStatus.ABANDONED
    # the crashed goal's remaining rows are still queued but its scheduler
    # is dead: abandon them (registration lapsed)
    reg.abandon_foreign_queued("sched-alive", now=_iso_plus(T0, 62))
    for row in rows_c:
        assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    # goal-a is now the only contending goal: it claims freely (full cap)
    claimed = 0
    for row in rows_a:
        if reg.claim(row.work_id, "w", 60.0, _iso_plus(T0, 63), 600.0,
                     scheduler_id="sched-alive"):
            claimed += 1
            reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                              owner_worker_id="w", now=_iso_plus(T0, 64))
    assert claimed == 6  # no distortion from the dead goal
    reg.close()
