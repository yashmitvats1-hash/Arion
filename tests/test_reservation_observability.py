"""Reservation observability (ADR-029, Phase F) - tests first.

- scheduler_status() exposes goal_reservations, reserved_capacity,
  reservation_satisfied, reservation_pressure (deterministic);
- status is a read-only observation (calling it mutates nothing);
- reservation.denied / reservation.satisfied / goal_reservation_changed
  events are emitted atomically with the transitions;
- `arion scheduler watch` shows the reservation kinds (human + JSON);
- telemetry stays observational: wiping events changes nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"

REPO = Path(__file__).resolve().parent.parent


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


def test_status_exposes_reservation_fields(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2, by="tester", now=T0)
    st = reg.scheduler_status(now=T0)
    assert st["goal_reservations"] == [{
        "goal_id": "goal-b", "reservation": 2, "enabled": True,
        "updated_at": T0, "updated_by": "tester"}]
    assert st["reserved_capacity"] == 2
    assert st["reservation_satisfied"] == {"goal-b": False}
    assert st["reservation_pressure"] == 0  # no runnable B work yet
    reg.close()


def test_status_pressure_and_satisfaction_deterministic(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 2, start=100)  # exactly the floor's worth of work
    # B runnable and below its floor: pressure = 2
    st = reg.scheduler_status(now=T0)
    assert st["reservation_pressure"] == 2
    assert st["reservation_satisfied"] == {"goal-b": False}
    # B claims its floor: satisfied, pressure 0
    _fill(reg, "goal-b")
    st = reg.scheduler_status(now=T0)
    assert st["reservation_satisfied"] == {"goal-b": True}
    assert st["reservation_pressure"] == 0
    # B's running work completes (its queue is now empty): idle -> no
    # pressure; satisfaction reports running >= reservation (False)
    for r in reg.list_work(status=SchedulerWorkStatus.RUNNING):
        reg.mark_terminal(r.work_id, SchedulerWorkStatus.COMPLETED,
                          owner_worker_id="w", now=_iso_plus(T0, 1))
    st = reg.scheduler_status(now=T0)
    assert st["reservation_pressure"] == 0  # idle goals reserve nothing
    assert st["reservation_satisfied"] == {"goal-b": False}
    reg.close()


def test_status_is_read_only_observation(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 2)
    before_events = reg.scheduler_event_count()
    before_credit = dict(reg.scheduler_status(now=T0)["dwr_credit"])
    st = reg.scheduler_status(now=T0)
    reg.scheduler_status(now=T0)  # repeated calls mutate nothing
    assert reg.scheduler_event_count() == before_events
    assert reg.scheduler_status(now=T0)["dwr_credit"] == before_credit
    assert st["reserved_capacity"] == 2
    reg.close()


def test_telemetry_kinds_emitted_atomically(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 2, start=100)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    events = reg.scheduler_events()
    kinds = [e.kind for e in events]
    assert "goal_reservation_changed" in kinds
    assert "reservation.satisfied" in kinds
    assert "reservation.denied" in kinds
    sat = [e for e in events if e.kind == "reservation.satisfied"]
    assert len(sat) == 1
    assert sat[0].detail["reservation"] == 2 and sat[0].detail["running"] == 2
    denied = [e for e in events if e.kind == "reservation.denied"]
    assert all(e.detail["reason"] == "reservation" for e in denied)
    assert all(e.detail["goal_id"] == "goal-a" for e in denied)
    reg.close()


def test_cli_watch_shows_reservation_kinds(tmp_path):
    db = str(tmp_path / "watch.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 2)
    _fill(reg, "goal-b")
    reg.close()

    proc = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler", "watch",
         "--json", "--db", str(db)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO))
    assert proc.returncode == 0
    rows = json.loads(proc.stdout)
    kinds = {r["kind"] for r in rows}
    assert "goal_reservation_changed" in kinds
    assert "reservation.satisfied" in kinds
    sat = [r for r in rows if r["kind"] == "reservation.satisfied"]
    assert sat and sat[0]["detail"]["reservation"] == 2

    # human mode renders the reservation extras
    proc = subprocess.run(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler", "watch",
         "--type", "reservation.satisfied", "--db", str(db)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO))
    assert proc.returncode == 0
    assert "reservation=2" in proc.stdout
    reg = SQLiteStorage(db)
    st = reg.scheduler_status()
    # the floor is still occupied (claimed rows RUNNING): satisfied
    assert st["reservation_satisfied"].get("goal-b") is True
    reg.close()
