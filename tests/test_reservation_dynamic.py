"""Dynamic reservation changes (ADR-029, Phase D) - tests first.

- reservation added / removed / increased / decreased / disabled while
  work is queued: new claims use the CURRENT durable policy;
- RUNNING work is never retroactively cancelled or re-owned;
- a goal becoming idle releases its floor; runnable-again re-engages;
- global cap changes interact with reservations (fail closed below the
  total);
- reservation configuration survives restart and stays authoritative;
- oversubscribing configuration fails closed with no partial write.
"""

from __future__ import annotations

from arion.state.scheduler_work import (
    SchedulerRegistryError,
    SchedulerWork,
    SchedulerWorkStatus,
)
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int, start: int = 0) -> list[SchedulerWork]:
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


def test_reservation_added_while_work_queued(db_path: str):
    """A already holds 4 of cap 6 when B's reservation is added: B's
    queued work then claims its floor (A's next claims are protected)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-a") == 6  # no reservation yet: A takes all
    # add B's reservation while both queues are non-empty
    reg.set_goal_reservation("goal-b", 2)
    # A can no longer fill (protected); A frees two slots, then B claims
    # its floor
    assert _fill(reg, "goal-a") == 0
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-a"][:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    b_filled = _fill(reg, "goal-b")
    assert b_filled == 2
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    reg.close()


def test_reservation_removed_while_work_queued(db_path: str):
    """Removing B's reservation frees A to consume capacity again."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-a") == 4
    assert _fill(reg, "goal-b") == 2
    reg.remove_goal_reservation("goal-b")
    # B frees its two floor slots; A's next claims are no longer
    # protected and A refills to the cap
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-b"][:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    assert _fill(reg, "goal-a") == 2  # up to the cap (6)
    assert _running_for(reg, "goal-a") == 6
    reg.close()


def test_reservation_increased_and_decreased_while_queued(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 1)
    _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 6)
    assert _fill(reg, "goal-a") == 5  # cap 6 - floor 1
    assert _fill(reg, "goal-b") == 1
    reg.set_goal_reservation("goal-b", 3)  # increase while queued
    assert _fill(reg, "goal-a") == 0  # fully protected now (free=2 < 3)
    # A must free capacity before B can claim its raised floor
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-a"][:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    assert _fill(reg, "goal-b") == 2  # B reaches its new floor 3
    assert _running_for(reg, "goal-b") == 3
    reg.set_goal_reservation("goal-b", 1)  # decrease while queued
    assert _fill(reg, "goal-b") == 0  # already at/above floor 1
    # B frees two slots (its floor is now 1); the freed capacity returns
    # to A
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-b"][:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 2))
    assert _fill(reg, "goal-a") == 2
    assert _running_for(reg, "goal-a") == 5
    reg.close()


def test_reservation_disabled_while_queued(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-a") == 4
    reg.set_goal_reservation_enabled("goal-b", False)
    assert _fill(reg, "goal-a") == 2  # no floor while disabled
    assert _running_for(reg, "goal-a") == 6
    # re-enabling re-engages the floor (after A frees two slots)
    reg.set_goal_reservation_enabled("goal-b", True)
    assert _fill(reg, "goal-a") == 0
    for r in [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-a"][:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    assert _fill(reg, "goal-b") == 2
    reg.close()


def test_goal_idle_releases_then_runnable_reengages(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    # B idle: no floor
    assert _fill(reg, "goal-a") == 6
    # B becomes runnable: floor re-engages after A frees two slots
    b_rows = _rows(reg, "goal-b", 2, start=100)
    _fill(reg, "goal-a")  # no change (cap full)
    assert _fill(reg, "goal-b") == 0  # cap full: B must wait
    for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)[:2]:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    assert _fill(reg, "goal-b") == 2  # floor re-engaged
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    # B's queue drains and its work completes: idle again -> A freed
    for r in b_rows:
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 2))
    assert _fill(reg, "goal-a") == 2
    assert _running_for(reg, "goal-a") == 6
    reg.close()


def test_cap_change_with_active_reservations(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_scheduler_global_max(4)  # still >= total: legal
    assert reg.get_scheduler_global_max() == 4
    try:
        reg.set_scheduler_global_max(1)  # below total: fail closed
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("cap dropped below reservation total")
    assert reg.get_scheduler_global_max() == 4
    # raising the cap changes behavior for new claims only
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-a") == 6  # cap 8 - floor 2
    assert _fill(reg, "goal-b") == 2
    reg.close()


def test_config_survives_restart_and_stays_authoritative(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2, by="tester", now=T0)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 4)
    reg.close()

    reg2 = SQLiteStorage(db_path)  # "restart"
    assert reg2.get_goal_reservation("goal-b") == 2
    assert reg2.get_goal_reservation_config("goal-b")["updated_by"] == "tester"
    assert _fill(reg2, "goal-a") == 4  # the floor is enforced after restart
    assert _fill(reg2, "goal-b") == 2
    reg2.close()


def test_oversubscribing_change_fails_closed_without_partial_write(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 3)
    try:
        reg.set_goal_reservation("goal-b", 4)  # total 7 > 6
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("accepted oversubscription")
    assert reg.get_goal_reservation_config("goal-b") is None
    assert reg.get_goal_reservation("goal-a") == 3
    # the failed write left no event behind
    events = [e for e in reg.scheduler_events(
        event_type="goal_reservation_changed")]
    assert len(events) == 1  # only goal-a's successful write
    reg.close()
