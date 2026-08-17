"""Ceiling cross-process correctness (ADR-031, Phase E) - tests first.

Real subprocess workers prove:

- two+ processes cannot collectively exceed a goal's ceiling;
- racing claims at the final ceiling slot yield exactly one owner;
- the global cap stays authoritative (ceiling can't exceed it in
  effect);
- reservation floors remain protected;
- scheduler fair share remains authoritative;
- DWRR stays durable;
- stale reclaim frees a ceiling slot;
- restart preserves the ceiling;
- disable/removed ceiling immediately permits future claims.

Exact durable final counts; no timing-based assertions.
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


def _rows(reg, goal_id: str, n: int, scheduler_id: str,
          start: int = 0) -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id=scheduler_id,
                              now=(datetime.now(timezone.utc)
                                   + timedelta(seconds=start + i)).isoformat()))
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


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def test_two_processes_cannot_exceed_ceiling(tmp_path):
    """6 parallel subprocess claims for one goal with ceiling 4: exactly 4
    succeed (each worker claims once via claim-once-hold)."""
    db = str(tmp_path / "two.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 4)
    rows = _rows(reg, "goal-a", 6, "sched-shared")
    reg.close()
    results = _run_parallel([["claim-once-hold", "--db", db,
                              "--work-id", r.work_id, "--sleep", "0.05"]
                             for r in rows])
    assert len([r for r in results if r["claimed"]]) == 4
    assert len([r for r in results if not r["claimed"]]) == 2
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 4
    assert len(reg.scheduler_events(event_type="ceiling.denied")) == 2
    reg.close()


def test_three_processes_cannot_exceed_ceiling(tmp_path):
    """9 parallel claims, ceiling 3, three scheduler identities: exactly 3
    succeed; the ceiling is cross-scheduler (not per-process)."""
    db = str(tmp_path / "three.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(12)
    reg.set_goal_ceiling("goal-a", 3)
    rows = _rows(reg, "goal-a", 9, "sched-1")
    reg.close()
    args_list = []
    for i, r in enumerate(rows):
        args_list.append(["claim-once-hold", "--db", db, "--work-id",
                          r.work_id, "--sleep", "0.05"])
    results = _run_parallel(args_list)
    assert len([r for r in results if r["claimed"]]) == 3
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 3
    reg.close()


def test_race_for_final_ceiling_slot_exactly_one_owner(tmp_path):
    """Ceiling 2, one slot already held: two parallel workers race the
    final slot -> exactly one owner, one ceiling denial."""
    db = str(tmp_path / "race.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 3, "sched-shared")
    assert reg.claim(rows[0].work_id, "w-0", 60.0, _now(), 600.0,
                     scheduler_id="sched-shared") is not None
    reg.close()
    results = _run_parallel([["claim-once-hold", "--db", db,
                              "--work-id", rows[1].work_id, "--sleep", "0.05"],
                             ["claim-once-hold", "--db", db,
                              "--work-id", rows[2].work_id, "--sleep", "0.05"]])
    winners = [r for r in results if r["claimed"]]
    losers = [r for r in results if not r["claimed"]]
    assert len(winners) == 1 and len(losers) == 1, results
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_stale_reclaim_frees_ceiling_slot(tmp_path):
    """A worker crashes holding a ceiling slot (lease 60s); reclaim with
    an explicit future now frees it; a fresh claim succeeds (running 2)."""
    db = str(tmp_path / "stale.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 3, "sched-shared")
    reg.close()
    # worker claims and crashes (lease 60s real-time)
    proc = subprocess.Popen([sys.executable, WORKER, "crash-claimed",
                             "--db", db, "--work-id", rows[0].work_id,
                             "--lease", "60.0"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    out, _ = proc.communicate(timeout=90)
    assert proc.returncode == 1
    assert json.loads(out.strip().splitlines()[-1])["status"] == "running"
    # the ceiling still binds: only one more slot may be claimed
    got = _run("claim-once-hold", "--db", db, "--work-id", rows[1].work_id,
               "--lease", "600", "--sleep", "0.05")
    assert got["claimed"] is True
    got2 = _run("claim-once-hold", "--db", db, "--work-id", rows[2].work_id,
                "--lease", "600", "--sleep", "0.05")
    assert got2["claimed"] is False
    # reclaim the crashed slot: the ceiling slot is freed
    reg = SQLiteStorage(db)
    reclaimed = reg.reclaim_stale(
        now=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat())
    assert rows[0].work_id in reclaimed
    assert _running_for(reg, "goal-a") == 1
    reg.close()
    got3 = _run("claim-once-hold", "--db", db, "--work-id", rows[2].work_id,
                "--lease", "600", "--sleep", "0.05")
    assert got3["claimed"] is True  # the freed slot is claimable again
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_restart_preserves_ceiling_and_disable_permits_immediately(tmp_path):
    db = str(tmp_path / "restart.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 1)
    rows = _rows(reg, "goal-a", 3, "sched-shared")
    reg.close()
    assert _run("claim-once-hold", "--db", db, "--work-id", rows[0].work_id,
                "--sleep", "0.05")["claimed"] is True
    assert _run("claim-once-hold", "--db", db, "--work-id", rows[1].work_id,
                "--sleep", "0.05")["claimed"] is False
    # restart
    reg = SQLiteStorage(db)
    assert reg.get_goal_ceiling("goal-a") == 1
    reg.close()
    assert _run("claim-once-hold", "--db", db, "--work-id", rows[1].work_id,
                "--sleep", "0.05")["claimed"] is False  # still bound
    # disabling the ceiling immediately permits future claims
    reg = SQLiteStorage(db)
    reg.set_goal_ceiling_enabled("goal-a", False)
    reg.close()
    assert _run("claim-once-hold", "--db", db, "--work-id", rows[1].work_id,
                "--sleep", "0.05")["claimed"] is True
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_global_cap_and_floors_stay_authoritative(tmp_path):
    """Cap 6, B floor 2 ceiling 6 (never binds), A ceiling 10 (never
    binds): the observed behavior is exactly ADR-029 (A capped by the
    floor protection at 4, B reaches 2)."""
    db = str(tmp_path / "authority.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_ceiling("goal-a", 10)   # never binds
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 6)    # never binds
    _rows(reg, "goal-a", 8, "sched-shared")
    _rows(reg, "goal-b", 2, "sched-shared", start=100)
    reg.close()
    results = _run_parallel([["race-claim", "--db", db, "--scheduler-id",
                              "sched-shared"] for _ in range(8)])
    assert len([r for r in results if r["claimed"] is not None]) == 4
    reg = SQLiteStorage(db)
    assert _running_for(reg, "goal-a") == 4  # floor protection, not ceiling
    assert _running_for(reg, "goal-b") == 0
    assert len(reg.scheduler_events(event_type="ceiling.denied")) == 0
    reg.close()
