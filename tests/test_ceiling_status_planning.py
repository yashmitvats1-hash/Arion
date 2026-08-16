"""Ceiling status + planning (ADR-031, Phases H + I) - tests first.

- capacity_snapshot per-goal: ceiling (None = unbounded), ceiling_enabled,
  ceiling_headroom (None vs int, never an invented number);
- aggregates: ceiling_limited_goal_count, goals_at_ceiling,
  recent_ceiling_denials;
- explanation state `goal_ceiling_limited` (with disclaimer; projection
  never mutates DWRR credit);
- reservation_feasibility validates floor <= ceiling (proposed mode,
  reason floor_exceeds_ceiling; ceilings never count toward the cap);
- simulate_reservation_change floor/ceiling validity;
- simulate_ceiling_change and simulate_goal_policy: dry-runs, never
  mutate anything; validation fails closed.
"""

from __future__ import annotations

import pytest

from arion.state.scheduler_work import SchedulerRegistryError, SchedulerWorkStatus
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


def _claim(reg, row, worker="w") -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=T0, max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    return got is not None


def _state(reg) -> dict:
    return {
        "ceilings": reg.list_goal_ceilings(),
        "reservations": reg.list_goal_reservations(),
        "weights": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id)
                 for r in reg.list_work()],
    }


# --------------------------------------------------------------------------- #
# Phase H: status
# --------------------------------------------------------------------------- #


def test_snapshot_exposes_ceiling_fields(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 5)
    rows = _rows(reg, "goal-a", 6)
    for r in rows[:3]:
        _claim(reg, r)
    snap = reg.capacity_snapshot(now=T0)
    a = [g for g in snap["goals"] if g["goal_id"] == "goal-a"][0]
    assert a["ceiling"] == 5
    assert a["ceiling_enabled"] is True
    assert a["ceiling_headroom"] == 2  # 5 - 3
    assert snap["ceiling_limited_goal_count"] == 1
    assert snap["goals_at_ceiling"] == []
    assert snap["recent_ceiling_denials"] == 0
    assert "goal_ceilings" in snap
    reg.close()


