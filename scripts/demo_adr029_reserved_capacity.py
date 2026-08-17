#!/usr/bin/env python3
"""ADR-029 DoD demo: per-goal weighted capacity reservation.

A configured goal may reserve a bounded minimum share of the global
scheduler capacity (a FLOOR of concurrent RUNNING slots while the goal
has runnable work). Weight = relative opportunity; reservation =
minimum guarantee. Deterministic and offline: fixed timestamps, no
wall-clock races (except the subprocess lease-lapse in K, which uses an
explicit reclaim `now` instead of sleeping).

  A  default reservation = 0
  B  single reserved goal reaches its floor
  C  2-goal reservation protection (competitor denied)
  D  high-weight goal cannot consume reserved capacity
  E  multiple reservations
  F  reservation + DWRR interaction (remaining capacity stays weighted)
  G  idle reserved goal releases capacity
  H  reserved goal becomes runnable again (floor re-engages)
  I  cross-process reservation enforcement (real subprocess)
  J  reservation change while queued (no retroactive cancellation)
  K  restart/reclaim (config + floor survive; stale reclaim re-engages)
  L  reservation-denial telemetry (reservation.denied / satisfied)
  M  forged reservation attempt is powerless
  N  CLI/status/watch output
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
        task_id: str | None = None, t: int = 0):
    return reg.create(task_id=task_id or f"t-{goal_id}-{t}", goal_id=goal_id,
                      step_index=t, scheduler_id=scheduler_id,
                      now=_iso_plus(T0, t))


def _claim(reg, row, worker="w", now: str | None = None,
           scheduler_id: str | None = None) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now or T0, max_lease_seconds=600.0,
                    scheduler_id=scheduler_id or row.scheduler_id)
    return got is not None


def _fill(reg, goal_id: str) -> int:
    claimed = 0
    while True:
        row = next((r for r in reg.list_work(
            status=SchedulerWorkStatus.QUEUED) if r.goal_id == goal_id), None)
        if row is None:
            break
        if not _claim(reg, row):
            break
        claimed += 1
    return claimed


def _running(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def _complete(reg, row, worker="w", now: str | None = None) -> None:
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now or T0, owner_worker_id=worker)


def _kinds(reg, **filters) -> list[str]:
    return [e.kind for e in reg.scheduler_events(**filters)]


def main() -> int:
    global _checks
    print("ADR-029 demo: per-goal weighted capacity reservation\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr029-"))

    # ---------------------------------------------------------------- A -----
    print("A. default reservation = 0")
    reg = SQLiteStorage(tmp / "a.db")
    reg.set_scheduler_global_max(6)
    check(reg.get_goal_reservation("goal-a") == 0
          and reg.get_goal_reservation_config("goal-a") is None,
          "A: unconfigured goal defaults to reservation 0")
    a = _mk(reg, goal_id="goal-a", t=0)
    b = _mk(reg, goal_id="goal-b", t=1)
    check(_claim(reg, a) and _claim(reg, b),
          "A: no floor -> both goals claim freely")
    reg.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. single reserved goal reaches its floor")
    reg = SQLiteStorage(tmp / "b.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    check(_fill(reg, "goal-a") == 4 and _running(reg, "goal-b") == 0,
          "B: A is protected from B's floor (4 of 6)")
    check(_fill(reg, "goal-b") == 2 and _running(reg, "goal-b") == 2
          and _running(reg, "goal-a") == 4,
          "B: B reaches its floor; cap exactly 6 (4 + 2), never exceeded")
    reg.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. 2-goal reservation protection")
    reg = SQLiteStorage(tmp / "c.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(2):
        _mk(reg, goal_id="goal-b", t=100 + i)
    denied = _fill(reg, "goal-a")
    check(denied == 4 and _running(reg, "goal-a") == 4,
          "C: A's 5th claim was denied (slot protected for B)")
    check("reservation.denied" in _kinds(reg, goal_id="goal-a")
          and _fill(reg, "goal-b") == 2,
          "C: denial recorded as reservation.denied; B claims its floor")
    reg.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. high-weight goal cannot consume reserved capacity")
    reg = SQLiteStorage(tmp / "d.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)   # weight 8, reservation 0
    reg.set_goal_weight("goal-b", 1)   # weight 1, reservation 2
    reg.set_goal_reservation("goal-b", 2)
    for i in range(20):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(10):
        _mk(reg, goal_id="goal-b", t=100 + i)
    check(_fill(reg, "goal-a") == 6,
          "D: A (weight 8) is capped at cap - floor = 6")
    check(_fill(reg, "goal-b") == 2 and _running(reg, "goal-b") == 2
          and _running(reg, "goal-a") == 6,
          "D: B (weight 1) occupies its 2 reserved slots; A gets the rest")
    reg.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. multiple reservations")
    reg = SQLiteStorage(tmp / "e.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_reservation("goal-c", 1)
    for g in ("goal-a", "goal-b", "goal-c"):
        for i in range(10):
            _mk(reg, goal_id=g, t=i if g == "goal-a" else 100 + i)
    check(_fill(reg, "goal-a") == 5,
          "E: A (first claimer) reaches cap - floors = 5")
    check(_fill(reg, "goal-b") == 2 and _fill(reg, "goal-c") == 1
          and _running(reg, "goal-a") + _running(reg, "goal-b")
          + _running(reg, "goal-c") == 8,
          "E: B and C reach their floors; cap exactly 8 (5+2+1)")
    reg.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. reservation + DWRR interaction")
    reg = SQLiteStorage(tmp / "f.db")
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_weight("goal-c", 1)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(40):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(20):
        _mk(reg, goal_id="goal-b", t=100 + i)
    for i in range(40):
        _mk(reg, goal_id="goal-c", t=200 + i)
    claimed = {"goal-a": 0, "goal-b": 0, "goal-c": 0}
    for _ in range(3):
        for g in ("goal-a", "goal-b", "goal-c"):
            while True:
                row = next((r for r in reg.list_work(
                    status=SchedulerWorkStatus.QUEUED)
                    if r.goal_id == g), None)
                if row is None or not _claim(reg, row):
                    break
                claimed[g] += 1
        check(_running(reg, "goal-b") == 2,
              "F: B's floor held throughout the round")
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    check(claimed["goal-a"] == 5 * claimed["goal-c"]
          and claimed["goal-b"] == 6,
          f"F: remaining capacity split A:C exactly 5:1 "
          f"({claimed['goal-a']}:{claimed['goal-c']}); B: 2 per round")
    reg.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. idle reserved goal releases capacity")
    reg = SQLiteStorage(tmp / "g.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(10):
        _mk(reg, goal_id="goal-a", t=i)
    check(_fill(reg, "goal-a") == 6
          and reg.scheduler_status(now=T0)["reservation_pressure"] == 0,
          "G: idle reserved goal reserves nothing (A takes all 6; pressure 0)")
    reg.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. reserved goal becomes runnable again")
    reg = SQLiteStorage(tmp / "h.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(10):
        _mk(reg, goal_id="goal-a", t=i)
    _fill(reg, "goal-a")  # A holds all 6
    b_rows = [_mk(reg, goal_id="goal-b", t=100 + i) for i in range(2)]
    check(_fill(reg, "goal-b") == 0,
          "H: cap full: B waits (no eviction of RUNNING work)")
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-a"][:2]:
        _complete(reg, r)
    check(_fill(reg, "goal-b") == 2 and _running(reg, "goal-b") == 2
          and _running(reg, "goal-a") == 4,
          "H: A's freed slots go to B (floor re-engaged; A back to 4)")
    for r in b_rows:
        _complete(reg, r)
    check(_fill(reg, "goal-a") == 2 and _running(reg, "goal-a") == 6,
          "H: B idle again -> A reclaims the full cap")
    reg.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. cross-process reservation enforcement")
    db_i = tmp / "i.db"
    reg = SQLiteStorage(db_i)
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
    results = []
    for _ in range(8):
        p = subprocess.run(
            [sys.executable, WORKER, "race-claim", "--db", str(db_i),
             "--scheduler-id", "sched-shared"],
            capture_output=True, text=True, timeout=90, cwd=str(tmp))
        results.append(json.loads(p.stdout.strip().splitlines()[-1]))
    check(len([r for r in results if r["claimed"] is not None]) == 4,
          "I: subprocess hot claims capped at 4 (floor protected)")
    b_out = []
    for r in b_rows:
        p = subprocess.run(
            [sys.executable, WORKER, "claim-run", "--db", str(db_i),
             "--work-id", r.work_id, "--sleep", "0.1"],
            capture_output=True, text=True, timeout=90, cwd=str(tmp))
        b_out.append(json.loads(p.stdout.strip().splitlines()[-1]))
    check(all(o["status"] == "completed" for o in b_out),
          "I: B's floor claimed and completed by subprocesses")
    reg = SQLiteStorage(db_i)
    check(_running(reg, "goal-a") == 4,
          "I: durable RUNNING counts confirm the floor held")
    reg.close()

    # ---------------------------------------------------------------- J -----
    print("\nJ. reservation change while queued")
    reg = SQLiteStorage(tmp / "j.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(4):
        _mk(reg, goal_id="goal-b", t=100 + i)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")  # no reservation yet: A=6
    a_row = next(r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                 if r.goal_id == "goal-a")
    lease_before = reg.get_work(a_row.work_id).lease_expires_at
    reg.set_goal_reservation("goal-b", 2)  # added while everything queued
    check(reg.get_work(a_row.work_id).lease_expires_at == lease_before,
          "J: RUNNING work untouched by the config change")
    check("goal_reservation_changed" in _kinds(reg)
          and reg.get_goal_reservation("goal-b") == 2,
          "J: the change emitted goal_reservation_changed + persisted")
    reg.close()

    # ---------------------------------------------------------------- K -----
    print("\nK. restart/reclaim")
    db_k = tmp / "k.db"
    reg = SQLiteStorage(db_k)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    b = _mk(reg, goal_id="goal-b", t=100)
    assert reg.claim(b.work_id, "w-stale", 1.0, T0, 600.0,
                     scheduler_id="sched-1") is not None
    reg.close()
    reg2 = SQLiteStorage(db_k)  # restart
    check(reg2.get_goal_reservation("goal-b") == 2
          and reg2.get_goal_weight("goal-a") == 8
          and _running(reg2, "goal-b") == 1 and _fill(reg2, "goal-a") == 5,
          "K: config survives restart; floor enforced (B at 1 of 2 -> A capped)")
    reclaimed = reg2.reclaim_stale(now=_iso_plus(T0, 200))
    check(b.work_id in reclaimed,
          "K: stale lease reclaimed deterministically")
    fresh = reg2.create(task_id="t-b-fresh", goal_id="goal-b", step_index=9,
                        scheduler_id="sched-1", now=_iso_plus(T0, 201))
    check(reg2.claim(fresh.work_id, "w", 60.0, _iso_plus(T0, 202), 600.0,
                     scheduler_id="sched-1") is not None,
          "K: reclaimed goal re-enters and claims its floor slot")
    reg2.close()

    # ---------------------------------------------------------------- L -----
    print("\nL. reservation-denial telemetry")
    reg = SQLiteStorage(tmp / "l.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(2):
        _mk(reg, goal_id="goal-b", t=100 + i)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    denied = [e for e in reg.scheduler_events(
        event_type="reservation.denied")]
    check(len(denied) == 1 and denied[0].detail["reason"] == "reservation"
          and denied[0].detail["pressure"] == 2,
          "L: reservation.denied carries reason + pressure")
    sat = [e for e in reg.scheduler_events(
        event_type="reservation.satisfied")]
    check(len(sat) == 1 and sat[0].detail["reservation"] == 2
          and sat[0].detail["running"] == 2,
          "L: reservation.satisfied records the floor reached")
    st = reg.scheduler_status(now=T0)
    check(st["reserved_capacity"] == 2
          and st["reservation_satisfied"] == {"goal-b": True}
          and st["reservation_pressure"] == 0,
          "L: status exposes reserved_capacity / satisfied / pressure")
    reg.close()

    # ---------------------------------------------------------------- M -----
    print("\nM. forged reservation attempt is powerless")
    reg = SQLiteStorage(tmp / "m.db")
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    for i in range(8):
        _mk(reg, goal_id="goal-a", t=i)
    for i in range(2):
        _mk(reg, goal_id="goal-b", t=100 + i)
    reg.append_scheduler_event(AuditEvent(
        kind="goal_reservation_changed", ts=T0,
        detail={"goal_id": "goal-evil", "config": "goal_reservation",
                "reservation": 99, "outcome": "set"}))
    reg.append_scheduler_event(AuditEvent(
        kind="reservation.satisfied", ts=T0,
        detail={"goal_id": "goal-b", "work_id": "sw-fake",
                "reservation": 2, "running": 2, "satisfied": True}))
    check(reg.get_goal_reservation("goal-evil") == 0
          and len(reg.list_goal_reservations()) == 1
          and _fill(reg, "goal-a") == 4 and _fill(reg, "goal-b") == 2
          and _running(reg, "goal-b") == 2,
          "M: forged config + satisfied telemetry change nothing "
          "(floor counts durable rows, never events)")
    reg.close()

    # ---------------------------------------------------------------- N -----
    print("\nN. CLI/status/watch output")
    db_n = tmp / "n.db"
    reg = SQLiteStorage(db_n)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2, by="demo", now=T0)
    for i in range(2):
        _mk(reg, goal_id="goal-b", t=100 + i)
    _fill(reg, "goal-b")
    reg.close()
    repo = str(Path(__file__).resolve().parent.parent)
    p = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler",
         "reservations", "--json", "--db", str(db_n)],
        capture_output=True, text=True, timeout=60, cwd=repo)
    rows = json.loads(p.stdout)
    check(p.returncode == 0 and len(rows) == 1
          and rows[0]["reservation"] == 2 and rows[0]["enabled"] is True,
          "N: `scheduler reservations --json` lists the config")
    p = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler",
         "watch", "--type", "reservation.satisfied", "--db", str(db_n)],
        capture_output=True, text=True, timeout=60, cwd=repo)
    check(p.returncode == 0 and "reservation=2" in p.stdout
          and SQLiteStorage(db_n).scheduler_status()["reserved_capacity"] == 2,
          "N: `scheduler watch` renders reservation.satisfied; status ok")
    SQLiteStorage(db_n).close()

    print("\n" + "=" * 78)
    print(f"ADR-029 demo PASSED ({_checks} checks) - per-goal reserved capacity")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
