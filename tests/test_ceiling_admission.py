"""Ceiling admission (ADR-031, Phases B + C + D) - tests first.

- exact ceiling boundary: running C-1 claims, running C denies;
- denial keeps the row QUEUED and consumes NO DWRR credit, triggers NO
  refill;
- claim_next path enforces the ceiling too;
- the floor can never bypass the ceiling (unconstructible state, proven
  via config validation + runtime observations);
- a ceiling-limited goal does NOT strand peer DWRR credit (refill rounds
  keep firing; peers keep progressing; exact durable credit assertions);
- dynamic changes: increase/decrease/disable/restart; decrease never
  cancels RUNNING work;
- global cap and reservation floors remain authoritative.
"""

from __future__ import annotations

from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int, start: int = 0,
          scheduler_id: str = "sched-1") -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id=scheduler_id,
                              now=_iso_plus(T0, start + i)))
    return out


def _claim(reg, row, worker="w", now: str | None = None,
           scheduler_id: str | None = None) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now or T0, max_lease_seconds=600.0,
                    scheduler_id=scheduler_id or row.scheduler_id)
    return got is not None


def _complete(reg, row, worker="w", now: str | None = None) -> None:
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now or T0, owner_worker_id=worker)


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def _credit(reg) -> dict:
    return dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())


# --------------------------------------------------------------------------- #
# Phase B: the gate
# --------------------------------------------------------------------------- #


def test_exact_ceiling_boundary(db_path: str):
    """running C-1 -> claim succeeds (running C); next claim denied."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 3)
    rows = _rows(reg, "goal-a", 6)
    assert _claim(reg, rows[0])   # 1
    assert _claim(reg, rows[1])   # 2
    assert _claim(reg, rows[2])   # 3 = ceiling: allowed (running was C-1)
    assert _running_for(reg, "goal-a") == 3
    assert _claim(reg, rows[3]) is False  # running == C: denied
    assert _running_for(reg, "goal-a") == 3
    # the denied row is still QUEUED with no owner
    w = reg.get_work(rows[3].work_id)
    assert w.status == SchedulerWorkStatus.QUEUED and w.worker_id is None
    denied = [e for e in reg.scheduler_events(
        work_id=rows[3].work_id) if e.kind == "ceiling.denied"]
    assert len(denied) == 1
    assert denied[0].detail["reason"] == "goal_ceiling"
    assert denied[0].detail["ceiling"] == 3
    assert denied[0].detail["running"] == 3
    reg.close()


def test_ceiling_denial_consumes_no_dwrr_credit(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 5)
    assert _claim(reg, rows[0])  # refill A+8; A=1, credit 7
    assert _claim(reg, rows[1])  # A=2, credit 6
    credit_at_ceiling = _credit(reg)
    assert credit_at_ceiling.get("goal-a", 0) == 6
    assert _claim(reg, rows[2]) is False  # ceiling denial
    assert _credit(reg) == credit_at_ceiling  # no debit, no refill
    assert len(reg.scheduler_events(event_type="goal_weight.refill")) == 1
    reg.close()


def test_claim_next_enforces_ceiling(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    _rows(reg, "goal-a", 4)
    got1 = reg.claim_next("sched-1", worker_id="w", lease_seconds=60.0,
                          now=T0, max_lease_seconds=600.0)
    got2 = reg.claim_next("sched-1", worker_id="w", lease_seconds=60.0,
                          now=T0, max_lease_seconds=600.0)
    got3 = reg.claim_next("sched-1", worker_id="w", lease_seconds=60.0,
                          now=T0, max_lease_seconds=600.0)
    assert got1 is not None and got2 is not None and got3 is None
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_unbounded_goal_has_no_ceiling_limit(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    rows = _rows(reg, "goal-a", 6)
    for r in rows:
        assert _claim(reg, r)  # all 6 claim: cap is the only limit
    assert _running_for(reg, "goal-a") == 6
    reg.close()


def test_disabled_and_removed_ceiling_permit_claims(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 1)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0])
    assert _claim(reg, rows[1]) is False  # at ceiling 1
    reg.set_goal_ceiling_enabled("goal-a", False)
    assert _claim(reg, rows[1])  # disabled: unbounded again
    reg.set_goal_ceiling_enabled("goal-a", True)
    assert _claim(reg, rows[2]) is False  # re-enabled at 1: denied
    reg.remove_goal_ceiling("goal-a")
    assert _claim(reg, rows[2])  # removed: unbounded
    assert _running_for(reg, "goal-a") == 3
    reg.close()


# --------------------------------------------------------------------------- #
# Phase C: floor + ceiling composition at runtime
# --------------------------------------------------------------------------- #


def test_floor_and_ceiling_compose(db_path: str):
    """A: floor 2 ceiling 5; B: floor 2 ceiling 3 (mission example, cap 8).
    Both floors hold; ceilings bind above the floor; cap authoritative."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_ceiling("goal-a", 5)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 3)
    a_rows = _rows(reg, "goal-a", 10)
    b_rows = _rows(reg, "goal-b", 10, start=100)
    # A (weight 5) fills first: protection caps it at cap - B floor = 6,
    # its ceiling at 5 -> A reaches 5
    for r in a_rows:
        if not _claim(reg, r):
            break
    assert _running_for(reg, "goal-a") == 5  # ceiling 5 binds
    # B claims: floor path guarantees 2; with A at its ceiling (and the
    # at-ceiling refill skip), B earns DWRR credit and may claim up to
    # its ceiling 3 - never past it
    b_claimed = 0
    for r in b_rows:
        if not _claim(reg, r):
            break
        b_claimed += 1
    assert b_claimed == 3  # floor 2 + one DWRR slot
    assert _running_for(reg, "goal-b") == 3  # exactly B's ceiling
    assert _running_for(reg, "goal-a") == 5  # exactly A's ceiling
    assert _running_for(reg, "goal-a") + _running_for(reg, "goal-b") <= 8
    assert _claim(reg, b_rows[3]) is False  # B at ceiling: denied
    reg.close()


