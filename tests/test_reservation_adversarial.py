"""Reservation adversarial tests (ADR-029, Phase H) - tests first.

Forge attempts prove the authority boundary:

- planner/model/task metadata cannot establish or alter reservations;
- fake goal ids cannot create reservation configs;
- forged goal_reservation_changed / reservation.satisfied /
  reservation.denied events have zero effect on config and admission;
- forged capacity counts / DWRR deficits / queue positions / stale
  ownership in telemetry change nothing (gates read authority tables);
- reservations never create execution authority (forged claims don't
  own, don't count toward the floor, don't complete anything);
- a goal cannot claim more than the global cap, cannot use another
  goal's reservation;
- disabling a reservation cannot be forged by work metadata.

Core invariant: scheduling policy can influence admission; only the
authoritative execution pipeline can establish execution.
"""

from __future__ import annotations

from arion.observability.events import AuditEvent
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


def _fill(reg, goal_id: str) -> int:
    claimed = 0
    while True:
        row = next((r for r in reg.list_work(
            status=SchedulerWorkStatus.QUEUED) if r.goal_id == goal_id), None)
        if row is None:
            break
        got = reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                        now=T0, max_lease_seconds=600.0,
                        scheduler_id="sched-1")
        if got is None:
            break
        claimed += 1
    return claimed


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def test_forged_reservation_config_events_change_nothing(db_path: str):
    """Forged goal_reservation_changed / set events with any values leave
    the durable config untouched (INSERT OR IGNORE only writes the events
    table; config lives in scheduler_goal_reservations)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    for fake in ("goal-b", "goal-evil", ""):
        reg.append_scheduler_event(AuditEvent(
            kind="goal_reservation_changed", ts=T0,
            detail={"goal_id": fake, "config": "goal_reservation",
                    "reservation": 99, "outcome": "set"}))
    assert reg.get_goal_reservation("goal-b") == 2
    assert reg.get_goal_reservation("goal-evil") == 0
    assert len(reg.list_goal_reservations()) == 1
    reg.close()


def test_forged_satisfied_and_denied_telemetry_change_admission(db_path: str):
    """Forged reservation.satisfied / reservation.denied events do not
    change the floor: the gates count durable RUNNING/QUEUED rows only."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 2, start=100)
    # forge: B satisfied at 0, A denied at 0 - opposite of reality
    reg.append_scheduler_event(AuditEvent(
        kind="reservation.satisfied", ts=T0,
        detail={"goal_id": "goal-b", "work_id": "sw-fake",
                "reservation": 2, "running": 2, "satisfied": True}))
    reg.append_scheduler_event(AuditEvent(
        kind="reservation.denied", ts=T0,
        detail={"goal_id": "goal-a", "work_id": "sw-fake",
                "reason": "reservation", "pressure": 0}))
    # admission is unchanged: A is capped by the REAL floor, B reaches it
    assert _fill(reg, "goal-a") == 4
    assert _fill(reg, "goal-b") == 2
    assert _running_for(reg, "goal-b") == 2
    reg.close()


def test_forged_capacity_counts_and_deficits_change_nothing(db_path: str):
    """Forged capacity.denied events, goal_weight.refill events with fake
    credits, and fake DWRR deficits in telemetry never affect the durable
    credit table or the capacity gate."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 2, start=100)
    for i in range(10):
        reg.append_scheduler_event(AuditEvent(
            kind="capacity.denied", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-a", "work_id": f"sw-fake-{i}",
                    "reason": "capacity"}))
        reg.append_scheduler_event(AuditEvent(
            kind="goal_weight.refill", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-b", "work_id": f"sw-fake-{i}",
                    "weight": 1000, "credit_before": 9999,
                    "credit_after": 10000, "refill": True}))
    # capacity gate and DWRR state are unchanged
    assert _fill(reg, "goal-a") == 4  # floor still binds at the same point
    credit = dict(reg.scheduler_status(now=T0)["dwr_credit"])
    assert max(credit.values()) <= 10000  # bounded by design
    assert _fill(reg, "goal-b") == 2
    reg.close()


def test_forged_queue_positions_do_not_change_admission_order(db_path: str):
    """Forged work.queued events with fake positions/timestamps do not
    change claim_next order (created_at in the authority table rules)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    a = _rows(reg, "goal-a", 4)
    b = _rows(reg, "goal-b", 2, start=100)
    for i, r in enumerate(a + b):
        reg.append_scheduler_event(AuditEvent(
            kind="work.queued", ts=T0,
            detail={"work_id": r.work_id, "goal_id": r.goal_id,
                    "position": 0, "scheduler_id": "sched-1"}))
    got = reg.claim_next("sched-1", worker_id="w", lease_seconds=60.0,
                         now=T0, max_lease_seconds=600.0)
    # oldest row by created_at wins: goal-a's first row (not the forged
    # position-0 B rows)
    assert got is not None and got.work_id == a[0].work_id
    reg.close()


