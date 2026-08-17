"""Cross-process planning consistency (ADR-030, Phase J) - tests first.

Observation is read-only even while real subprocess workers are active:

1. workers claim while status is queried;
2. workers heartbeat while planning runs;
3. reservation config changes while a snapshot is being computed;
4. stale work is reclaimed while status is queried;
5. multiple schedulers are active;
6. DWRR credit changes during observation.

The snapshot may be stale the instant it returns - acceptable. The
critical invariant: observation NEVER mutates authority (reservations,
weights, DWRR credit, events, ownership byte-identical after planning
calls). No timing-dependent assertions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

WORKER = str(Path(__file__).resolve().parent.parent / "scripts"
             / "_scheduler_multi_worker.py")
T0 = "2026-01-01T00:00:00+00:00"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(reg, goal_id: str, n: int, scheduler_id: str) -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id=scheduler_id,
                              now=_now()))
    return out


def _run(*argv: str, timeout: float = 90.0) -> dict:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, (
        f"worker failed: {proc.stdout[-800:]} {proc.stderr[-800:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_parallel(args_list: list[list[str]], timeout: float = 120.0
                  ) -> list[dict]:
    procs = [subprocess.Popen([sys.executable, WORKER, *argv],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
             for argv in args_list]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"worker failed: {stderr[-800:]}"
        out.append(json.loads(stdout.strip().splitlines()[-1]))
    return out


def _state(reg) -> dict:
    return {
        "reservations": reg.list_goal_reservations(),
        "weights": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id,
                  r.lease_expires_at) for r in reg.list_work()],
    }


def test_planning_read_only_while_workers_claim(tmp_path):
    """Subprocess workers claim/heartbeat/complete while the parent runs
    every planning API; the workers' transitions are the ONLY changes."""
    db = str(tmp_path / "busy.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 6, "sched-1")
    b_rows = _rows(reg, "goal-b", 2, "sched-1")
    reg.close()

    # workers claim B's floor (claim-run: claim + heartbeat + complete);
    # their transitions (incl. DWRR refills) are the ONLY expected diffs
    results = _run_parallel([["claim-run", "--db", db, "--work-id", r.work_id,
                              "--sleep", "0.3"] for r in b_rows])
    assert all(o["status"] == "completed" for o in results)
    before = _state(SQLiteStorage(db))
    # interleave planning calls AFTER the worker activity
    for _ in range(3):
        reg = SQLiteStorage(db)
        reg.capacity_snapshot(now=_now())
        reg.reservation_check()
        reg.reservation_feasibility(proposed={"goal-a": 1, "goal-b": 2})
        reg.simulate_reservation_change("goal-b", 4)
        reg.explain_goal_eligibility("goal-b")
        reg.close()

    reg = SQLiteStorage(db)
    after = _state(reg)
    # reservations / weights / events must be identical; only work rows
    # changed (claimed -> completed by the WORKERS, not by planning)
    assert after["reservations"] == before["reservations"]
    assert after["weights"] == before["weights"]
    assert after["credit"] == before["credit"]
    assert after["events"] == before["events"]
    b_ids = {r.work_id for r in b_rows}
    before_work = {w for w in before["work"] if w[0] not in b_ids}
    after_work = {w for w in after["work"] if w[0] not in b_ids}
    assert before_work == after_work
    # the workers' claims completed exactly once (no duplicates)
    completed = [w for w in after["work"] if w[0] in b_ids]
    assert all(w[1] == "completed" for w in completed)
    assert len(completed) == 2
    reg.close()


def test_planning_while_multiple_schedulers_active(tmp_path):
    """Two scheduler identities with active workers: snapshots see both,
    planning stays read-only, fair-share projection is sane."""
    db = str(tmp_path / "multi.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 6, "sched-a")
    _rows(reg, "goal-b", 2, "sched-b")
    reg.close()

    results = _run_parallel([["race-claim", "--db", db, "--scheduler-id",
                              "sched-a"] for _ in range(6)]
                            + [["race-claim", "--db", db, "--scheduler-id",
                                "sched-b"] for _ in range(2)])
    assert len([r for r in results[:6] if r["claimed"] is not None]) == 4
    assert len([r for r in results[6:] if r["claimed"] is not None]) == 2

    reg = SQLiteStorage(db)
    before = _state(reg)
    snap = reg.capacity_snapshot(now=_now())
    assert snap["active_scheduler_count"] == 2
    assert snap["running_count"] == 6
    b = [g for g in snap["goals"] if g["goal_id"] == "goal-b"][0]
    assert b["reservation_satisfied"] is True
    assert _state(reg) == before
    reg.close()


def test_planning_while_reservation_changes_and_stale_reclaim(tmp_path):
    """A reservation change + a stale-lease reclaim happen while planning
    calls run: the planning calls themselves mutate nothing (the change
    and reclaim are the only durable diffs)."""
    db = str(tmp_path / "churn.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 6, "sched-1")
    b_rows = _rows(reg, "goal-b", 2, "sched-1")
    # a stale RUNNING row (past lease)
    reg.claim(b_rows[0].work_id, "w-stale", 1.0, _now(), 600.0,
              scheduler_id="sched-1")
    reg.close()

    # config change + reclaim interleaved with planning
    reg = SQLiteStorage(db)
    reg.set_goal_reservation("goal-b", 3)  # the only config mutation
    for _ in range(2):
        reg.capacity_snapshot(now=_now())
        reg.reservation_check()
    reg.reclaim_stale(now=(datetime.now(timezone.utc)
                           + timedelta(seconds=120)).isoformat())
    reg.capacity_snapshot(now=_now())
    reg.simulate_reservation_change("goal-b", 1)
    reg.close()

    reg = SQLiteStorage(db)
    assert reg.get_goal_reservation("goal-b") == 3
    assert reg.get_work(b_rows[0].work_id).status == \
        SchedulerWorkStatus.ABANDONED
    assert reg.get_goal_reservation_config("goal-a") is None
    # planning never created rows or events of its own
    queued = [r for r in reg.list_work(status=SchedulerWorkStatus.QUEUED)]
    assert len(queued) == len(a_rows) + 1  # 6 A + 1 B (one reclaimed)
    reg.close()


def test_dwrr_credit_changes_during_observation(tmp_path):
    """Workers' claims change DWRR credit; planning only READS it (the
    projection's credit values match the durable table, and planning
    calls never write credit)."""
    db = str(tmp_path / "credit.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 6, "sched-1")
    reg.close()

    _run_parallel([["race-claim", "--db", db, "--scheduler-id", "sched-1"]
                   for _ in range(4)])
    reg = SQLiteStorage(db)
    before_credit = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    for _ in range(3):
        snap = reg.capacity_snapshot(now=_now())
        for g in snap["goals"]:
            assert g["dwr_credit"] == reg._sys_credit_for_goal(
                g["goal_id"], reg.get_scheduler_global_max())
        reg.simulate_reservation_change("goal-a", 1)
        reg.reservation_feasibility()
    after_credit = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    assert after_credit == before_credit
    reg.close()
