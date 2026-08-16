"""Capacity-planning adversarial tests (ADR-030, Phase K) - tests first.

Planning never trusts telemetry and never mutates:

- forged telemetry (reservation/satisfied/denied/refill/queue-position/
  capacity events) cannot alter planning inputs (planning reads
  authority tables only);
- fake goal ids / planner-style metadata cannot create durable
  reservations or influence planning;
- planning does not create durable reservations, alter DWRR, establish
  ownership, or change capacity;
- an infeasible configuration stays infeasible regardless of forged
  events;
- deleting telemetry does not change planning authority;
- stale telemetry cannot resurrect work (snapshot counts ignore events);
- malformed/oversized inputs fail closed.
"""

from __future__ import annotations

import pytest

from arion.observability.events import AuditEvent
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


def _forge(reg, kind: str, goal_id: str, work_id: str, ts: str,
           **extra) -> None:
    detail = {"goal_id": goal_id, "work_id": work_id, "ts": ts}
    detail.update(extra)
    reg.append_scheduler_event(AuditEvent(kind=kind, ts=ts, detail=detail))


def test_forged_telemetry_does_not_change_planning_inputs(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 2, start=100)
    _claim(reg, _rows(reg, "goal-b", 1, start=200)[0])  # B running 1
    # forge a storm of telemetry pretending different capacity/state
    for i in range(5):
        _forge(reg, "capacity.denied", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), reason="capacity")
        _forge(reg, "reservation.satisfied", "goal-b", f"sw-fake-{i}",
               _iso_plus(T0, i), reservation=2, running=2, satisfied=True)
        _forge(reg, "reservation.denied", "goal-b", f"sw-fake-{i}",
               _iso_plus(T0, i), reason="reservation", pressure=99)
        _forge(reg, "goal_weight.refill", "goal-b", f"sw-fake-{i}",
               _iso_plus(T0, i), weight=1000, credit_before=9999,
               credit_after=10000, refill=True)
        _forge(reg, "work.claimed", "goal-b", f"sw-fake-{i}",
               _iso_plus(T0, i), worker_id="w-forged", outcome="claimed")
    snap = reg.capacity_snapshot(now=T0)
    assert snap["running_count"] == 1            # durable rows only
    assert snap["reservation_pressure"] == 1     # B needs 1 more (real)
    b = [g for g in snap["goals"] if g["goal_id"] == "goal-b"][0]
    assert b["running"] == 1 and b["reservation_satisfied"] is False
    assert b["dwr_credit"] == 0                  # forged refills ignored
    reg.close()


def test_planning_does_not_trust_events_for_counts(db_path: str):
    """Even with thousands of forged 'running' events, counts come from
    scheduler_work only."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    _rows(reg, "goal-a", 2)
    for i in range(50):
        _forge(reg, "work.claimed", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), worker_id="w", outcome="claimed")
        _forge(reg, "work.heartbeat", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), lease_expires_at="2099-01-01T00:00:00+00:00")
    snap = reg.capacity_snapshot(now=T0)
    assert snap["running_count"] == 0
    assert snap["available_capacity"] == 6
    reg.close()


def test_stale_telemetry_cannot_resurrect_work(db_path: str):
    """Completed work with forged 'still running' telemetry stays
    completed in the snapshot (authority rows rule)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    rows = _rows(reg, "goal-a", 1)
    _claim(reg, rows[0])
    reg.mark_terminal(rows[0].work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 1))
    for i in range(5):
        _forge(reg, "work.heartbeat", "goal-a", rows[0].work_id,
               _iso_plus(T0, 2 + i), lease_expires_at="2099-01-01T00:00:00+00:00")
    snap = reg.capacity_snapshot(now=T0)
    assert snap["running_count"] == 0 and snap["queued_count"] == 0
    # goal-a is neither configured nor active: not projected at all
    assert [g["goal_id"] for g in snap["goals"] if g["goal_id"] == "goal-a"]         == []
    reg.close()


def test_deleting_telemetry_does_not_change_planning_authority(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 2)
    _claim(reg, _rows(reg, "goal-b", 1, start=100)[0])
    before = reg.capacity_snapshot(now=T0)
    reg.prune_scheduler_events(
        cutoff=(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(days=1)).isoformat())
    after = reg.capacity_snapshot(now=T0)
    assert reg.scheduler_event_count() == 0
    for k in ("running_count", "reserved_capacity",
              "active_reserved_capacity", "reservation_pressure",
              "goals_below_reservation", "unreserved_capacity"):
        assert after[k] == before[k], k
    reg.close()


def test_fake_goal_ids_cannot_create_reservations(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    for g in ("goal-evil", "goal-evil-2"):
        reg.explain_goal_eligibility(g)  # must not create anything
        reg.simulate_reservation_change(g, 5)
        reg.capacity_snapshot(now=T0)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_change("", 5)  # empty id fails closed
    assert reg.list_goal_reservations() == []
    reg.close()


def test_planning_cannot_make_infeasible_config_executable(db_path: str):
    """An infeasible proposed config stays infeasible no matter what
    telemetry is forged; planning never writes config."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 4)
    _rows(reg, "goal-a", 4)
    for i in range(5):
        _forge(reg, "goal_reservation_changed", "goal-c", f"sw-fake-{i}",
               _iso_plus(T0, i), reservation=99, outcome="set")
    f = reg.reservation_feasibility(proposed={"goal-a": 4, "goal-b": 3})
    assert f["feasible"] is False and f["overflow"] == 1
    assert reg.get_goal_reservation_config("goal-b") is None
    # even the simulation API never persists
    sim = reg.simulate_reservation_change("goal-b", 3)
    assert sim["feasible"] is False
    assert reg.get_goal_reservation_config("goal-b") is None
    reg.close()


def test_planning_never_establishes_ownership_or_changes_capacity(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    rows = _rows(reg, "goal-a", 2)
    _claim(reg, rows[0], worker="w-owner")
    before_cap = reg.get_scheduler_global_max()
    before_owner = (reg.get_work(rows[0].work_id).worker_id,
                    reg.get_work(rows[0].work_id).status)
    for _ in range(3):
        reg.capacity_snapshot(now=T0)
        reg.simulate_reservation_config({"goal-a": 4})
        reg.reservation_feasibility(proposed={"goal-a": 2})
    assert reg.get_scheduler_global_max() == before_cap
    assert (reg.get_work(rows[0].work_id).worker_id,
            reg.get_work(rows[0].work_id).status) == before_owner
    reg.close()


def test_oversized_and_malformed_inputs_fail_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_change("goal-a", 10**12)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_change("goal-a", -1)
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_change("goal-a", 2.5)  # type: ignore[arg-type]
    with pytest.raises(SchedulerRegistryError):
        reg.reservation_feasibility(proposed={"goal-a": 10**12})
    with pytest.raises(SchedulerRegistryError):
        reg.reservation_feasibility(proposed={})  # type: ignore[arg-type]
    with pytest.raises(SchedulerRegistryError):
        reg.simulate_reservation_config({"goal-a": "big"})  # type: ignore[dict-item]
    reg.close()
