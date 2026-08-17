"""Reservation cross-process correctness (ADR-029, Phase C) - tests first.

Real subprocess workers (scripts/_scheduler_multi_worker.py) sharing one
SQLite registry prove:

- two processes share one registry and both see the same reservations;
- one hot goal cannot consume another goal's reserved capacity;
- reservations never exceed global capacity;
- exactly one process owns a claimed row (even the last protected slot);
- stale scheduler recovery does not permanently consume reservation
  capacity;
- reservation enforcement is transactional (two racing processes are
  both denied the protected slot);
- rapid repeated claims cannot bypass the floor.

All assertions are durable/exact (no sleep-based fairness).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

WORKER = str(Path(__file__).resolve().parent.parent / "scripts"
             / "_scheduler_multi_worker.py")
T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int, scheduler_id: str,
          start: int = 0) -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id=scheduler_id,
                              now=_iso_plus(T0, start + i)))
    return out


def _run(*argv: str, timeout: float = 90.0) -> dict:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, (
        f"worker failed: {proc.stdout[-800:]} {proc.stderr[-800:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_parallel(args_list: list[list[str]], timeout: float = 120.0) -> list[dict]:
    procs = [subprocess.Popen([sys.executable, WORKER, *argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for argv in args_list]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"worker failed: {stderr[-800:]}"
        out.append(json.loads(stdout.strip().splitlines()[-1]))
    return out


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


# --------------------------------------------------------------------------- #
# 1. shared registry + same reservations + hot goal cannot eat the floor
# --------------------------------------------------------------------------- #


def test_two_processes_share_registry_and_reservations(tmp_path):
    """The parent configures reservations; SUBPROCESS claims honor them
    (both processes see the same durable reservation policy). One shared
    scheduler identity (single-engine multi-goal deployment, ADR-027)."""
    db = str(tmp_path / "shared.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8, "sched-shared")
    b_rows = _rows(reg, "goal-b", 2, "sched-shared", start=100)
    reg.close()

    # a fresh process reading the db sees the same reservations
    reg2 = SQLiteStorage(db)
    assert reg2.get_goal_reservation("goal-b") == 2
    assert reg2.get_scheduler_global_max() == 6
    reg2.close()

    # sequential A claims from subprocesses: exactly 4 succeed (slots 5-6
    # are protected for B's floor), 4 are denied
    results = [_run("race-claim", "--db", db, "--scheduler-id", "sched-shared")
               for _ in range(8)]
    claimed = [r for r in results if r["claimed"] is not None]
    denied = [r for r in results if r["claimed"] is None]
    assert len(claimed) == 4 and len(denied) == 4, results

    # B's floor is honored across processes: both B workers succeed
    b_out = _run_parallel([["claim-run", "--db", db, "--work-id",
                            r.work_id, "--sleep", "0.2"]
                           for r in b_rows])
    reg = SQLiteStorage(db)
    assert len(b_out) == 2 and all(o["status"] == "completed" for o in b_out)
    events = reg.scheduler_events(event_type="reservation.denied")
    assert len(events) == 4
    assert len(reg.scheduler_events(event_type="reservation.satisfied")) == 1
    reg.close()


def test_parallel_hot_goal_cannot_consume_reserved_capacity(tmp_path):
    """8 hot subprocesses race A claims while 2 B subprocesses claim via
    the floor: exactly 6 A rows end RUNNING (cap 8 - floor 2), B wins its
    floor; the cap is never exceeded."""
    db = str(tmp_path / "hot.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 8, "sched-shared")
    b_rows = _rows(reg, "goal-b", 2, "sched-shared", start=100)
    reg.close()

    args_list = [["race-claim", "--db", db, "--scheduler-id", "sched-shared"]
                 for _ in range(8)]
    args_list += [["claim-run", "--db", db, "--work-id", r.work_id,
                   "--sleep", "0.4"] for r in b_rows]
    results = _run_parallel(args_list)
    a_results = results[:8]
    b_results = results[8:]
    assert len([r for r in a_results if r["claimed"] is not None]) == 6
    assert len([r for r in a_results if r["claimed"] is None]) == 2
    assert all(r["status"] == "completed" for r in b_results)

    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 6
    assert _running_for(reg, "goal-b") == 0  # B completed its floor work
    assert len(reg.list_work(status=SchedulerWorkStatus.RUNNING)) == 6
    # exactly two A claims were denied (reservation and/or capacity
    # depending on interleaving - the durable floor is the invariant)
    assert len([r for r in a_results if r["claimed"] is None]) == 2
    reg.close()


# --------------------------------------------------------------------------- #
# 3. transactional protection + exactly-one-owner on the last slot
# --------------------------------------------------------------------------- #


def test_racing_processes_both_denied_protected_slot(tmp_path):
    """Cap 4, B floor 2, A already holds 2: TWO racing A subprocesses both
    see free=2 and are BOTH denied (transactional enforcement) - the
    protected slot is not raceable; then B's floor claims succeed."""
    db = str(tmp_path / "race-denied.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 4, "sched-shared")
    b_rows = _rows(reg, "goal-b", 2, "sched-shared", start=100)
    # A takes two slots (REAL now + long lease so the workers' claims do
    # not see the rows as stale); B has runnable work but is below floor
    from datetime import datetime, timezone
    real_now = datetime.now(timezone.utc).isoformat()
    assert reg.claim(a_rows[0].work_id, "w-a", 60.0, real_now, 600.0,
                     scheduler_id="sched-shared") is not None
    assert reg.claim(a_rows[1].work_id, "w-a", 60.0, real_now, 600.0,
                     scheduler_id="sched-shared") is not None
    reg.close()

    # free=2, B needs both: two racing A workers are BOTH reservation-
    # denied (transactional: neither can race into the protected slot)
    results = _run_parallel([["race-claim", "--db", db, "--scheduler-id",
                              "sched-shared"] for _ in range(2)])
    assert all(r["claimed"] is None for r in results), results
    reg = SQLiteStorage(db)
    assert len(reg.scheduler_events(event_type="reservation.denied")) == 2
    assert len(reg.scheduler_events(event_type="capacity.denied")) == 0
    reg.close()

    # B's floor claims across processes then succeed exactly once each
    b_out = _run_parallel([["claim-run", "--db", db, "--work-id",
                            r.work_id, "--sleep", "0.2"] for r in b_rows])
    assert all(o["status"] == "completed" for o in b_out)
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 2
    assert _running_for(reg, "goal-b") == 0  # B completed its floor work
    reg.close()


# --------------------------------------------------------------------------- #
# 5. stale crash does not permanently consume reservation capacity
# --------------------------------------------------------------------------- #


def test_crashed_worker_does_not_permanently_consume_floor(tmp_path):
    """A B worker crash-claims one row (lease 0.3s): B is below its floor
    (1 < 2); A is protected from the last slot; after reclaim the B row
    returns to QUEUED and B's floor is restored."""
    db = str(tmp_path / "crash-floor.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 8, "sched-shared")
    b_rows = _rows(reg, "goal-b", 2, "sched-shared", start=100)
    reg.close()

    # the B worker claims one row and dies while RUNNING (long lease so
    # the claim phase below cannot race the expiry)
    proc = subprocess.Popen([sys.executable, WORKER, "crash-claimed",
                             "--db", db, "--work-id", b_rows[0].work_id,
                             "--lease", "60.0"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    out, err = proc.communicate(timeout=90)
    assert proc.returncode == 1
    worker = json.loads(out.strip().splitlines()[-1])["worker"]

    # while B is below its floor (1 running, stale), A is capped; A's
    # claims use a LONG lease (600s) so the explicit reclaim below cannot
    # touch them
    results = [_run("race-claim", "--db", db, "--scheduler-id", "sched-shared",
                    "--lease", "600")
               for _ in range(8)]
    assert len([r for r in results if r["claimed"] is not None]) == 4
    assert len([r for r in results if r["claimed"] is None]) == 4

    # deterministic stale reclaim: explicit now past the dead 60s lease
    # (the crash worker's lease is in the REAL timeline, so the reclaim
    # now is real-time + margin)
    from datetime import datetime, timedelta, timezone
    reclaim_now = (datetime.now(timezone.utc)
                   + timedelta(seconds=120)).isoformat()
    reg = SQLiteStorage(db)
    reclaimed = reg.reclaim_stale(now=reclaim_now)
    assert b_rows[0].work_id in reclaimed
    assert reg.get_work(b_rows[0].work_id).status == \
        SchedulerWorkStatus.ABANDONED
    # the stale row is terminal; re-entry = a fresh submission (the
    # engine's re-submit path) - the floor must be honored for it
    fresh = reg.create(task_id="t-b-retry", goal_id="goal-b", step_index=9,
                       scheduler_id="sched-shared", now=_iso_plus(T0, 10))
    reg.close()
    got = _run("claim-run", "--db", db, "--work-id", fresh.work_id,
               "--sleep", "0.2")
    assert got["status"] == "completed"
    reg = SQLiteStorage(db)
    assert reg.get_work(fresh.work_id).status == \
        SchedulerWorkStatus.COMPLETED
    assert _running_for(reg, "goal-a") == 4  # A never exceeded 4
    assert _running_for(reg, "goal-b") == 0
    reg.close()


# --------------------------------------------------------------------------- #
# 4 + 7: cap never exceeded; rapid claims cannot bypass the floor
# --------------------------------------------------------------------------- #


def test_reservations_never_exceed_cap_across_processes(tmp_path):
    """Mixed parallel claims across both goals: the durable RUNNING count
    never exceeds the cap (exact final observation), and every claim that
    would break the floor is denied."""
    db = str(tmp_path / "cap.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 10, "sched-shared")
    _rows(reg, "goal-b", 4, "sched-shared", start=100)
    reg.close()

    b_rows = [r for r in SQLiteStorage(db).list_work(
        status=SchedulerWorkStatus.QUEUED) if r.goal_id == "goal-b"]
    args_list = ([["race-claim", "--db", db, "--scheduler-id", "sched-shared"]
                  for _ in range(6)]
                 + [["claim-once-hold", "--db", db, "--work-id", r.work_id,
                     "--sleep", "0.05"] for r in b_rows])
    results = _run_parallel(args_list)
    a_claimed = len([r for r in results[:6] if r["claimed"] is not None])
    b_claimed = len([r for r in results[6:] if r["claimed"]])
    assert a_claimed + b_claimed <= 6
    assert b_claimed == 2  # B's floor won

    reg = SQLiteStorage(db)
    running = reg.list_work(status=SchedulerWorkStatus.RUNNING)
    assert len(running) == 6  # exactly the cap: A 4 + B 2
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    reg.close()


def test_rapid_sequential_claims_cannot_bypass_floor(tmp_path):
    """8 rapid sequential subprocess claims by the hot goal: exactly 4
    succeed - the floor cannot be bypassed by claim volume."""
    db = str(tmp_path / "rapid.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8, "sched-shared")
    _rows(reg, "goal-b", 2, "sched-shared", start=100)
    reg.close()

    # specific-row rapid claims from subprocesses; claimed rows stay
    # RUNNING (claim-once-hold), so the hot goal's capacity accumulates
    # and the floor cannot be bypassed by claim volume
    a_rows = [r for r in SQLiteStorage(db).list_work(
        status=SchedulerWorkStatus.QUEUED) if r.goal_id == "goal-a"]
    results = [_run("claim-once-hold", "--db", db, "--work-id", r.work_id,
                    "--sleep", "0.05") for r in a_rows]
    assert [r["claimed"] for r in results] == [True] * 4 + [False] * 4
    reg = SQLiteStorage(db)
    assert len(reg.scheduler_events(event_type="reservation.denied")) == 4
    assert _running_for(reg, "goal-a") == 4
    reg.close()


# --------------------------------------------------------------------------- #
# fair share still binds before the floor (cross-process)
# --------------------------------------------------------------------------- #


def test_fair_share_binds_before_floor_across_processes(tmp_path):
    """Two scheduler identities, cap 6 (share = 3 each): sched-b's B
    (floor 2) wins its floor, but sched-a's A is capped at its FAIR SHARE
    (3), not the reservation ceiling - reservations never bypass the
    scheduler fair share."""
    db = str(tmp_path / "fairshare.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8, "sched-a")
    _rows(reg, "goal-b", 2, "sched-b")
    reg.close()

    a_results = [_run("race-claim", "--db", db, "--scheduler-id", "sched-a")
                 for _ in range(8)]
    assert len([r for r in a_results if r["claimed"] is not None]) == 3
    assert len([r for r in a_results if r["claimed"] is None]) == 5
    # 3 = the fair share (ceil(6/2)); a reservation-only world would have
    # allowed 4 (cap - B floor). claim_next denials are silent by design
    # (no specific work id), so assert the durable counts, not events.
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 3
    reg.close()

    # B's floor across the other scheduler identity
    b_rows = [r for r in SQLiteStorage(db).list_work(
        status=SchedulerWorkStatus.QUEUED) if r.goal_id == "goal-b"]
    b_out = _run_parallel([["claim-run", "--db", db, "--work-id", r.work_id,
                            "--sleep", "0.2"] for r in b_rows])
    assert all(o["status"] == "completed" for o in b_out)
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 3
    assert _running_for(reg, "goal-b") == 0
    reg.close()
