"""Capacity feasibility + proposed-policy simulation (ADR-030, C + D).

- reservation_feasibility: current vs full proposed configs; exact
  overflow; affected goals; reason enum; no-cap semantics;
- simulate_reservation_change: replacement semantics, totals,
  remaining capacity, pressure delta, affected goals;
- simulate_reservation_config: full-config variant;
- fail-closed validation (non-int, negative, oversized, empty ids);
- NOTHING mutates: reservations, weights, DWRR credit, scheduler
  events, and work ownership are byte-identical after every call.
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


def _snapshot_state(reg):
    return {
        "reservations": reg.list_goal_reservations(),
        "weights": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id,
                  r.lease_expires_at) for r in reg.list_work()],
    }


def test_feasibility_current_config_ok(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 3)
    reg.set_goal_reservation("goal-c", 2)  # total 7 <= 8
    f = reg.reservation_feasibility()
    assert f["feasible"] is True
    assert f["global_max"] == 8
    assert f["configured_total"] == 7
    assert f["proposed_total"] == 7
    assert f["overflow"] == 0
    assert f["affected_goals"] == ["goal-a", "goal-b", "goal-c"]
    assert f["reason"] == "ok"
    reg.close()


def test_feasibility_current_config_infeasible(db_path: str):
    """The config API rejects impossible writes (ADR-029), so the CURRENT
    config is always feasible by construction; the feasibility evaluator
    reports proposed configurations that would be impossible."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 3)
    reg.set_goal_reservation("goal-b", 2)  # total 5 <= 6
    try:
        reg.set_goal_reservation("goal-c", 4)  # 9 > 6: rejected
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("config API accepted an infeasible write")
    f = reg.reservation_feasibility()
    assert f["feasible"] is True
    assert f["configured_total"] == 5
    # the evaluator judges the impossible proposed config
    f2 = reg.reservation_feasibility(proposed={"goal-a": 3, "goal-b": 2,
                                               "goal-c": 4})
    assert f2["feasible"] is False
    assert f2["proposed_total"] == 9
    assert f2["overflow"] == 3
    assert f2["reason"] == "oversubscribed"
    reg.close()


def test_feasibility_proposed_overflow_exact(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    # mission example: adding D=2 to A2+B3+C2 = 9 -> overflow 1
    f = reg.reservation_feasibility(
        proposed={"goal-a": 2, "goal-b": 3, "goal-c": 2, "goal-d": 2})
    assert f["feasible"] is False
    assert f["proposed_total"] == 9
    assert f["overflow"] == 1
    assert f["affected_goals"] == ["goal-a", "goal-b", "goal-c", "goal-d"]
    reg.close()


def test_feasibility_no_global_cap(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 2)
    f = reg.reservation_feasibility(proposed={"goal-a": 2, "goal-b": 9000})
    assert f["feasible"] is True
    assert f["global_max"] is None
    assert f["overflow"] == 0
    assert f["reason"] == "no_global_cap"
    reg.close()


def test_feasibility_validation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    for bad_proposed in ({"goal-a": -1}, {"goal-a": 1.5},
                         {"goal-a": True}, {"goal-a": 10**9},
                         {"": 1}, {1: 2}, {}):
        with pytest.raises(SchedulerRegistryError):
            reg.reservation_feasibility(proposed=bad_proposed)  # type: ignore[arg-type]
    reg.close()


def test_simulate_reservation_change_replacement_semantics(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 3)  # total 5
    sim = reg.simulate_reservation_change("goal-b", 4)
    assert sim["current_reservation"] == 3
    assert sim["proposed_reservation"] == 4
    assert sim["current_total"] == 5
    assert sim["proposed_total"] == 6
    assert sim["global_max"] == 8
    assert sim["remaining_capacity"] == 2
    assert sim["feasible"] is True and sim["overflow"] == 0
    # replacing an unconfigured goal adds
    sim2 = reg.simulate_reservation_change("goal-c", 5)
    assert sim2["current_reservation"] == 0
    assert sim2["proposed_total"] == 10
    assert sim2["feasible"] is False
    assert sim2["overflow"] == 2
    # removing via 0
    sim3 = reg.simulate_reservation_change("goal-a", 0)
    assert sim3["proposed_total"] == 3
    assert sim3["feasible"] is True
    reg.close()


def test_simulate_pressure_delta(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 4)
    # B runnable at 0 running: pressure 2
    sim = reg.simulate_reservation_change("goal-b", 2)
    assert sim["reservation_pressure_now"] == 2
    assert sim["pressure_delta"] == "unchanged"
    # raising the floor increases pressure
    sim = reg.simulate_reservation_change("goal-b", 4)
    assert sim["reservation_pressure_proposed"] == 4
    assert sim["pressure_delta"] == "increase"
    # lowering it decreases pressure
    sim = reg.simulate_reservation_change("goal-b", 1)
    assert sim["reservation_pressure_proposed"] == 1
    assert sim["pressure_delta"] == "decrease"
    # an idle goal's change does not change pressure
    sim = reg.simulate_reservation_change("goal-idle", 3)
    assert sim["reservation_pressure_now"] == 2
    assert sim["pressure_delta"] == "unchanged"
    reg.close()


def test_simulate_config_full_variant(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 3)
    _rows(reg, "goal-a", 2)
    sim = reg.simulate_reservation_config(
        {"goal-a": 2, "goal-b": 3, "goal-c": 2})
    assert sim["feasible"] is True
    assert sim["current_total"] == 5
    assert sim["proposed_total"] == 7
    assert sim["overflow"] == 0
    assert sim["affected_goals"] == ["goal-a", "goal-b", "goal-c"]
    assert "pressure_delta" in sim
    reg.close()


def test_simulate_validation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    for bad in (-1, 1.5, True, 10**9):
        with pytest.raises(SchedulerRegistryError):
            reg.simulate_reservation_change("goal-a", bad)  # type: ignore[arg-type]
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_change("", 1)
    reg.close()


def test_planning_never_mutates_anything(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_reservation("goal-b", 2)
    rows = _rows(reg, "goal-a", 3)
    _rows(reg, "goal-b", 2, start=100)
    for r in rows[:2]:
        _claim(reg, r)
    before = _snapshot_state(reg)
    # run every planning API repeatedly
    for _ in range(3):
        reg.capacity_snapshot(now=T0)
        reg.reservation_feasibility()
        reg.reservation_feasibility(proposed={"goal-a": 1, "goal-b": 2})
        reg.simulate_reservation_change("goal-b", 4)
        reg.simulate_reservation_change("goal-x", 1)
        reg.simulate_reservation_config({"goal-a": 0, "goal-b": 4})
        reg.explain_goal_eligibility("goal-a")
    after = _snapshot_state(reg)
    assert after == before
    reg.close()


def test_planning_does_not_touch_work_ownership(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    rows = _rows(reg, "goal-b", 2)
    assert _claim(reg, rows[0], worker="w-owner")
    before = reg.get_work(rows[0].work_id)
    reg.simulate_reservation_change("goal-b", 4)
    reg.capacity_snapshot(now=T0)
    after = reg.get_work(rows[0].work_id)
    assert (after.work_id, after.worker_id, after.status,
            after.lease_expires_at) == \
        (before.work_id, before.worker_id, before.status,
         before.lease_expires_at)
    reg.close()
