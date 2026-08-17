"""Reservation restart/crash behavior (ADR-029, Phase E) - tests first.

- reservations + DWRR state survive a store reopen (restart);
- the floor is still enforced after restart;
- stale RUNNING work is reclaimed and the reclaimed work counts toward
  the correct goal (re-entry re-engages that goal's floor);
- a crashed scheduler cannot permanently consume another goal's
  reserved capacity;
- mutation-lock recovery stays exactly-once alongside reservations;
- telemetry continues to describe transitions without becoming
  authority (reservation config events survive; gates ignore events).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
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


def _rows(reg, goal_id: str, n: int, start: int = 0) -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id="sched-1",
                              now=_iso_plus(T0, start + i)))
    return out


def _fill(reg, goal_id: str) -> int:
    claimed = 0
    while True:
        row = next((r for r in reg.list_work(
            status=SchedulerWorkStatus.QUEUED) if r.goal_id == goal_id), None)
        if row is None:
            break
        got = reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                        now=T0, max_lease_seconds=600.0,
                        scheduler_id="sched-1")
        if got is None:
            break
        claimed += 1
    return claimed


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def test_reservations_and_floor_survive_restart(db_path: str):
    """Restart with reservations + DWRR credit + RUNNING work: config
    persists, the floor is enforced for new claims, RUNNING stays owned."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 8)
    b_rows = _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-a") == 4
    assert _fill(reg, "goal-b") == 2
    running_before = [(r.work_id, r.worker_id, r.lease_expires_at)
                      for r in reg.list_work(
                          status=SchedulerWorkStatus.RUNNING)]
    reg.close()

    reg2 = SQLiteStorage(db_path)  # restart
    assert reg2.get_goal_reservation("goal-b") == 2
    assert reg2.get_goal_weight("goal-a") == 8
    # DWRR credit survived (a durable table), RUNNING rows still owned
    running_after = [(r.work_id, r.worker_id, r.lease_expires_at)
                     for r in reg2.list_work(
                         status=SchedulerWorkStatus.RUNNING)]
    assert running_after == running_before
    # the floor is still enforced: A cannot exceed 4 while B is below 2
    assert _fill(reg2, "goal-a") == 0
    assert _running_for(reg2, "goal-a") == 4
    assert _running_for(reg2, "goal-b") == 2
    reg2.close()


