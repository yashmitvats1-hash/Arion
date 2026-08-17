"""Ceiling adversarial tests (ADR-031, Phase K) - tests first.

Forge attempts prove the authority boundary:

- forged ceiling config/changed/denied events cannot change a ceiling;
- forged task metadata / planner-style output cannot set a ceiling;
- a ceiling cannot establish ownership;
- a goal cannot exceed its ceiling (even with forged events);
- another goal cannot consume or transfer a goal's ceiling;
- the reservation floor cannot bypass the ceiling;
- DWRR cannot bypass the ceiling;
- scheduler fair share cannot bypass the ceiling;
- the global cap remains authoritative.

Policy influences admission; policy never establishes execution authority.
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


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def _forge(reg, kind: str, goal_id: str, work_id: str, ts: str,
           **extra) -> None:
    detail = {"goal_id": goal_id, "work_id": work_id, "ts": ts}
    detail.update(extra)
    reg.append_scheduler_event(AuditEvent(kind=kind, ts=ts, detail=detail))


def test_forged_ceiling_events_change_nothing(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    for i in range(5):
        _forge(reg, "goal_ceiling_changed", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), ceiling=99, outcome="set")
        _forge(reg, "goal_ceiling_changed", "goal-evil", f"sw-fake-{i}",
               _iso_plus(T0, i), ceiling=99, outcome="set")
        _forge(reg, "ceiling.denied", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), running=99, ceiling=99,
               reason="goal_ceiling")
    assert reg.get_goal_ceiling("goal-a") == 2
    assert reg.get_goal_ceiling("goal-evil") is None
    assert len(reg.list_goal_ceilings()) == 1
    reg.close()


def test_forged_events_cannot_exceed_ceiling(db_path: str):
    """Even with forged 'claimed'/'running' events and forged ceiling
    denials, the goal cannot exceed its real ceiling (authority rows)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 5)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    for i in range(5):
        _forge(reg, "work.claimed", "goal-a", rows[i].work_id,
               _iso_plus(T0, i), worker_id="w-forged", outcome="claimed")
        _forge(reg, "ceiling.denied", "goal-a", rows[i].work_id,
               _iso_plus(T0, i), running=1, ceiling=99, reason="goal_ceiling")
    assert _claim(reg, rows[2]) is False
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_metadata_and_planner_output_cannot_set_ceiling(db_path: str):
    """Engine-level work metadata / planner-like fields never touch the
    ceiling registry."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    row = reg.create(task_id="t-1", goal_id="goal-a", step_index=0,
                     scheduler_id="sched-1", now=T0)
    reg.mark_running(row.work_id, "w", 60.0, now=T0)
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 1))
    assert reg.list_goal_ceilings() == []
    assert reg.get_goal_ceiling("goal-a") is None
    reg.close()


def test_ceiling_cannot_establish_ownership(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 4)
    rows = _rows(reg, "goal-a", 2)
    assert _running_for(reg, "goal-a") == 0  # config alone owns nothing
    got = reg.claim(rows[0].work_id, "w-real", 60.0, T0, 600.0,
                    scheduler_id="sched-1")
    assert got is not None
    assert reg.get_work(rows[0].work_id).worker_id == "w-real"
    # forged claims for the other row create no ownership
    _forge(reg, "work.claimed", "goal-a", rows[1].work_id, T0,
           worker_id="w-forged", outcome="claimed")
    assert reg.get_work(rows[1].work_id).status == \
        SchedulerWorkStatus.QUEUED
    assert reg.get_work(rows[1].work_id).worker_id is None
    reg.close()


def test_other_goal_cannot_consume_or_transfer_ceiling(db_path: str):
    """B's ceiling never constrains A and A cannot borrow B's ceiling."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)  # enough credit to fill the cap
    reg.set_goal_ceiling("goal-b", 1)
    a_rows = _rows(reg, "goal-a", 6)
    b_rows = _rows(reg, "goal-b", 3, start=100)
    for r in a_rows:
        if not _claim(reg, r):
            break
    assert _running_for(reg, "goal-a") == 6  # A ignores B's ceiling
    assert _claim(reg, b_rows[0])
    assert _claim(reg, b_rows[1]) is False  # B bound by ITS ceiling
    reg.close()


def test_floor_cannot_bypass_ceiling(db_path: str):
    """A below-floor goal at its ceiling is unconstructible via config,
    but the gate order still protects the ceiling: a goal whose floor
    was validly configured with R <= C never exceeds C."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_ceiling("goal-a", 2)  # R == C: floor == ceiling
    rows = _rows(reg, "goal-a", 6)
    claimed = 0
    for r in rows:
        if not _claim(reg, r):
            break
        claimed += 1
        assert _running_for(reg, "goal-a") <= 2  # ceiling holds even via floor
    assert claimed == 2
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_dwrr_cannot_bypass_ceiling(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 10000)  # maximal weight + credit
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 5)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    # forge massive refill telemetry; the ceiling still binds
    for i in range(5):
        _forge(reg, "goal_weight.refill", "goal-a", f"sw-fake-{i}",
               _iso_plus(T0, i), weight=10000, credit_before=0,
               credit_after=10000, refill=True)
    assert _claim(reg, rows[2]) is False
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_fair_share_cannot_bypass_ceiling(db_path: str):
    """Two schedulers: each process' claims are capped by the ceiling
    across processes (already proven cross-process); here: within one
    scheduler, fair share never relaxes the ceiling."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0], worker="w1", scheduler_id="sched-a")
    assert _claim(reg, rows[1], worker="w2", scheduler_id="sched-a")
    assert _claim(reg, rows[2], worker="w3", scheduler_id="sched-a") is False
    assert _running_for(reg, "goal-a") == 2
    reg.close()


def test_global_cap_remains_authoritative(db_path: str):
    """Ceilings above the cap never let a goal exceed the cap."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_ceiling("goal-a", 100)  # ceiling never binds
    rows = _rows(reg, "goal-a", 8)
    claimed = 0
    for r in rows:
        if not _claim(reg, r):
            break
        claimed += 1
    assert claimed == 4  # the cap binds, not the ceiling
    assert _running_for(reg, "goal-a") == 4
    reg.close()


def test_deleting_telemetry_does_not_change_ceiling_authority(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 1)
    rows = _rows(reg, "goal-a", 3)
    assert _claim(reg, rows[0])
    assert _claim(reg, rows[1]) is False  # ceiling denial event recorded
    reg.prune_scheduler_events(
        cutoff=(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(days=1)).isoformat())
    assert reg.scheduler_event_count() == 0
    assert _claim(reg, rows[1]) is False  # still bound after the wipe
    assert reg.get_goal_ceiling("goal-a") == 1
    reg.close()


def test_stale_telemetry_cannot_resurrect_ceiling_slots(db_path: str):
    """Completed work with forged running telemetry does not occupy
    ceiling slots (authority rows only)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 2)
    rows = _rows(reg, "goal-a", 4)
    assert _claim(reg, rows[0]) and _claim(reg, rows[1])
    reg.mark_terminal(rows[0].work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 1))
    for i in range(3):
        _forge(reg, "work.heartbeat", "goal-a", rows[0].work_id,
               _iso_plus(T0, 2 + i), lease_expires_at="2099-01-01T00:00:00+00:00")
        _forge(reg, "work.claimed", "goal-a", rows[0].work_id,
               _iso_plus(T0, 2 + i), worker_id="w", outcome="claimed")
    assert _claim(reg, rows[2])  # the freed slot is claimable
    assert _running_for(reg, "goal-a") == 2
    assert _claim(reg, rows[3]) is False  # ceiling still binds at 2
    reg.close()
