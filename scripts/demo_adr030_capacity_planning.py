#!/usr/bin/env python3
"""ADR-030 DoD demo: reservation-aware capacity planning & scheduler status.

A READ-ONLY projection over durable scheduler state: exact capacity
arithmetic, configured vs active reservations, feasibility, proposed-
policy simulation, admission explanations, upgraded status CLI,
`reservations --check` and `reservation plan` dry-runs. Deterministic
and offline: fixed timestamps, no wall-clock races.

  A  empty scheduler
  B  global capacity snapshot
  C  running capacity
  D  configured reservations
  E  active reservation pressure
  F  idle reserved goal
  G  below-floor goal
  H  satisfied goal
  I  feasible configuration
  J  infeasible configuration
  K  proposed reservation increase
  L  proposed reservation decrease
  M  status JSON
  N  reservation check JSON
  O  dry-run proves no mutation
  P  forged telemetry is powerless
  Q  cross-process observation
  R  restart persistence
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


def main() -> int:
    global _checks
    print("ADR-030 demo: capacity planning & scheduler status\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr030-"))

    # ---------------------------------------------------------------- A -----
    print("A. empty scheduler")
    reg = SQLiteStorage(tmp / "a.db")
    reg.set_scheduler_global_max(8)
    snap = reg.capacity_snapshot(now=T0)
    check(snap["running_count"] == 0 and snap["queued_count"] == 0
          and snap["available_capacity"] == 8
          and snap["reserved_capacity"] == 0
          and snap["active_reserved_capacity"] == 0
          and snap["reservation_pressure"] == 0
          and snap["unreserved_capacity"] == 8,
          "A: empty scheduler: 8 available, no reservations, unreserved 8")
    reg.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. global capacity snapshot")
    reg = SQLiteStorage(tmp / "b.db")
    reg.set_scheduler_global_max(6)
    snap = reg.capacity_snapshot(now=T0)
    check(snap["global_max_concurrency"] == 6
          and snap["available_capacity"] == 6
          and snap["active_scheduler_count"] == 0
          and snap["active_goal_count"] == 0,
          "B: cap 6 -> available 6; no active schedulers/goals")
    reg2 = SQLiteStorage(tmp / "b2.db")  # no cap
    snap = reg2.capacity_snapshot(now=T0)
    check(snap["global_max_concurrency"] is None
          and snap["available_capacity"] is None
          and snap["unreserved_capacity"] is None,
          "B: no cap -> explicit unbounded (None) sentinels")
    reg2.close()
    reg.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. running capacity")
    reg = SQLiteStorage(tmp / "c.db")
    reg.set_scheduler_global_max(8)
    rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(6)]
    for r in rows[:3]:
        _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    check(snap["running_count"] == 3 and snap["queued_count"] == 3,
          "C: 3 running / 3 queued")
    check(snap["available_capacity"] == 5,  # 8 - 3
          "C: available = max(cap - running, 0) = 5")
    reg.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. configured reservations")
    reg = SQLiteStorage(tmp / "d.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 3)
    snap = reg.capacity_snapshot(now=T0)
    check(snap["reserved_capacity"] == 5
          and snap["reserved_goal_count"] == 2,
          "D: configured reserved total = 5 (2 + 3)")
    check(snap["active_reserved_capacity"] == 0
          and snap["reservation_pressure"] == 0,
          "D: nothing active yet (no runnable work)")
    reg.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. active reservation pressure")
    reg = SQLiteStorage(tmp / "e.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    snap = reg.capacity_snapshot(now=T0)
    check(snap["active_reserved_capacity"] == 2
          and snap["reservation_pressure"] == 2,
          "E: B runnable below floor -> active 2, pressure 2")
    check(snap["goals_below_reservation"] == ["goal-b"],
          "E: B classified below its reservation")
    reg.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. idle reserved goal")
    reg = SQLiteStorage(tmp / "f.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    _mk(reg, goal_id="goal-a", t=0)  # only A has work
    snap = reg.capacity_snapshot(now=T0)
    check(snap["reserved_capacity"] == 2
          and snap["active_reserved_capacity"] == 0,
          "F: idle B's reservation is configured but consumes nothing")
    check(snap["unreserved_capacity"] == 8,
          "F: unreserved capacity ignores the idle floor")
    check("goal-b" in snap["goals_below_reservation"]
          and snap["reservation_pressure"] == 0,
          "F: idle B reads below by count but exerts zero pressure")
    reg.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. below-floor goal")
    reg = SQLiteStorage(tmp / "g.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(6):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    for r in [_mk(reg, goal_id="goal-b", t=200 + i) for i in range(1)]:
        _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    b = [g for g in snap["goals"] if g["goal_id"] == "goal-b"][0]
    check(b["running"] == 1 and b["reservation_deficit"] == 1
          and b["reservation_satisfied"] is False,
          "G: B below floor: deficit 1, not satisfied")
    check(b["state"] == "reserved_floor" and b["eligible"] is True,
          "G: B is reserved_floor (floor path would admit at claim time)")
    reg.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. satisfied goal")
    reg = SQLiteStorage(tmp / "h.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    b_rows = [_mk(reg, goal_id="goal-b", t=100 + i) for i in range(2)]
    for r in b_rows:
        _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    b = [g for g in snap["goals"] if g["goal_id"] == "goal-b"][0]
    check(b["reservation_satisfied"] is True and b["reservation_deficit"] == 0
          and b["reservation_pressure"] == 0,
          "H: B at its floor: satisfied, no pressure")
    check(snap["goals_at_reservation"] == ["goal-b"],
          "H: B classified at its reservation")
    reg.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. feasible configuration")
    reg = SQLiteStorage(tmp / "i.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 3)
    f = reg.reservation_feasibility(proposed={"goal-a": 2, "goal-b": 3,
                                              "goal-c": 2})
    check(f["feasible"] is True and f["proposed_total"] == 7
          and f["overflow"] == 0 and f["reason"] == "ok",
          "I: A2+B3+C2 = 7 <= 8 -> feasible")
    reg.close()

    # ---------------------------------------------------------------- J -----
    print("\nJ. infeasible configuration")
    reg = SQLiteStorage(tmp / "j.db")
    reg.set_scheduler_global_max(8)
    f = reg.reservation_feasibility(proposed={"goal-a": 2, "goal-b": 3,
                                              "goal-c": 2, "goal-d": 2})
    check(f["feasible"] is False and f["proposed_total"] == 9
          and f["overflow"] == 1 and f["reason"] == "oversubscribed",
          "J: adding D=2 -> total 9 -> infeasible, overflow 1")
    check(f["affected_goals"] == ["goal-a", "goal-b", "goal-c", "goal-d"],
          "J: affected goals reported deterministically")
    reg.close()

    # ---------------------------------------------------------------- K -----
    print("\nK. proposed reservation increase")
    reg = SQLiteStorage(tmp / "k.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    sim = reg.simulate_reservation_change("goal-b", 4)
    check(sim["current_reservation"] == 2
          and sim["proposed_reservation"] == 4,
          "K: 2 -> 4 proposed")
    check(sim["current_total"] == 2 and sim["proposed_total"] == 4
          and sim["feasible"] is True and sim["overflow"] == 0
          and sim["pressure_delta"] == "increase"
          and sim["reservation_pressure_now"] == 2
          and sim["reservation_pressure_proposed"] == 4,
          "K: total 2 -> 4, feasible, remaining 4, pressure 2 -> 4")
    reg.close()

    # ---------------------------------------------------------------- L -----
    print("\nL. proposed reservation decrease")
    reg = SQLiteStorage(tmp / "l.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 4)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    sim = reg.simulate_reservation_change("goal-b", 1)
    check(sim["proposed_total"] == 1 and sim["feasible"] is True
          and sim["pressure_delta"] == "decrease"
          and sim["reservation_pressure_proposed"] == 1,
          "L: 4 -> 1 lowers total to 1 and pressure to 1 (decrease)")
    sim0 = reg.simulate_reservation_change("goal-b", 0)
    check(sim0["proposed_total"] == 0 and sim0["feasible"] is True,
          "L: setting 0 removes the floor entirely")
    reg.close()

    # ---------------------------------------------------------------- M -----
    print("\nM. status JSON")
    db_m = tmp / "m.db"
    reg = SQLiteStorage(db_m)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = [_mk(reg, goal_id="goal-a", t=i) for i in range(4)]
    for i in range(3):
        _mk(reg, goal_id="goal-b", t=100 + i)
    for r in a_rows[:3]:
        _claim(reg, r)
    reg.close()
    p = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler",
         "status", "--json", "--db", str(db_m)],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parent.parent))
    out = json.loads(p.stdout)
    check(p.returncode == 0 and out["global_max_concurrency"] == 8
          and out["available_capacity"] == 5 and out["running"] == 3,
          "M: status JSON carries the capacity block")
    goals = {g["goal_id"]: g for g in out["goals"]}
    check(goals["goal-b"]["state"] == "reserved_floor"
          and goals["goal-b"]["reservation"] == 2,
          "M: status JSON carries per-goal projections")
    check("task_id" not in json.dumps(out) and "rowid" not in json.dumps(out),
          "M: JSON is bounded (no payloads / internals)")

    # ---------------------------------------------------------------- N -----
    print("\nN. reservation check JSON")
    p = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler",
         "reservations", "--check", "--json", "--db", str(db_m)],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parent.parent))
    data = json.loads(p.stdout)
    check(p.returncode == 0 and data["feasible"] is True
          and data["configured_total"] == 2
          and data["active_reservation"] == 2
          and data["goals_below"] == ["goal-b"]
          and "idle_reserved_goals" in data,
          "N: --check JSON: feasible, configured 2, active 2, below/goals")

    # ---------------------------------------------------------------- O -----
    print("\nO. dry-run proves no mutation")
    reg = SQLiteStorage(db_m)
    before = {
        "res": reg.list_goal_reservations(),
        "w": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id)
                 for r in reg.list_work()],
    }
    for _ in range(3):
        reg.capacity_snapshot(now=T0)
        reg.simulate_reservation_change("goal-b", 6)
        reg.reservation_feasibility(proposed={"goal-a": 8})
        reg.explain_goal_eligibility("goal-b")
        reg.reservation_check()
    after = {
        "res": reg.list_goal_reservations(),
        "w": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id)
                 for r in reg.list_work()],
    }
    check(after == before,
          "O: planning APIs leave reservations/weights/credit/events/"
          "ownership byte-identical")
    reg.close()

    # ---------------------------------------------------------------- P -----
    print("\nP. forged telemetry is powerless")
    reg = SQLiteStorage(tmp / "p.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2, now=T0)
    b_rows = [_mk(reg, goal_id="goal-b", t=100 + i) for i in range(2)]
    for r in b_rows:
        _claim(reg, r)
    for i in range(5):
        reg.append_scheduler_event(AuditEvent(
            kind="reservation.satisfied", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-b", "work_id": f"sw-fake-{i}",
                    "reservation": 2, "running": 2, "satisfied": True}))
        reg.append_scheduler_event(AuditEvent(
            kind="goal_weight.refill", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-b", "work_id": f"sw-fake-{i}",
                    "weight": 9999, "credit_before": 9999,
                    "credit_after": 9999, "refill": True}))
    snap = reg.capacity_snapshot(now=T0)
    check(snap["running_count"] == 2
          and snap["reservation_pressure"] == 0,
          "P: forged satisfied/refill telemetry changes no planning input")
    check(reg.list_goal_reservations() == [{
        "goal_id": "goal-b", "reservation": 2, "enabled": True,
        "updated_at": T0, "updated_by": "operator"}],
          "P: durable reservations untouched by forged events")
    reg.close()

    # ---------------------------------------------------------------- Q -----
    print("\nQ. cross-process observation")
    db_q = tmp / "q.db"
    reg = SQLiteStorage(db_q)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        reg.create(task_id=f"t-a-{i}", goal_id="goal-a", step_index=i,
                   scheduler_id="sched-shared", now=_iso_plus(T0, i))
    b_rows = [reg.create(task_id=f"t-b-{i}", goal_id="goal-b", step_index=i,
                         scheduler_id="sched-shared",
                         now=_iso_plus(T0, 100 + i)) for i in range(2)]
    reg.close()
    procs = [subprocess.Popen(
        [sys.executable, WORKER, "race-claim", "--db", str(db_q),
         "--scheduler-id", "sched-shared"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(8)]
    # observe WHILE the workers race
    snap_observed = SQLiteStorage(db_q).capacity_snapshot(now=T0)
    for p_ in procs:
        out, _ = p_.communicate(timeout=90)
        assert p_.returncode == 0
    check(snap_observed["running_count"] <= 6
          and "running_count" in snap_observed,
          "Q: snapshot during worker races stays bounded")
    snap = SQLiteStorage(db_q).capacity_snapshot(now=T0)
    check(snap["running_count"] == 4
          and snap["reservation_pressure"] == 2,
          "Q: after the race: A capped at 4, B floor intact")
    for r in b_rows:
        p_ = subprocess.run([sys.executable, WORKER, "claim-run",
                             "--db", str(db_q), "--work-id", r.work_id,
                             "--sleep", "0.1"],
                            capture_output=True, text=True, timeout=90)
        assert p_.returncode == 0
    reg = SQLiteStorage(db_q)
    before = reg.list_goal_reservations()
    reg.capacity_snapshot(now=T0)
    check(reg.list_goal_reservations() == before,
          "Q: observation never mutated the shared registry")
    reg.close()

    # ---------------------------------------------------------------- R -----
    print("\nR. restart persistence")
    db_r = tmp / "r.db"
    reg = SQLiteStorage(db_r)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_reservation("goal-b", 2, by="demo", now=T0)
    b_rows = [_mk(reg, goal_id="goal-b", t=100 + i) for i in range(2)]
    for r in b_rows:
        _claim(reg, r)
    reg.close()
    reg2 = SQLiteStorage(db_r)  # restart
    snap = reg2.capacity_snapshot(now=T0)
    # B's queue drained (both rows claimed): its floor is satisfied and
    # no longer actively protects anything - matching the claim gate's
    # "idle goals reserve nothing" rule exactly
    check(snap["global_max_concurrency"] == 8
          and snap["reserved_capacity"] == 2
          and snap["active_reserved_capacity"] == 0
          and snap["running_count"] == 2,
          "R: capacity/planning view fully reconstructed after restart")
    check(snap["goals_at_reservation"] == ["goal-b"]
          and reg2.reservation_feasibility()["feasible"] is True,
          "R: feasibility + classification identical after restart")
    reg2.close()

    print("\n" + "=" * 78)
    print(f"ADR-030 demo PASSED ({_checks} checks) - capacity planning")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