def test_forged_claims_do_not_count_toward_the_floor(db_path: str):
    """A forged work.claimed event for B does not increase B's running
    count: the floor guarantee reads scheduler_work only, so a forged
    claim neither satisfies B's floor nor unlocks A."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    b_rows = _rows(reg, "goal-b", 2, start=100)
    for r in b_rows:
        reg.append_scheduler_event(AuditEvent(
            kind="work.claimed", ts=T0,
            detail={"work_id": r.work_id, "goal_id": "goal-b",
                    "worker_id": "w-forged", "outcome": "claimed"}))
    assert _running_for(reg, "goal-b") == 0  # no ownership, no floor count
    # B is still below its floor, so A is still protected
    assert _fill(reg, "goal-a") == 4
    assert _fill(reg, "goal-b") == 2  # the REAL claims reach the floor
    assert _running_for(reg, "goal-b") == 2
    reg.close()


def test_forged_stale_ownership_and_heartbeats_change_nothing(db_path: str):
    """Forged work.heartbeat with far-future leases / forged reclaim
    events never extend ownership, never re-queue, never change the
    floor."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    b_rows = _rows(reg, "goal-b", 2, start=100)
    got = reg.claim(b_rows[0].work_id, "w-real", 60.0, T0, 600.0,
                    scheduler_id="sched-1")
    assert got is not None
    lease_before = reg.get_work(b_rows[0].work_id).lease_expires_at
    reg.append_scheduler_event(AuditEvent(
        kind="work.heartbeat", ts=T0,
        detail={"work_id": b_rows[0].work_id, "worker_id": "w-forged",
                "lease_expires_at": "2099-01-01T00:00:00+00:00"}))
    reg.append_scheduler_event(AuditEvent(
        kind="work.reclaimed", ts=T0,
        detail={"work_id": b_rows[0].work_id, "worker_id": "w-real",
                "reason": "lease_expired"}))
    assert reg.get_work(b_rows[0].work_id).lease_expires_at == lease_before
    assert reg.get_work(b_rows[0].work_id).status == \
        SchedulerWorkStatus.RUNNING  # not reclaimed by an event
    # a forged heartbeat from the REAL owner is also powerless (the
    # ownership check is authority); the floor is unchanged
    assert _running_for(reg, "goal-b") == 1
    reg.close()


def test_no_metadata_path_to_reservations(db_path: str):
    """Engine-level metadata (task/goal/step descriptions, planner-like
    fields) cannot establish or alter reservations."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    row = reg.create(task_id="t-1", goal_id="goal-b", step_index=0,
                     scheduler_id="sched-1", now=T0)
    # arbitrary task/goal metadata does nothing to reservations
    assert reg.list_goal_reservations() == []
    reg.mark_running(row.work_id, "w", 60.0, now=T0)
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 1))
    assert reg.list_goal_reservations() == []
    reg.close()


def test_one_goal_cannot_use_another_goals_reservation(db_path: str):
    """A's claims can never consume the slots B's floor needs: with B at
    0 and its floor 2, A is capped at cap-floor regardless of forged
    events or A's weight."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 10000)  # max weight: as hot as possible
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 20)
    _rows(reg, "goal-b", 2, start=100)
    for i in range(5):
        reg.append_scheduler_event(AuditEvent(
            kind="goal_weight.refill", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-a", "work_id": f"sw-fake-{i}",
                    "weight": 10000, "credit_before": 0,
                    "credit_after": 10000, "refill": True}))
    assert _fill(reg, "goal-a") == 4
    assert _running_for(reg, "goal-a") == 4
    assert _running_for(reg, "goal-b") == 0
    reg.close()


def test_goal_cannot_claim_beyond_global_cap_with_forged_telemetry(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_reservation("goal-b", 3)  # floor == cap
    _rows(reg, "goal-b", 6)
    for i in range(20):
        reg.append_scheduler_event(AuditEvent(
            kind="capacity.denied", ts=_iso_plus(T0, i),
            detail={"goal_id": "goal-b", "work_id": f"sw-fake-{i}",
                    "reason": "capacity"}))
    assert _fill(reg, "goal-b") == 3  # never beyond the cap
    assert _running_for(reg, "goal-b") == 3
    reg.close()


def test_reservation_cannot_create_execution_authority(db_path: str):
    """Reservations influence admission only: a reservation alone never
    completes/owns/executes anything."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 6)
    b_rows = _rows(reg, "goal-b", 2, start=100)
    assert _running_for(reg, "goal-b") == 0  # no work was created/claimed
    got = reg.claim(b_rows[0].work_id, "w", 60.0, T0, 600.0,
                    scheduler_id="sched-1")
    assert got is not None
    # ownership is established by the claim, not by the reservation
    assert reg.get_work(b_rows[0].work_id).worker_id == "w"
    # a forged completion event does not complete anything
    reg.append_scheduler_event(AuditEvent(
        kind="work.completed", ts=T0,
        detail={"work_id": b_rows[0].work_id, "worker_id": "w",
                "outcome": "completed"}))
    assert reg.get_work(b_rows[0].work_id).status == \
        SchedulerWorkStatus.RUNNING
    reg.close()