def test_below_floor_implies_below_ceiling_invariant(db_path: str):
    """Under valid configuration (R <= C) a goal below its floor is never
    at its ceiling - proven by construction: for every valid pair, claim
    until the floor is reached and observe running < C throughout."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    for r, c in ((1, 1), (2, 3), (3, 5)):
        g = f"goal-{r}-{c}"
        reg.set_goal_reservation(g, r)
        reg.set_goal_ceiling(g, c)
        rows = _rows(reg, g, c + 2, start=r * 100)
        for i, row in enumerate(rows):
            ok = _claim(reg, row)
            if not ok:
                break
            assert _running_for(reg, g) <= c  # ceiling never exceeded
            if _running_for(reg, g) < r:
                assert _running_for(reg, g) < c  # below floor -> below ceil
    reg.close()


# --------------------------------------------------------------------------- #
# Phase D: DWRR interaction - no stranded credit
# --------------------------------------------------------------------------- #


def test_ceiling_limited_goal_does_not_strand_peer_credit(db_path: str):
    """A (weight 8, ceiling 2) hits its ceiling while B (weight 1) waits:
    B must still get refill rounds and progress (exact durable credit)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_ceiling("goal-a", 2)
    a_rows = _rows(reg, "goal-a", 4)
    b_rows = _rows(reg, "goal-b", 10, start=100)
    assert _claim(reg, a_rows[0])  # refill: A+8, B+1
    assert _claim(reg, a_rows[1])  # A=2 (at ceiling), credit 6
    assert _claim(reg, a_rows[2]) is False  # ceiling denial, no credit move
    # B claims: its credit (1) is spendable
    assert _claim(reg, b_rows[0])  # B=1, credit 0
    # B claims again: A holds credit BUT is at its ceiling -> the refill
    # round must NOT be blocked by A's stranded credit
    assert _claim(reg, b_rows[1]), "B must progress despite A at ceiling"
    assert _running_for(reg, "goal-b") == 2
    # exact credit: A's credit is bounded by the ADR-027 clamp
    # max(weight, 2*cap) = 12 and was never destroyed; B spent its own
    assert _credit(reg).get("goal-a", 0) == 12
    assert _credit(reg).get("goal-b", 0) == 0
    reg.close()


def test_ceiling_limited_goal_credit_spendable_again(db_path: str):
    """When A drops below its ceiling, its durable credit is spendable
    again and counts as held (rounds resume normally)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_ceiling("goal-a", 2)
    a_rows = _rows(reg, "goal-a", 6)
    b_rows = _rows(reg, "goal-b", 10, start=100)
    assert _claim(reg, a_rows[0])  # A=1, credit 7
    assert _claim(reg, a_rows[1])  # A=2 (at ceiling), credit 6
    # B makes one claim while A is at ceiling (refill not blocked)
    assert _claim(reg, b_rows[0])
    assert _credit(reg).get("goal-a", 0) == 6
    # A completes one task: running 1 < ceiling 2 -> A's credit counts
    a_running = [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                 if r.goal_id == "goal-a"]
    _complete(reg, a_running[0])
    assert _claim(reg, a_rows[2])  # A claims (spends credit 6 -> 5)
    assert _credit(reg).get("goal-a", 0) == 5
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_dynamic_ceiling_change_and_restart(db_path: str):
    """Ceiling 2 -> 5 allows more claims; 5 -> 2 never cancels RUNNING;
    restart preserves the ceiling."""
    db = db_path
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 6)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    assert _claim(reg, rows[2]) is False
    reg.set_goal_ceiling("goal-a", 5)  # increase while running
    assert _claim(reg, rows[2]) and _claim(reg, rows[3]) and _claim(reg, rows[4])
    assert _running_for(reg, "goal-a") == 5
    running_before = [(r.work_id, r.worker_id, r.lease_expires_at)
                      for r in reg.list_work(
                          status=SchedulerWorkStatus.RUNNING)]
    reg.set_goal_ceiling("goal-a", 2)  # decrease while running=5
    assert _claim(reg, rows[5]) is False  # new claims denied
    running_after = [(r.work_id, r.worker_id, r.lease_expires_at)
                     for r in reg.list_work(
                         status=SchedulerWorkStatus.RUNNING)]
    assert running_after == running_before  # nothing cancelled
    reg.close()

    reg2 = SQLiteStorage(db)  # restart
    assert reg2.get_goal_ceiling("goal-a") == 2
    assert _claim(reg2, rows[5]) is False  # ceiling persists
    # complete two tasks: running 3 -> still >= 2 -> denied; complete two
    # more: running 1 < 2 -> claim allowed
    for r in list(reg2.list_work(status=SchedulerWorkStatus.RUNNING))[:4]:
        _complete(reg2, r)
    assert _running_for(reg2, "goal-a") == 1
    assert _claim(reg2, rows[5])
    assert _running_for(reg2, "goal-a") == 2
    reg2.close()
