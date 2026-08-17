"""Capacity snapshot (ADR-030, Phases A + B) - tests first.

Read-only typed projection over durable scheduler state:

- exact capacity arithmetic (available = max(cap - running, 0));
- no global cap -> None sentinels (never an invented finite capacity);
- configured vs active reservations; idle goals consume nothing;
- per-goal projection: weight/reservation config, running/queued,
  deficit, satisfied, pressure, clamped DWRR credit, eligibility;
- deterministic ordering and bounded fields (no payloads/secrets);
- snapshot computation NEVER mutates any durable state.
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


def _rows(reg, goal_id: str, n: int, start: int = 0) -> list:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                              step_index=i, scheduler_id="sched-1",
                              now=_iso_plus(T0, start + i)))
    return out


def _claim(reg, row, worker="w", now: str | None = None) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now or T0, max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    return got is not None


def test_empty_scheduler_snapshot(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    snap = reg.capacity_snapshot(now=T0)
    assert snap["global_max_concurrency"] == 8
    assert snap["running_count"] == 0
    assert snap["queued_count"] == 0
    assert snap["available_capacity"] == 8
    assert snap["reserved_capacity"] == 0
    assert snap["active_reserved_capacity"] == 0
    assert snap["reservation_pressure"] == 0
    assert snap["unreserved_capacity"] == 8
    assert snap["active_scheduler_count"] == 0
    assert snap["active_goal_count"] == 0
    assert snap["reserved_goal_count"] == 0
    assert snap["goals_below_reservation"] == []
    assert snap["goals_at_reservation"] == []
    assert snap["goals_above_reservation"] == []
    assert snap["goals"] == []
    reg.close()


def test_running_capacity_arithmetic(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    rows = _rows(reg, "goal-a", 6)
    for r in rows[:3]:
        assert _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    assert snap["running_count"] == 3
    assert snap["queued_count"] == 3
    assert snap["available_capacity"] == 5  # max(8 - 3, 0)
    assert snap["unreserved_capacity"] == 5
    assert snap["active_goal_count"] == 1
    assert snap["active_scheduler_count"] == 1
    # never negative even when over-subscribed transiently
    reg2 = SQLiteStorage(db_path)
    for r in rows[3:6]:
        assert _claim(reg2, r)
    snap = reg2.capacity_snapshot(now=T0)
    assert snap["running_count"] == 6
    assert snap["available_capacity"] == 2
    reg2.close()
    reg.close()


def test_no_global_cap_uses_explicit_unbounded_sentinels(db_path: str):
    """No cap: available/unreserved are None (unbounded) - never an
    invented finite capacity. Config views still work."""
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 2)
    _rows(reg, "goal-a", 3)
    snap = reg.capacity_snapshot(now=T0)
    assert snap["global_max_concurrency"] is None
    assert snap["available_capacity"] is None
    assert snap["unreserved_capacity"] is None
    assert snap["reserved_capacity"] == 2  # config view still computed
    assert snap["reservation_pressure"] == 2  # runnable below floor
    reg.close()


def test_configured_vs_active_reservations(db_path: str):
    """A (r2, runnable) + B (r2, idle): configured total 4, active 2
    (B's idle floor consumes nothing), pressure 1. Classification is by
    running vs R, so idle B still reads 'below' (pressure shows it is
    not actually pressured)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 2)
    a_rows = _rows(reg, "goal-a", 4)   # runnable
    # B configured but idle (no rows)
    _claim(reg, a_rows[0])             # A holds 1 of its floor
    snap = reg.capacity_snapshot(now=T0)
    assert snap["reserved_capacity"] == 4       # configured view
    assert snap["active_reserved_capacity"] == 2  # only runnable A
    assert snap["reservation_pressure"] == 1    # A needs 1 more
    assert snap["unreserved_capacity"] == 5     # 8 - 1 running - 2 active
    assert snap["reserved_goal_count"] == 2
    assert snap["goals_below_reservation"] == ["goal-a", "goal-b"]
    assert snap["goals_at_reservation"] == []
    assert snap["goals_above_reservation"] == []
    reg.close()


