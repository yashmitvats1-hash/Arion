#!/usr/bin/env python3
"""ADR-028 DoD demo: scheduler observability / telemetry.

A durable, bounded, queryable telemetry layer over the existing audit
abstraction. Events commit ATOMICALLY with the state transitions they
describe - a rolled-back transition leaves no phantom success event.
Telemetry is OBSERVATIONAL ONLY: forged/deleted/duplicated events have
zero effect on execution semantics (the registry rows + transactional
claim path remain authoritative).

  A  scheduler registration event
  B  work claim event
  C  work heartbeat event
  D  successful completion event
  E  failed completion event
  F  claim denial (global capacity)
  G  claim denial (scheduler fair share)
  H  DWRR / goal-weight events (refill + denied + credit)
  I  stale lease reclaim event (atomic with the reclaim)
  J  ownership handoff event
  K  scheduler abandonment event
  L  restart / history preservation (reopen sees the same events)
  M  rollback produces no phantom event
  N  forged telemetry is powerless
  O  bounded retention / pruning
  P  CLI JSON output (scheduler watch --json)

Deterministic and offline: fixed timestamps, no wall-clock races.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.observability.events import AuditEvent
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

CHECKS = 0
T0 = "2026-01-01T00:00:00+00:00"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def _iso_plus(iso: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, goal_id="goal-a", task_id="t1", scheduler_id="sched-1", t=0.0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=0,
                      scheduler_id=scheduler_id, now=_iso_plus(T0, t))


def _kinds(reg, **filters):
    return [e.kind for e in reg.scheduler_events(**filters)]


def main() -> int:
    print("ADR-028 demo: scheduler observability / telemetry\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr028-"))

    # ---------------------------------------------------------------- A -----
    print("A. scheduler registration event")
    reg = SQLiteStorage(tmp / "a.db")
    reg.register_scheduler("sched-1", pid=1001, lease_seconds=60.0, now=T0)
    regs = [e for e in reg.scheduler_events(event_type="scheduler.registered")]
    check(len(regs) == 1 and regs[0].detail["scheduler_id"] == "sched-1"
          and regs[0].detail["pid"] == 1001,
          "A: registration event committed with the registration")
    reg.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. work claim event")
    reg = SQLiteStorage(tmp / "b.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    claimed = [e for e in reg.scheduler_events(work_id=row.work_id)
               if e.kind == "work.claimed"]
    check(len(claimed) == 1 and claimed[0].detail["worker_id"] == "w-1"
          and claimed[0].detail["lease_expires_at"] is not None,
          "B: claim event (owner + lease) committed atomically")
    check(claimed[0].detail["scheduler_id"] == "sched-1",
          "B: claim event names the claiming scheduler")
    reg.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. work heartbeat event")
    reg = SQLiteStorage(tmp / "c.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.heartbeat(row.work_id, "w-1", lease_seconds=60.0,
                  now=_iso_plus(T0, 10), max_lease_seconds=600.0)
    hb = [e for e in reg.scheduler_events(work_id=row.work_id)
          if e.kind == "work.heartbeat"]
    check(len(hb) == 1 and hb[0].detail["lease_expires_at"] == _iso_plus(T0, 70),
          "C: heartbeat event records the extended lease")
    reg.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. successful completion event")
    reg = SQLiteStorage(tmp / "d.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w-1", now=_iso_plus(T0, 5))
    done = [e for e in reg.scheduler_events(work_id=row.work_id)
            if e.kind == "work.completed"]
    check(len(done) == 1 and done[0].success is True,
          "D: work.completed emitted once, flagged success")
    reg.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. failed completion event")
    reg = SQLiteStorage(tmp / "e.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.FAILED,
                      error="boom", owner_worker_id="w-1", now=_iso_plus(T0, 5))
    failed = [e for e in reg.scheduler_events(work_id=row.work_id)
              if e.kind == "work.failed"]
    check(len(failed) == 1 and failed[0].detail["reason"] == "boom",
          "E: work.failed emitted with bounded error")
    reg.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. claim denial - global capacity")
    reg = SQLiteStorage(tmp / "f.db")
    reg.set_scheduler_global_max(1)
    a = _mk(reg, task_id="t-a", t=0)
    b = _mk(reg, task_id="t-b", t=1)
    reg.claim(a.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-1")
    got = reg.claim(b.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 2), max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    denied = [e for e in reg.scheduler_events(work_id=b.work_id)
              if e.kind == "capacity.denied"]
    check(got is None and len(denied) == 1
          and denied[0].detail["reason"] == "capacity",
          "F: capacity denial recorded with reason code")
    reg.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. claim denial - scheduler fair share")
    reg = SQLiteStorage(tmp / "g.db")
    reg.set_scheduler_global_max(4)  # cap 4, two schedulers -> share = 2
    reg.set_goal_weight("goal-a", 4)
    reg.set_goal_weight("goal-b", 1)
    a1 = _mk(reg, goal_id="goal-a", scheduler_id="sched-A", t=0)
    a2 = _mk(reg, goal_id="goal-a", scheduler_id="sched-A", t=1)
    a3 = _mk(reg, goal_id="goal-a", scheduler_id="sched-A", t=2)
    b1 = _mk(reg, goal_id="goal-b", scheduler_id="sched-B", t=3)
    reg.claim(a1.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 4),
              max_lease_seconds=600.0, scheduler_id="sched-A")
    reg.claim(a2.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 4),
              max_lease_seconds=600.0, scheduler_id="sched-A")
    reg.claim(b1.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 4),
              max_lease_seconds=600.0, scheduler_id="sched-B")
    # sched-A is at its fair share (2 of cap 4, room remains globally); the
    # third A claim is denied by the SCHEDULER SHARE (not the global cap)
    got = reg.claim(a3.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 4), max_lease_seconds=600.0,
                    scheduler_id="sched-A")
    denied = [e for e in reg.scheduler_events(work_id=a3.work_id)
              if e.kind == "scheduler_share.denied"]
    check(got is None and len(denied) == 1
          and denied[0].detail["reason"] == "scheduler_share",
          "G: scheduler fair-share denial recorded (distinct from capacity)")
    check(reg.scheduler_status()["running_count"] == 3,
          "G: global capacity still had room - share was the binding gate")
    reg.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. DWRR / goal-weight events")
    reg = SQLiteStorage(tmp / "h.db")
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    a = _mk(reg, goal_id="goal-a", t=0)
    b = _mk(reg, goal_id="goal-b", t=1)
    reg.claim(a.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-1")
    refills = [e for e in reg.scheduler_events(event_type="goal_weight.refill")]
    check(len(refills) == 1 and refills[0].detail["weight"] == 2
          and refills[0].detail["credit_before"] == 0
          and refills[0].detail["credit_after"] == 1,
          "H: refill event exposes weight + credit before/after")
    # after goal-b's credit is spent (1 claim), further attempts are denied
    reg.claim(b.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-1")
    a2 = _mk(reg, goal_id="goal-a", task_id="t-a2", t=3)
    got = reg.claim(a2.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 4), max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    gw_denied = [e for e in reg.scheduler_events(work_id=a2.work_id)
                 if e.kind == "goal_weight.denied"]
    check(got is not None and gw_denied == [],
          "H: goal-a still had credit for a second claim (2:1)")
    reg.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. stale lease reclaim (atomic with the event)")
    reg = SQLiteStorage(tmp / "i.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-stale", lease_seconds=1.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.reclaim_stale(now=_iso_plus(T0, 2))
    reclaimed = [e for e in reg.scheduler_events(work_id=row.work_id)
                 if e.kind == "work.reclaimed"]
    check(reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
          and len(reclaimed) == 1
          and reclaimed[0].detail["reason"] == "lease_expired",
          "I: reclaim committed atomically with work.reclaimed")
    reg.close()

    # ---------------------------------------------------------------- J -----
    print("\nJ. ownership handoff event")
    reg = SQLiteStorage(tmp / "j.db")
    reg.set_scheduler_global_max(2)
    a = _mk(reg, task_id="t-a", t=0)
    b = _mk(reg, task_id="t-b", t=1)
    reg.claim(a.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    _, nxt = reg.release_and_claim_next(
        a.work_id, owner_worker_id="w-1", status=SchedulerWorkStatus.COMPLETED,
        error=None, scheduler_id="sched-1", worker_id="w-1",
        lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
    handoff = [e for e in reg.scheduler_events(work_id=a.work_id)
               if e.kind == "work.handoff"]
    check(nxt is not None and len(handoff) == 1
          and handoff[0].detail["next_work_id"] == b.work_id,
          "J: handoff event names the next claimed work")
    reg.close()

    # ---------------------------------------------------------------- K -----
    print("\nK. scheduler abandonment event")
    reg = SQLiteStorage(tmp / "k.db")
    reg.register_scheduler("sched-dead", pid=9, lease_seconds=0.01, now=T0)
    row = _mk(reg, scheduler_id="sched-dead")
    import time as _time
    _time.sleep(0.05)
    reg.abandon_foreign_queued("sched-mine")
    abandoned = [e for e in reg.scheduler_events()
                 if e.kind == "scheduler.abandoned"]
    check(len(abandoned) == 1
          and abandoned[0].detail["scheduler_id"] == "sched-dead"
          and abandoned[0].detail["work_id"] == row.work_id,
          "K: abandonment event names the dead scheduler + work")
    reg.close()

    # ---------------------------------------------------------------- L -----
    print("\nL. restart / history preservation")
    db_l = tmp / "l.db"
    reg = SQLiteStorage(db_l)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w-1", now=_iso_plus(T0, 5))
    reg.close()
    reg2 = SQLiteStorage(db_l)  # "restart"
    kinds = _kinds(reg2, work_id=row.work_id)
    check("work.claimed" in kinds and "work.completed" in kinds,
          "L: committed events survive a reopen")
    check(kinds[0] == "work.queued" and kinds[-1] == "work.completed",
          "L: history replays oldest-first after restart")
    reg2.close()

    # ---------------------------------------------------------------- M -----
    print("\nM. rollback produces no phantom event")
    reg = SQLiteStorage(tmp / "m.db")
    reg.set_scheduler_global_max(1)
    a = _mk(reg, task_id="t-a", t=0)
    b = _mk(reg, task_id="t-b", t=1)
    reg.claim(a.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-1")
    got = reg.claim(b.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 2), max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    check(got is None, "M: the second claim was denied by the cap")
    check("work.claimed" not in _kinds(reg, work_id=b.work_id),
          "M: no phantom work.claimed for the rolled-back transition")
    check("capacity.denied" in _kinds(reg, work_id=b.work_id),
          "M: the denied event is the only record")
    reg.close()

    # ---------------------------------------------------------------- N -----
    print("\nN. forged telemetry is powerless")
    reg = SQLiteStorage(tmp / "n.db")
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "outcome": "claimed"}))
    check(reg.get_work(row.work_id).status == SchedulerWorkStatus.QUEUED,
          "N: forged claim event created no ownership")
    reg.claim(row.work_id, worker_id="w-real", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.append_scheduler_event(AuditEvent(
        kind="work.completed", ts=T0,
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "outcome": "completed"}))
    check(reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING,
          "N: forged completion event did not complete the work")
    real_lease = reg.get_work(row.work_id).lease_expires_at
    reg.append_scheduler_event(AuditEvent(
        kind="work.heartbeat", ts=T0,
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "lease_expires_at": "2099-01-01T00:00:00+00:00",
                "outcome": "extended"}))
    check(reg.get_work(row.work_id).lease_expires_at == real_lease,
          "N: forged heartbeat never extended the lease")
    reg.close()

    # ---------------------------------------------------------------- O -----
    print("\nO. bounded retention / pruning")
    reg = SQLiteStorage(tmp / "o.db")
    for i in range(5):
        reg.append_scheduler_event(AuditEvent(
            kind="work.queued", ts=_iso_plus(T0, i * 10),
            detail={"work_id": f"sw-{i}"}))
    removed = reg.prune_scheduler_events(cutoff=_iso_plus(T0, 25))
    check(removed == 3, "O: prune removed events older than the cutoff")
    remaining = _kinds(reg)
    check(len(remaining) == 2, "O: recent events preserved (no silent delete)")
    check(reg.scheduler_event_count() == 2, "O: event count observable")
    reg.close()

    # ---------------------------------------------------------------- P -----
    print("\nP. CLI JSON output (scheduler watch --json)")
    db_p = tmp / "p.db"
    reg = SQLiteStorage(db_p)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.close()
    proc = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler", "watch",
         "--json", "--work", row.work_id, "--db", str(db_p)],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parent.parent))
    if not proc.stdout.strip():
        raise SystemExit(f"P subprocess failed rc={proc.returncode} "
                         f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
    rows = json.loads(proc.stdout)
    check(proc.returncode == 0 and any(
        r["kind"] == "work.claimed" for r in rows),
        "P: watch --json emits stable machine-readable events")
    check(all({"id", "ts", "kind", "detail"}.issubset(r) for r in rows),
          "P: JSON rows carry id/ts/kind/detail")
    check(len(rows) >= 2,
          "P: JSON covers the claim and its preceding refill")

    print("\n" + "=" * 78)
    print(f"ADR-028 demo PASSED ({CHECKS} checks) - scheduler observability")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