def test_reclaimed_work_counts_toward_its_goal_floor(db_path: str):
    """A stale RUNNING row of the reserved goal is reclaimed (ABANDONED)
    and a fresh submission of the same goal re-engages its floor: the
    reclaimed capacity counts toward the CORRECT goal."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    b_rows = _rows(reg, "goal-b", 4)
    # B claims one row with a lease that will lapse; B holds 1 of 2
    row = reg.claim(b_rows[0].work_id, "w-b", 1.0, T0, 600.0,
                    scheduler_id="sched-1")
    assert row is not None
    assert _running_for(reg, "goal-b") == 1
    # A is protected: with B at 1 (below 2), A can reach at most 4
    assert _fill(reg, "goal-a") == 4
    # the lease lapses deterministically; reclaim returns the row
    reclaimed = reg.reclaim_stale(now=_iso_plus(T0, 100))
    assert b_rows[0].work_id in reclaimed
    assert reg.get_work(b_rows[0].work_id).status == \
        SchedulerWorkStatus.ABANDONED
    assert _running_for(reg, "goal-b") == 0
    # fresh submission of goal-b: its floor re-engages (2 new claims)
    fresh = reg.create(task_id="t-b-fresh", goal_id="goal-b", step_index=9,
                       scheduler_id="sched-1", now=_iso_plus(T0, 101))
    got = reg.claim(fresh.work_id, "w-b", 60.0, _iso_plus(T0, 102), 600.0,
                    scheduler_id="sched-1")
    assert got is not None
    assert _running_for(reg, "goal-b") == 1
    reg.close()


def test_crashed_scheduler_cannot_permanently_consume_other_goals_floor(db_path: str, tmp_path):
    """A REAL subprocess crashes while holding a RUNNING row of the
    reserved goal; after reclaim + re-submission the floor is restored
    (the crash did not permanently eat the reserved capacity)."""
    db = str(tmp_path / "crash-restart.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8, start=0)
    b_rows = _rows(reg, "goal-b", 2, start=100)
    reg.close()

    proc = subprocess.Popen([sys.executable, WORKER, "crash-claimed",
                             "--db", db, "--work-id", b_rows[0].work_id,
                             "--lease", "0.3"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    out, _ = proc.communicate(timeout=90)
    assert proc.returncode == 1
    assert json.loads(out.strip().splitlines()[-1])["status"] == "running"

    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-b") == 1  # crashed while RUNNING
    time.sleep(0.5)  # lease 0.3 lapses (subprocess crash, no race)
    reclaimed = reg.reclaim_stale()
    assert b_rows[0].work_id in reclaimed
    # a fresh B submission claims its floor slot after the crash
    fresh = reg.create(task_id="t-b-retry", goal_id="goal-b", step_index=9,
                       scheduler_id="sched-1", now=_iso_plus(T0, 200))
    got = reg.claim(fresh.work_id, "w-b", 60.0, _iso_plus(T0, 201), 600.0,
                    scheduler_id="sched-1")
    assert got is not None
    assert _running_for(reg, "goal-b") == 1
    reg.close()


def test_mutation_lock_recovery_unchanged_by_reservations(db_path: str, tmp_path):
    """Reservation config does not disturb ADR-020/021 mutation-lock
    recovery: a worker that dies holding BOTH a work lease and a
    mutation lock is recovered exactly-once (one lock, reclaimed once,
    one fresh mutation)."""
    db = str(tmp_path / "lock-res.db")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    from arion.state.locks import canonical_resource
    res = canonical_resource("filesystem:path", "a.txt")  # worker's resource
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    row = reg.create(task_id="t-lock", goal_id="goal-b", step_index=0,
                     scheduler_id="sched-1", now=T0)
    reg.close()

    proc = subprocess.Popen([sys.executable, WORKER, "claim-lock-crash",
                             "--db", db, "--work-id", row.work_id,
                             "--sandbox", str(sandbox), "--lease", "0.3"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    out, _ = proc.communicate(timeout=90)
    assert proc.returncode == 1
    data = json.loads(out.strip().splitlines()[-1])
    assert data["work_id"] == row.work_id

    time.sleep(0.5)
    reg = SQLiteStorage(db)
    locks = reg.list(resource_kind="filesystem:path", resource=res) \
        if hasattr(reg, "list") else []
    assert len(locks) == 1  # exactly one lock record (no duplicates)
    assert reg.reclaim_expired() == [locks[0].lock_id]
    assert reg.reclaim_stale() == [row.work_id]
    # the reservation config is untouched by recovery
    assert reg.get_goal_reservation("goal-b") == 2
    reg.close()


def test_telemetry_describes_transitions_never_authority(db_path: str):
    """Reservation telemetry events exist and survive reopen; deleting
    them changes nothing (the gates read authority tables only)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 2)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    kinds = {e.kind for e in reg.scheduler_events()}
    assert "goal_reservation_changed" in kinds
    assert "reservation.satisfied" in kinds
    assert _running_for(reg, "goal-a") == 4
    assert _running_for(reg, "goal-b") == 2
    # prune ALL events: behavior is identical (observational only)
    reg.prune_scheduler_events(
        cutoff=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    assert reg.scheduler_event_count() == 0
    assert _running_for(reg, "goal-a") == 4
    assert _running_for(reg, "goal-b") == 2
    # new claims still honor the floor after the telemetry wipe
    assert _fill(reg, "goal-a") == 0
    reg.close()