def test_classification_lists(db_path: str):
    """below = running < R; at = running == R; above = running > R."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(12)
    for g in ("goal-a", "goal-b", "goal-c"):
        reg.set_goal_weight(g, 8)  # no DWRR credit limit for this test
        reg.set_goal_reservation(g, 2)
    a_rows = _rows(reg, "goal-a", 6)
    b_rows = _rows(reg, "goal-b", 2, start=100)
    c_rows = _rows(reg, "goal-c", 4, start=200)
    for r in a_rows[:1]:
        _claim(reg, r)   # A: running 1 < 2 -> below
    for r in b_rows:
        _claim(reg, r)   # B: running 2 == 2 -> at
    for r in c_rows[:3]:
        _claim(reg, r)   # C: running 3 > 2 -> above
    snap = reg.capacity_snapshot(now=T0)
    assert snap["goals_below_reservation"] == ["goal-a"]
    assert snap["goals_at_reservation"] == ["goal-b"]
    assert snap["goals_above_reservation"] == ["goal-c"]
    reg.close()


def test_per_goal_projection_fields(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_reservation("goal-b", 2, by="tester", now=T0)
    a_rows = _rows(reg, "goal-a", 4)
    b_rows = _rows(reg, "goal-b", 3, start=100)
    for r in a_rows[:3]:
        _claim(reg, r)
    for r in b_rows[:1]:
        _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    by_id = {g["goal_id"]: g for g in snap["goals"]}
    assert set(by_id) == {"goal-a", "goal-b"}
    a = by_id["goal-a"]
    assert a["weight"] == 5 and a["weight_enabled"] is True
    assert a["reservation"] == 0 and a["reservation_enabled"] is True
    assert a["running"] == 3 and a["queued"] == 1
    assert a["reservation_deficit"] == 0
    assert a["reservation_satisfied"] is True   # R == 0 -> trivially yes
    assert a["reservation_pressure"] == 0
    assert "dwr_credit" in a and isinstance(a["dwr_credit"], int)
    b = by_id["goal-b"]
    assert b["reservation"] == 2 and b["reservation_enabled"] is True
    assert b["running"] == 1 and b["queued"] == 2
    assert b["reservation_deficit"] == 1
    assert b["reservation_satisfied"] is False
    assert b["reservation_pressure"] == 1
    assert "state" in b and "eligible" in b
    reg.close()


def test_snapshot_bounded_and_no_payloads(db_path: str):
    """No task payloads / prompts / secrets / arbitrary metadata."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    rows = _rows(reg, "goal-a", 2)
    _claim(reg, rows[0])
    snap = reg.capacity_snapshot(now=T0)
    dumped = str(snap)
    for needle in ("task_id", "payload", "prompt", "content", "secret",
                   "rowid", "scheduler_work", "memory"):
        assert needle not in dumped, needle
    reg.close()


def test_snapshot_is_read_only(db_path: str):
    """Computing snapshots never mutates reservations, weights, credit,
    events, or ownership."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 3)
    reg.set_goal_reservation("goal-b", 2)
    rows = _rows(reg, "goal-a", 2)
    _claim(reg, rows[0])
    before_res = reg.list_goal_reservations()
    before_w = reg.list_goal_weights()
    before_credit = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    before_events = reg.scheduler_event_count()
    before_work = [(r.work_id, r.status.value, r.worker_id,
                    r.lease_expires_at)
                   for r in reg.list_work()]
    reg.capacity_snapshot(now=T0)
    reg.capacity_snapshot(now=_iso_plus(T0, 1))
    assert reg.list_goal_reservations() == before_res
    assert reg.list_goal_weights() == before_w
    assert dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()) \
        == before_credit
    assert reg.scheduler_event_count() == before_events
    assert [(r.work_id, r.status.value, r.worker_id, r.lease_expires_at)
            for r in reg.list_work()] == before_work
    reg.close()


def test_goals_sorted_deterministically(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    _rows(reg, "goal-z", 1, start=300)
    _rows(reg, "goal-a", 1, start=100)
    _rows(reg, "goal-m", 1, start=200)
    snap = reg.capacity_snapshot(now=T0)
    assert [g["goal_id"] for g in snap["goals"]] == \
        ["goal-a", "goal-m", "goal-z"]
    reg.close()
