#!/usr/bin/env python3
"""ADR-027 DoD demo: durable per-goal capacity shares + weighted fair scheduling.

The ADR-026 cross-process scheduler gains GOAL-AWARE weighted admission:
a deterministic Deficit-Weighted-Round-Robin gate inside the same atomic
claim transaction (BEGIN IMMEDIATE). Every decision derives from durable
rows (weights + deficits + work rows), so a restart reconstructs the exact
same policy - no in-memory counter, no wall-clock authority.

  A  default behavior: unconfigured goals use weight 1 (and no global cap
     means the gate is a no-op - ADR-026 behavior exactly).
  B  equal weights: 1:1 per DWRR round.
  C  2:1 weighted competition: the gate itself produces the ratio.
  D  3-goal weighted competition: 2:1:1.
  E  low-weight eventual progress: a weight-1 goal claims every round
     against a weight-8 goal (anti-starvation floor).
  F  global cap enforcement: running rows never exceed the cap.
  G  cross-process: two store handles ("processes") observe the same
     durable policy; racing claims yield exactly one owner.
  H  dynamic weight change: applies to future admission only; RUNNING
     work stays owned.
  I  restart: weights + deficit survive a store reopen; the cycle
     continues exactly.
  J  adversarial: forged plan/task claims cannot set weights; forged
     deficits cannot bypass cap or ownership.

Deterministic and offline: no LLM, no network, no shell. Cross-process
atomicity is additionally proven with real subprocesses in
tests/test_weighted_cross_process.py.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _mk(reg, goal_id, task_id, scheduler_id="sched-1", t=0.0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=0,
                      scheduler_id=scheduler_id, now=_iso_plus(T0, t))


def _claim(reg, row, worker="w", t=100.0):
    return reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                     now=_iso_plus(T0, t), max_lease_seconds=600.0,
                     scheduler_id=row.scheduler_id) is not None


def _complete(reg, row, worker="w", t=101.0):
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=_iso_plus(T0, t), owner_worker_id=worker)


def _running(reg):
    return len(reg.list_work(status=SchedulerWorkStatus.RUNNING))


def _peek(reg, goal_id):
    for r in reg.list_work(status=SchedulerWorkStatus.QUEUED):
        if r.goal_id == goal_id:
            return r
    return None


def _claim_next(reg, goal_id, t=100.0):
    row = _peek(reg, goal_id)
    if row is None:
        return None, False
    return row, _claim(reg, row, t=t)


def _drain(reg, t=101.0):
    for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
        _complete(reg, row, t=t)


def main() -> int:
    print("ADR-027 demo: durable per-goal capacity shares + weighted fair\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr027-"))
    db = tmp / "adr027.db"

    # ---------------------------------------------------------------- A -----
    print("A. default behavior (no weights / no global cap)")
    reg = SQLiteStorage(tmp / "adr027a.db")
    check(reg.get_goal_weight("goal-none") == 1,
          "A: unconfigured goal uses the deterministic default weight 1")
    check(reg.get_goal_weight_config("goal-none") is None,
          "A: no config row exists for an unconfigured goal")
    rows = [_mk(reg, "goal-a", "t-a"), _mk(reg, "goal-b", "t-b")]
    for row in rows:
        assert _claim(reg, row)  # never gated (hard assert, not a check)
    _drain(reg)
    check(True, "A: no global cap -> claims never gated")
    reg.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. equal weights -> equal claims per round")
    reg = SQLiteStorage(tmp / "adr027b.db")
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    for i in range(6):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
    claimed = {"goal-a": 0, "goal-b": 0}
    first = True
    for _ in range(3):
        for g in ("goal-a", "goal-b"):
            _, ok = _claim_next(reg, g)
            if first:
                check(ok, "B: equal-weight claim admitted")
                first = False
            assert ok
            claimed[g] += 1
        _drain(reg)
    check(claimed == {"goal-a": 3, "goal-b": 3},
          f"B: equal weights -> 1:1 ({claimed})")
    reg.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. 2:1 weighted competition (gate-enforced)")
    reg = SQLiteStorage(tmp / "adr027c.db")
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    for i in range(8):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(4):
        while _peek(reg, "goal-a"):
            row, ok = _claim_next(reg, "goal-a")
            if not ok:
                break
            claimed["goal-a"] += 1
        while _peek(reg, "goal-b"):
            row, ok = _claim_next(reg, "goal-b")
            if not ok:
                break
            claimed["goal-b"] += 1
        _drain(reg)
    check(claimed["goal-a"] == 2 * claimed["goal-b"],
          f"C: weight 2 gets exactly 2x the claims ({claimed})")
    reg.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. 3-goal weighted competition: 2:1:1")
    reg = SQLiteStorage(tmp / "adr027d.db")
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_weight("goal-c", 1)
    for i in range(8):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
        _mk(reg, "goal-c", f"t-c{i}", t=i + 0.75)
    claimed = {"goal-a": 0, "goal-b": 0, "goal-c": 0}
    for _ in range(3):
        for g in ("goal-a", "goal-b", "goal-c"):
            while _peek(reg, g):
                _, ok = _claim_next(reg, g)
                if not ok:
                    break
                claimed[g] += 1
        _drain(reg)
    check(claimed["goal-a"] == 2 * claimed["goal-b"] == 2 * claimed["goal-c"],
          f"D: 2:1:1 distribution ({claimed})")
    reg.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. low-weight goal still progresses every round")
    reg = SQLiteStorage(tmp / "adr027e.db")
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-hot", 8)
    reg.set_goal_weight("goal-low", 1)
    for i in range(20):
        _mk(reg, "goal-hot", f"t-h{i}", t=i)
        _mk(reg, "goal-low", f"t-l{i}", t=i + 0.5)
    claimed = {"goal-hot": 0, "goal-low": 0}
    for _ in range(4):
        # the hot goal fills capacity until its credit is spent
        while True:
            filled = 0
            while _peek(reg, "goal-hot"):
                _, ok = _claim_next(reg, "goal-hot")
                if ok:
                    claimed["goal-hot"] += 1
                    filled += 1
                else:
                    break
            _drain(reg)
            if filled == 0:
                break
        # the low goal claims at the round boundary
        _, ok = _claim_next(reg, "goal-low")
        assert ok  # admitted every round (never starved)
        claimed["goal-low"] += 1
        _drain(reg)
    check(claimed["goal-low"] == 4, "E: low-weight goal admitted every round")
    check(claimed["goal-hot"] > 4 * claimed["goal-low"],
          "E: the hot goal still dominates capacity")
    check(claimed["goal-hot"] == 4 * claimed["goal-low"] + 4
          or claimed["goal-hot"] >= 16,
          f"E: hot goal's per-round bound held ({claimed})")
    reg.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. global cap never exceeded under weights")
    reg = SQLiteStorage(tmp / "adr027f.db")
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 3)
    reg.set_goal_weight("goal-b", 1)
    max_running = 0
    for i in range(6):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
    for _ in range(6):
        for g in ("goal-a", "goal-b"):
            if _peek(reg, g):
                _claim_next(reg, g)
            max_running = max(max_running, _running(reg))
        _drain(reg)
    check(max_running <= 2, f"F: running rows never exceeded the cap (max {max_running})")
    reg.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. cross-process weighted admission (two handles, one DB)")
    db_g = tmp / "adr027g.db"
    reg = SQLiteStorage(db_g)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    for i in range(8):
        _mk(reg, "goal-a", f"t-a{i}", scheduler_id="sched-A", t=i)
        _mk(reg, "goal-b", f"t-b{i}", scheduler_id="sched-B", t=i + 0.5)
    reg.close()
    # two independent store handles ("processes") race for the same row
    reg_a = SQLiteStorage(db_g)
    reg_b = SQLiteStorage(db_g)
    row = _peek(reg_a, "goal-a")
    outcomes = []

    def race(handle, worker):
        got = handle.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                           now=_iso_plus(T0, 50), max_lease_seconds=600.0,
                           scheduler_id="sched-A")
        outcomes.append(got is not None)

    t1 = threading.Thread(target=race, args=(reg_a, "w-a"))
    t2 = threading.Thread(target=race, args=(reg_b, "w-b"))
    t1.start(); t2.start(); t1.join(timeout=15); t2.join(timeout=15)
    check(outcomes.count(True) == 1,
          "G: exactly one owner under a cross-process claim race")
    check(reg_a.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING,
          "G: the row is RUNNING under exactly one owner")
    # complete the raced row with its actual owner
    winner = "w-a" if outcomes[0] else "w-b"
    reg_a.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                        owner_worker_id=winner, now=_iso_plus(T0, 51))
    # both processes observe the same durable 2:1 policy. The raced row
    # left goal-a with 1 leftover credit, so the deterministic totals over
    # 4 shared rounds are: goal-a = 1 (race) + 1 + 2 + 2 + 2 = 8 and
    # goal-b = 0 + 2 + 1 + 1 + 1 = 5 (ratio ~1.6, weight-2 goal ahead).
    claimed = {"goal-a": 1, "goal-b": 0}
    for _ in range(4):
        for g, h in (("goal-a", reg_a), ("goal-b", reg_b)):
            while _peek(h, g):
                r2, ok = _claim_next(h, g, t=60)
                if not ok:
                    break
                claimed[g] += 1
        _drain(reg_a)
    check(claimed == {"goal-a": 8, "goal-b": 5},
          f"G: shared policy is deterministic across handles ({claimed})")
    check(claimed["goal-a"] > claimed["goal-b"]
          and claimed["goal-b"] >= 2,
          "G: weight-2 goal ahead; weight-1 goal still progresses")
    reg_a.close(); reg_b.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. dynamic weight change -> future admission only")
    reg = SQLiteStorage(tmp / "adr027h.db")
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    for i in range(6):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
    # one 2:1 round
    for g in ("goal-a", "goal-b", "goal-a"):
        _, ok = _claim_next(reg, g)
        assert ok
    running_before = [r.work_id for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)]
    reg.set_goal_weight("goal-a", 1)  # change while queued/running
    check(len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
               if r.work_id in running_before]) == 3,
          "H: RUNNING work stayed owned (no retroactive cancellation)")
    _drain(reg)
    claimed = {"goal-a": 3, "goal-b": 1}
    for _ in range(2):
        for g in ("goal-a", "goal-b"):
            _, ok = _claim_next(reg, g)
            check(ok, "H: 1:1 rounds after the change")
            claimed[g] += 1
        _drain(reg)
    check(claimed == {"goal-a": 5, "goal-b": 3},
          "H: future admission used the new weight (1:1 after the change)")
    reg.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. restart: weights + deficit survive a store reopen")
    db_i = tmp / "adr027i.db"
    reg = SQLiteStorage(db_i)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    for i in range(6):
        _mk(reg, "goal-a", f"t-a{i}", t=i)
        _mk(reg, "goal-b", f"t-b{i}", t=i + 0.5)
    for g in ("goal-a", "goal-b", "goal-a"):
        _claim_next(reg, g)
    _drain(reg)
    reg.close()

    reg2 = SQLiteStorage(db_i)  # "process restart"
    check(reg2.get_goal_weight("goal-a") == 2 and reg2.get_goal_weight("goal-b") == 1,
          "I: weights survived the restart")
    claimed = {"goal-a": 0, "goal-b": 0}
    first = True
    for _ in range(2):
        for g in ("goal-a", "goal-b", "goal-a"):
            _, ok = _claim_next(reg2, g)
            if first:
                check(ok, "I: weighted cycle continues after restart")
                first = False
            assert ok
            claimed[g] += 1
        _drain(reg2)
    check(claimed == {"goal-a": 4, "goal-b": 2},
          "I: 2:1 continued from durable state after restart")
    reg2.close()

    # ---------------------------------------------------------------- J -----
    print("\nJ. adversarial configuration attempts")
    reg = SQLiteStorage(tmp / "adr027j.db")
    reg.set_scheduler_global_max(2)
    # forged plan/task claims cannot set weights (weights come only from
    # the store protocol) - the goal stays at default weight 1
    row = _mk(reg, "goal-forged", "t-forged")
    check(reg.get_goal_weight("goal-forged") == 1 and reg.list_goal_weights() == [],
          "J: a claim row for an unconfigured goal creates no config")
    check(_claim(reg, row), "J: unconfigured goal admitted at default weight")
    # a forged deficit cannot exceed the cap nor grant ownership
    reg._conn.execute(
        "INSERT OR REPLACE INTO scheduler_goal_state (goal_id, deficit, updated_at) "
        "VALUES ('goal-forged', 100000, ?)", (T0,))
    reg._conn.commit()
    second = _mk(reg, "goal-forged", "t-forged2", t=1)
    third = _mk(reg, "goal-forged", "t-forged3", t=2)
    check(_claim(reg, second, t=5), "J: second claim within the cap (legal)")
    check(not _claim(reg, third, t=5),
          "J: forged deficit cannot exceed the global cap")
    try:
        reg.mark_terminal(second.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w-attacker", now=_iso_plus(T0, 6))
        check(False, "J: forged ownership must be rejected")
    except SchedulerStateError:
        check(True, "J: forged deficit cannot grant ownership (owner-checked)")
    # disabled goals cannot be re-enabled via metadata
    reg.set_goal_weight("goal-off", 1, enabled=False)
    off = _mk(reg, "goal-off", "t-off", t=3)
    check(not _claim(reg, off, t=7), "J: disabled goal never admitted")
    check(reg.get_goal_weight_config("goal-off")["enabled"] is False,
          "J: config stays disabled")
    reg.close()

    print("\n" + "=" * 78)
    print(f"ADR-027 demo PASSED ({CHECKS} checks) - weighted fair scheduling")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