def test_unbounded_ceiling_headroom_is_none(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    rows = _rows(reg, "goal-a", 2)
    _claim(reg, rows[0])
    snap = reg.capacity_snapshot(now=T0)
    a = [g for g in snap["goals"] if g["goal_id"] == "goal-a"][0]
    assert a["ceiling"] is None
    assert a["ceiling_headroom"] is None  # never an invented integer
    assert snap["ceiling_limited_goal_count"] == 0
    reg.close()


def test_goals_at_ceiling_and_recent_denials(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    assert _claim(reg, rows[2]) is False  # ceiling denial
    snap = reg.capacity_snapshot(now=T0)
    assert snap["goals_at_ceiling"] == ["goal-a"]
    assert snap["recent_ceiling_denials"] == 1
    reg.close()


def test_goal_ceiling_limited_explanation_state(db_path: str):
    """At ceiling with capacity available: the ceiling is the binding
    constraint (outranks credit states); disclaimer present."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    exp = reg.explain_goal_eligibility("goal-a")
    assert exp["state"] == "goal_ceiling_limited"
    assert exp["eligible"] is False
    assert "authoritative at claim time" in exp["note"]
    reg.close()


def test_explanation_distinguishes_ceiling_from_other_states(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    # below ceiling, credit available -> eligible (not ceiling_limited)
    assert _claim(reg, rows[0])
    exp = reg.explain_goal_eligibility("goal-a")
    assert exp["state"] == "eligible"
    # at ceiling with the cap EXHAUSTED -> the ceiling state still wins
    # only when capacity is available; with cap full it is exhaustion
    reg2 = SQLiteStorage(db_path + ".capfull")
    reg2.set_scheduler_global_max(2)
    reg2.set_goal_ceiling("goal-a", 2)
    rows2 = _rows(reg2, "goal-a", 4)
    assert _claim(reg2, rows2[0]) and _claim(reg2, rows2[1])
    assert reg2.explain_goal_eligibility("goal-a")["state"] == \
        "global_capacity_exhausted"
    reg.close()
    reg2.close()


def test_explanation_projection_never_mutates_credit(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    before = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    reg.explain_goal_eligibility("goal-a")
    reg.capacity_snapshot(now=T0)
    after = dict(reg._conn.execute(
        "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall())
    assert after == before
    reg.close()


# --------------------------------------------------------------------------- #
# Phase I: planning
# --------------------------------------------------------------------------- #


def test_feasibility_validates_floor_vs_ceiling(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-b", 3)
    f = reg.reservation_feasibility(proposed={"goal-a": 2, "goal-b": 2})
    assert f["feasible"] is True and f["reason"] == "ok"
    f2 = reg.reservation_feasibility(proposed={"goal-b": 4})
    assert f2["feasible"] is False
    assert f2["reason"] == "floor_exceeds_ceiling"
    assert f2["overflow"] == 0
    # a disabled ceiling does not constrain the floor
    reg.set_goal_ceiling_enabled("goal-b", False)
    f3 = reg.reservation_feasibility(proposed={"goal-b": 4})
    assert f3["feasible"] is True
    reg.close()


def test_ceilings_never_count_toward_feasibility(db_path: str):
    """A ceiling 8 + B ceiling 8 with cap 8: floors 2+2 are feasible even
    though the ceilings sum to 16."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 8)
    reg.set_goal_ceiling("goal-b", 8)
    f = reg.reservation_feasibility(proposed={"goal-a": 2, "goal-b": 2})
    assert f["feasible"] is True
    assert f["proposed_total"] == 4
    reg.close()


def test_simulate_reservation_change_checks_ceiling(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-b", 3)
    sim = reg.simulate_reservation_change("goal-b", 2)
    assert sim["floor_ceiling_valid"] is True and sim["feasible"] is True
    sim = reg.simulate_reservation_change("goal-b", 5)
    assert sim["floor_ceiling_valid"] is False
    assert sim["feasible"] is False
    reg.close()


def test_simulate_ceiling_change(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-b", 4)
    reg.set_goal_reservation("goal-b", 2)
    rows = _rows(reg, "goal-b", 3, start=100)
    _claim(reg, rows[0])  # running 1
    sim = reg.simulate_ceiling_change("goal-b", 6)
    assert sim["current_ceiling"] == 4
    assert sim["proposed_ceiling"] == 6
    assert sim["floor"] == 2
    assert sim["floor_ceiling_valid"] is True
    assert sim["ceiling_headroom_now"] == 3   # 4 - 1
    assert sim["ceiling_headroom_proposed"] == 5  # 6 - 1
    assert sim["headroom_delta"] == "increase"
    # lowering below the floor is invalid
    sim2 = reg.simulate_ceiling_change("goal-b", 1)
    assert sim2["floor_ceiling_valid"] is False
    # None means unbounded
    sim3 = reg.simulate_ceiling_change("goal-b", None)
    assert sim3["proposed_ceiling"] is None
    assert sim3["ceiling_headroom_proposed"] is None
    assert sim3["headroom_delta"] == "increase"
    reg.close()


def test_simulate_goal_policy(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 5)
    reg.set_goal_weight("goal-b", 3)
    sim = reg.simulate_goal_policy("goal-b", reservation=2, ceiling=4,
                                   weight=4)
    assert sim["current_floor"] == 2 and sim["proposed_floor"] == 2
    assert sim["current_ceiling"] == 5 and sim["proposed_ceiling"] == 4
    assert sim["current_weight"] == 3 and sim["proposed_weight"] == 4
    assert sim["floor_ceiling_valid"] is True
    assert sim["feasible"] is True
    assert sim["ceiling_headroom"] == 4  # 4 - 0 running
    # invalid pair reported, never persisted
    sim2 = reg.simulate_goal_policy("goal-b", reservation=6)
    assert sim2["floor_ceiling_valid"] is False
    assert sim2["feasible"] is False
    assert sim2["reason"] == "floor_exceeds_ceiling"
    assert reg.get_goal_reservation("goal-b") == 2  # unchanged
    reg.close()


def test_simulations_never_mutate(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 5)
    rows = _rows(reg, "goal-b", 2, start=100)
    _claim(reg, rows[0])
    before = _state(reg)
    for _ in range(3):
        reg.simulate_ceiling_change("goal-b", 3)
        reg.simulate_ceiling_change("goal-b", None)
        reg.simulate_goal_policy("goal-b", reservation=1, ceiling=2,
                                 weight=9)
        reg.reservation_feasibility(proposed={"goal-b": 1})
        reg.simulate_reservation_change("goal-b", 1)
    assert _state(reg) == before
    reg.close()


def test_ceiling_simulation_validation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    for bad in (0, -1, 1.5, True, 10**9):
        with pytest.raises(SchedulerRegistryError):
            reg.simulate_ceiling_change("goal-a", bad)  # type: ignore[arg-type]
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_ceiling_change("", 2)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_goal_policy("goal-a", reservation=-1)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_goal_policy("goal-a", ceiling=0)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_goal_policy("goal-a", weight=0)
    reg.close()
