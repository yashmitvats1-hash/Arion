#!/usr/bin/env python3
"""ADR-031 DoD demo: per-goal concurrency ceilings.

A durable per-goal MAXIMUM concurrency ceiling, enforced transactionally
inside the authoritative claim path (between the reservation gate and
DWRR). Weight = relative opportunity; floor = minimum guarantee;
ceiling = maximum. Deterministic and offline: fixed timestamps, no
wall-clock races (explicit reclaim `now` where leases lapse).

  A  unconfigured goal is unbounded
  B  ceiling set/get/remove
  C  ceiling enforcement
  D  exact ceiling boundary
  E  multiple competing goals
  F  cross-process ceiling race
  G  high-weight goal hits ceiling
  H  lower-weight goal continues
  I  ceiling increase
  J  ceiling decrease without cancellation
  K  disable/remove
  L  restart persistence
  M  stale reclaim frees slot
  N  floor + ceiling valid
  O  floor > ceiling rejected
  P  ceiling denial telemetry
  Q  status projection
  R  planning simulation
  S  forged ceiling telemetry powerless
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.observability.events import AuditEvent  # noqa: E402
from arion.state.scheduler_work import SchedulerWorkStatus  # noqa: E402
from arion.state.store import SQLiteStorage  # noqa: E402

T0 = "2026-01-01T00:00:00+00:00"
WORKER = str(Path(__file__).resolve().parent / "_scheduler_multi_worker.py")

_checks = 0


def check(cond: bool, msg: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        raise SystemExit(f"  FAIL: {msg}")
    print(f"  ok: {msg}")


def _iso_plus(iso: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, goal_id: str = "goal-a", scheduler_id: str = "sched-1",
        t: int = 0):
    return reg.create(task_id=f"t-{goal_id}-{t}", goal_id=goal_id,
                      step_index=t, scheduler_id=scheduler_id,
                      now=_iso_plus(T0, t))


def _claim(reg, row, worker="w", now: str | None = None) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now or T0, max_lease_seconds=600.0,
                    scheduler_id=row.scheduler_id)
    return got is not None


def _running(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def _complete(reg, row, worker="w", now: str | None = None) -> None:
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now or T0, owner_worker_id=worker)


def _fill(reg, goal_id: str, limit: int | None = None) -> int:
    claimed = 0
    while True:
        row = next((r for r in reg.list_work(
            status=SchedulerWorkStatus.QUEUED) if r.goal_id == goal_id), None)
        if row is None:
            break
        if not _claim(reg, row):
            break
        claimed += 1
        if limit is not None and claimed >= limit:
            break
    return claimed


def main() -> int:
    global _checks
    print("ADR-031 demo: per-goal concurrency ceilings\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr031-"))

    # ---------------------------------------------------------------- A -----
    print("A. unconfigured goal is unbounded")
    reg = SQLiteStorage(tmp / "a.db")
    reg.set_scheduler_global_max(6)
    check(reg.get_goal_ceiling("goal-a") is None
          and reg.list_goal_ceilings() == [],
          "A: unconfigured goal is unbounded (no invented 0/huge)")
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    check(_fill(reg, "goal-a") == 4 and _running(reg, "goal-a") == 4,
          "A: claims bounded only by the global cap (6)")
    reg.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. ceiling set/get/remove")
    reg = SQLiteStorage(tmp / "b.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 3, by="demo", now=T0)
    cfg = reg.get_goal_ceiling_config("goal-a")
    check(cfg["ceiling"] == 3 and cfg["enabled"] is True
          and cfg["updated_by"] == "demo"
          and reg.remove_goal_ceiling("goal-a") is True
          and reg.get_goal_ceiling("goal-a") is None,
          "B: set/get with actor metadata; remove -> unbounded")
    reg.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. ceiling enforcement")
    reg = SQLiteStorage(tmp / "c.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    check(_claim(reg, rows[0]) and _claim(reg, rows[1])
          and not _claim(reg, rows[2]) and _running(reg, "goal-a") == 2,
          "C: two claims succeed; the third is denied at the ceiling")
    check(reg.get_work(rows[2].work_id).status ==
          SchedulerWorkStatus.QUEUED
          and reg.get_work(rows[2].work_id).worker_id is None,
          "C: the denied row stays QUEUED with no owner")
    reg.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. exact ceiling boundary")
    reg = SQLiteStorage(tmp / "d.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 3)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    check(_claim(reg, rows[0]) and _claim(reg, rows[1])
          and _claim(reg, rows[2])  # running was 2 < 3 -> allowed
          and not _claim(reg, rows[3]) and _running(reg, "goal-a") == 3,
          "D: the claim reaching exactly C is allowed; the next is denied")
    reg.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. multiple competing goals")
    reg = SQLiteStorage(tmp / "e.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_ceiling("goal-a", 3)
    reg.set_goal_ceiling("goal-b", 2)
    for i in range(6):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(6):
        _mk(reg, goal_id="goal-b", t=100 + i)
    a_filled = _fill(reg, "goal-a")
    b_filled = _fill(reg, "goal-b")
    check(a_filled == 3 and _running(reg, "goal-a") == 3
          and b_filled == 2 and _running(reg, "goal-b") == 2
          and _running(reg, "goal-a") + _running(reg, "goal-b") <= 8,
          "E: A capped at 3, B at 2; global cap authoritative")
    reg.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. cross-process ceiling race")
    db_f = tmp / "f.db"
    reg = SQLiteStorage(db_f)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [reg.create(task_id=f"t-a-{i}", goal_id="goal-a", step_index=i,
                       scheduler_id="sched-shared",
                       now=_iso_plus(T0, i)) for i in range(4)]
    reg.close()
    results = []
    for r in rows:
        p = subprocess.run(
            [sys.executable, WORKER, "claim-once-hold", "--db", str(db_f),
             "--work-id", r.work_id, "--sleep", "0.05"],
            capture_output=True, text=True, timeout=90)
        results.append(json.loads(p.stdout.strip().splitlines()[-1]))
    check(len([r for r in results if r["claimed"]]) == 2
          and SQLiteStorage(db_f).get_goal_ceiling("goal-a") == 2,
          "F: two subprocesses cannot collectively exceed the ceiling")
    reg = SQLiteStorage(db_f)
    check(_running(reg, "goal-a") == 2,
          "F: durable running count == ceiling exactly")
    reg.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. high-weight goal hits ceiling")
    reg = SQLiteStorage(tmp / "g.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-hot", 8)
    reg.set_goal_ceiling("goal-hot", 2)
    for i in range(6):
        _mk(reg, goal_id="goal-hot", t=i)
    check(_fill(reg, "goal-hot") == 2,
          "G: weight-8 goal stops at its ceiling 2")
    reg.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. lower-weight goal continues")
    reg = SQLiteStorage(tmp / "h.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-hot", 8)
    reg.set_goal_weight("goal-low", 1)
    reg.set_goal_ceiling("goal-hot", 2)
    for i in range(6):
        _mk(reg, goal_id="goal-hot", t=i)
    for i in range(6):
        _mk(reg, goal_id="goal-low", t=100 + i)
    _fill(reg, "goal-hot")
    low = _fill(reg, "goal-low")
    check(_running(reg, "goal-hot") == 2
          and low >= 1 and _running(reg, "goal-low") >= 1
          and reg.get_goal_ceiling("goal-hot") == 2,
          "H: hot at ceiling; low-weight goal keeps progressing")
    reg.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. ceiling increase")
    reg = SQLiteStorage(tmp / "i.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(5)]
    _fill(reg, "goal-a")
    check(_running(reg, "goal-a") == 2 and not _claim(reg, rows[2]),
          "I: bound at ceiling 2")
    reg.set_goal_ceiling("goal-a", 5)  # increase while running
    check(_claim(reg, rows[2]) and _running(reg, "goal-a") == 3,
          "I: increase -> new claims use the additional capacity")
    reg.close()

    # ---------------------------------------------------------------- J -----
    print("\nJ. ceiling decrease without cancellation")
    reg = SQLiteStorage(tmp / "j.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 5)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(6)]
    _fill(reg, "goal-a", limit=5)
    assert _running(reg, "goal-a") == 5
    running_before = [(r.work_id, r.worker_id, r.lease_expires_at)
                      for r in reg.list_work(
                          status=SchedulerWorkStatus.RUNNING)]
    reg.set_goal_ceiling("goal-a", 2)  # decrease while running == 5
    running_after = [(r.work_id, r.worker_id, r.lease_expires_at)
                     for r in reg.list_work(
                         status=SchedulerWorkStatus.RUNNING)]
    check(not _claim(reg, rows[5]) and running_after == running_before,
          "J: decrease denies new claims; RUNNING work not cancelled")
    reg.close()

    # ---------------------------------------------------------------- K -----
    print("\nK. disable/remove")
    reg = SQLiteStorage(tmp / "k.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 1)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(3)]
    _claim(reg, rows[0])
    check(not _claim(reg, rows[1]), "K: bound at ceiling 1")
    reg.set_goal_ceiling_enabled("goal-a", False)
    check(_claim(reg, rows[1]),
          "K: disabling immediately permits claims")
    reg.set_goal_ceiling_enabled("goal-a", True)
    reg.remove_goal_ceiling("goal-a")
    check(_claim(reg, rows[2]) and _running(reg, "goal-a") == 3,
          "K: removal returns the goal to unbounded")
    reg.close()

    # ---------------------------------------------------------------- L -----
    print("\nL. restart persistence")
    db_l = tmp / "l.db"
    reg = SQLiteStorage(db_l)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2, by="demo", now=T0)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    _fill(reg, "goal-a")
    reg.close()
    reg2 = SQLiteStorage(db_l)  # restart
    check(reg2.get_goal_ceiling("goal-a") == 2
          and not reg2.claim(rows[2].work_id, "w", 60.0, T0, 600.0,
                             scheduler_id="sched-1")
          and _running(reg2, "goal-a") == 2,
          "L: ceiling survives restart and stays enforced")
    reg2.close()

    # ---------------------------------------------------------------- M -----
    print("\nM. stale reclaim frees slot")
    reg = SQLiteStorage(tmp / "m.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(3)]
    assert reg.claim(rows[0].work_id, "w-stale", 1.0, T0, 600.0,
                     scheduler_id="sched-1") is not None
    assert _claim(reg, rows[1])
    check(not _claim(reg, rows[2]), "M: ceiling full (2)")
    # rows[0]'s 1s lease lapsed; rows[1]'s 60s lease is still valid
    reclaimed = reg.reclaim_stale(now=_iso_plus(T0, 30))
    check(rows[0].work_id in reclaimed and _running(reg, "goal-a") == 1
          and _claim(reg, rows[2]) and _running(reg, "goal-a") == 2,
          "M: stale reclaim frees a slot; it is claimable again")
    reg.close()

    # ---------------------------------------------------------------- N -----
    print("\nN. floor + ceiling valid")
    reg = SQLiteStorage(tmp / "n.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_ceiling("goal-a", 5)
    check(reg.get_goal_reservation("goal-a") == 2
          and reg.get_goal_ceiling("goal-a") == 5,
          "N: floor 2 ceiling 5 accepted (2 <= 5)")
    reg.set_goal_reservation("goal-b", 5)
    reg.set_goal_ceiling("goal-b", 5)
    check(reg.get_goal_reservation("goal-b") == 5
          and reg.get_goal_ceiling("goal-b") == 5,
          "N: floor 5 ceiling 5 accepted (equal)")
    reg.close()

    # ---------------------------------------------------------------- O -----
    print("\nO. floor > ceiling rejected")
    reg = SQLiteStorage(tmp / "o.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 4)
    try:
        reg.set_goal_ceiling("goal-a", 3)  # R=4 > C=3
    except Exception as exc:
        check("floor<=ceiling" in str(exc),
              "O: ceiling below floor fails closed")
    else:
        check(False, "O: ceiling below floor was accepted")
    check(reg.get_goal_ceiling("goal-a") is None
          and reg.get_goal_reservation("goal-a") == 4,
          "O: no partial policy update (floor untouched, no ceiling)")
    reg.close()

    # ---------------------------------------------------------------- P -----
    print("\nP. ceiling denial telemetry")
    reg = SQLiteStorage(tmp / "p.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(3)]
    filled = _fill(reg, "goal-a")  # claims 2, the 3rd is ceiling-denied
    assert filled == 2
    denied = [e for e in reg.scheduler_events(
        event_type="ceiling.denied")]
    check(len(denied) == 1 and denied[0].detail["reason"] == "goal_ceiling"
          and denied[0].detail["ceiling"] == 2
          and denied[0].detail["running"] == 2,
          "P: ceiling.denied carries ceiling + running + reason")
    changed = [e for e in reg.scheduler_events(
        event_type="goal_ceiling_changed")]
    check(len(changed) == 1 and changed[0].detail["outcome"] == "set",
          "P: goal_ceiling_changed emitted atomically with the write")
    reg.close()

    # ---------------------------------------------------------------- Q -----
    print("\nQ. status projection")
    reg = SQLiteStorage(tmp / "q.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(3)]
    _fill(reg, "goal-a")
    snap = reg.capacity_snapshot(now=T0)
    a = [g for g in snap["goals"] if g["goal_id"] == "goal-a"][0]
    check(a["ceiling"] == 2 and a["ceiling_headroom"] == 0
          and a["state"] == "goal_ceiling_limited"
          and a["eligible"] is False,
          "Q: per-goal ceiling + headroom + goal_ceiling_limited state")
    check(snap["goals_at_ceiling"] == ["goal-a"]
          and snap["recent_ceiling_denials"] == 1
          and snap["ceiling_limited_goal_count"] == 1
          and "authoritative at claim time"
          in reg.explain_goal_eligibility("goal-a")["note"],
          "Q: aggregates + claim-time disclaimer")
    reg.close()

    # ---------------------------------------------------------------- R -----
    print("\nR. planning simulation")
    reg = SQLiteStorage(tmp / "r.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 4)
    sim = reg.simulate_ceiling_change("goal-b", 6)
    check(sim["current_ceiling"] == 4 and sim["proposed_ceiling"] == 6
          and sim["floor"] == 2 and sim["floor_ceiling_valid"] is True
          and reg.simulate_ceiling_change("goal-b", 1)[
              "floor_ceiling_valid"] is False,
          "R: ceiling dry-run reports validity incl. floor violations")
    sim3 = reg.simulate_goal_policy("goal-b", reservation=2, ceiling=3,
                                    weight=4)
    check(sim3["proposed_ceiling"] == 3 and sim3["proposed_weight"] == 4
          and sim3["floor_ceiling_valid"] is True
          and sim3["feasible"] is True
          and reg.get_goal_ceiling("goal-b") == 4
          and reg.get_goal_reservation("goal-b") == 2,
          "R: simulate_goal_policy covers floor+ceiling+weight; no persist")
    reg.close()

    # ---------------------------------------------------------------- S -----
    print("\nS. forged ceiling telemetry powerless")
    reg = SQLiteStorage(tmp / "s.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2, now=T0)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    for i in range(5):
        reg.append_scheduler_event(AuditEvent(
            kind="goal_ceiling_changed", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-a", "config": "goal_ceiling",
                    "ceiling": 99, "outcome": "set"}))
        reg.append_scheduler_event(AuditEvent(
            kind="ceiling.denied", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-evil", "work_id": f"sw-fake-{i}",
                    "running": 99, "ceiling": 99, "reason": "goal_ceiling"}))
    check(reg.get_goal_ceiling("goal-a") == 2
          and reg.get_goal_ceiling("goal-evil") is None,
          "S: forged ceiling events create/change nothing")
    _fill(reg, "goal-a")
    check(_running(reg, "goal-a") == 2 and not _claim(reg, rows[2]),
          "S: the real ceiling still binds (authority rows only)")
    reg.close()

    print("\n" + "=" * 78)
    print(f"ADR-031 demo PASSED ({_checks} checks) - per-goal ceilings")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
