"""Admission explanation (ADR-030, Phase E) - tests first.

The per-goal `state` is a read-only PROJECTION over the same durable
state the claim path uses - never a gate:

- idle / weight_disabled / reserved_floor / reservation_waiting /
  global_capacity_exhausted / scheduler_share_limited / eligible /
  goal_weight_limited;
- the projection NEVER mutates DWRR credit (credit before == after);
- the explanation carries the authoritative-claim-time disclaimer;
- unknown goals fail closed with a bounded explanation.
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


def test_idle_goal_state(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    exp = reg.explain_goal_eligibility("goal-b")
    assert exp["state"] == "idle"
    assert exp["eligible"] is False
    assert "authoritative at claim time" in exp["note"]
    reg.close()


def test_weight_disabled_never_eligible(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-b", 1, enabled=False)
    _rows(reg, "goal-b", 2)
    exp = reg.explain_goal_eligibility("goal-b")
    assert exp["state"] == "weight_disabled"
    assert exp["eligible"] is False
    reg.close()


def test_reserved_floor_state(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 2, start=100)
    exp = reg.explain_goal_eligibility("goal-b")
    assert exp["state"] == "reserved_floor"
    assert exp["eligible"] is True  # the floor path would admit it
    reg.close()


def test_global_capacity_exhausted_state(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 8)
    a_rows = _rows(reg, "goal-a", 6)
    for r in a_rows[:4]:
        assert _claim(reg, r)
    _rows(reg, "goal-c", 2, start=200)
    exp = reg.explain_goal_eligibility("goal-c")
    assert exp["state"] == "global_capacity_exhausted"
    assert exp["eligible"] is False
    reg.close()


def test_reservation_waiting_when_capacity_exhausted(db_path: str):
    """A below-floor goal with the cap full is 'reservation_waiting' (its
    floor cannot be satisfied at this instant), not a generic exhaustion."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 6)
    for r in a_rows[:4]:
        assert _claim(reg, r)
    _rows(reg, "goal-b", 2, start=100)
    exp = reg.explain_goal_eligibility("goal-b")
    assert exp["state"] == "reservation_waiting"
    assert exp["eligible"] is False
    reg.close()


def test_goal_weight_limited_state(db_path: str):
    """At/above floor, capacity available, but DWRR credit < 1 and peers
    hold credit: the weighted gate would deny until a refill round."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    a_rows = _rows(reg, "goal-a", 10)
    b_rows = _rows(reg, "goal-b", 10, start=100)
    assert _claim(reg, a_rows[0])  # refill: A+8, B+1
    assert _claim(reg, b_rows[0])  # B spends its credit (deficit 0)
    # B at/above floor (R=0), capacity available, credit 0, A holds credit
    exp = reg.explain_goal_eligibility("goal-b")
    assert exp["state"] == "goal_weight_limited"
    assert exp["eligible"] is False
    reg.close()


def test_eligible_state(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    _rows(reg, "goal-a", 4)
    exp = reg.explain_goal_eligibility("goal-a")
    assert exp["state"] == "eligible"
    assert exp["eligible"] is True
    reg.close()


def test_scheduler_share_limited_state(db_path: str):
    """Two schedulers, cap 6 (share 3): sched-a at its share; goal-c's
    rows belong to sched-a -> share_limited (projection, not a gate)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    a_rows = _rows(reg, "goal-a", 6, scheduler_id="sched-a")
    for r in a_rows[:3]:
        assert _claim(reg, r, scheduler_id="sched-a")
    _rows(reg, "goal-x", 1, start=300, scheduler_id="sched-b")  # active=2
    _rows(reg, "goal-c", 2, start=200, scheduler_id="sched-a")
    exp = reg.explain_goal_eligibility("goal-c")
    assert exp["state"] == "scheduler_share_limited"
    assert exp["eligible"] is False
    reg.close()


def test_projection_never_mutates_dwrr_credit(db_path: str):
    """Explaining eligibility must not refill/debit credit (the real gate
    does; the projection must not)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    a_rows = _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 10, start=100)
    assert _claim(reg, a_rows[0])
    before = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    for g in ("goal-a", "goal-b", "goal-none"):
        for _ in range(3):
            reg.explain_goal_eligibility(g)
    after = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    assert after == before
    reg.close()


def test_unknown_goal_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    exp = reg.explain_goal_eligibility("goal-ghost")
    assert exp["state"] == "unknown"
    assert exp["eligible"] is False
    assert "authoritative at claim time" in exp["note"]
    reg.close()
