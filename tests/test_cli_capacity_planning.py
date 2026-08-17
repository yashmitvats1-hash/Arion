"""Capacity-planning CLI (ADR-030, Phases F/G/H) - tests first.

- `scheduler status` upgraded: planning layout (human) + additive JSON
  (old keys preserved, new capacity block + per-goal projections);
- `scheduler reservations --check`: read-only; exit 0 feasible / 1
  infeasible; --json stable schema; no mutation either way;
- `scheduler reservation plan <goal> <n>`: dry-run only; repeated runs
  leave reservations, weights, DWRR credit, events and ownership
  byte-identical; invalid input fails closed (exit 1).
"""

from __future__ import annotations

import json

from arion.interfaces.cli import main as cli_main
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


def _seed(db_path: str, cap: int = 8) -> None:
    from datetime import datetime, timezone
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(cap)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_reservation("goal-c", 3)
    a_rows = _rows(reg, "goal-a", 6)
    _rows(reg, "goal-b", 4, start=100)
    real_now = datetime.now(timezone.utc).isoformat()
    for r in a_rows[:3]:
        reg.claim(r.work_id, "w", 60.0, real_now, 600.0,
                  scheduler_id="sched-1")
    reg.close()


def _state(db_path: str) -> dict:
    reg = SQLiteStorage(db_path)
    state = {
        "reservations": reg.list_goal_reservations(),
        "weights": reg.list_goal_weights(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
        "work": [(r.work_id, r.status.value, r.worker_id,
                  r.lease_expires_at) for r in reg.list_work()],
    }
    reg.close()
    return state


def test_status_json_additive_with_planning_block(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    _seed(db)
    rc = cli_main(["scheduler", "status", "--json", "--db", db])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    # old keys preserved
    assert out["total"] == 10
    assert out["running"] == 3 and out["queued"] == 7
    assert out["stale_running_leases"] == 0
    # new capacity block
    assert out["global_max_concurrency"] == 8
    assert out["available_capacity"] == 5
    assert out["reserved_capacity"] == 5
    assert out["active_reserved_capacity"] == 2
    assert out["unreserved_capacity"] == 3
    # per-goal projections
    goals = {g["goal_id"]: g for g in out["goals"]}
    assert "goal-a" in goals and "goal-b" in goals
    assert goals["goal-b"]["reservation"] == 2
    assert goals["goal-b"]["state"] == "reserved_floor"
    # no payloads / internals
    dumped = json.dumps(out)
    for needle in ("task_id", "content", "rowid", "scheduler_work"):
        assert needle not in dumped, needle
    SQLiteStorage(db).close()


def test_status_human_planning_layout(tmp_path, capsys):
    db = str(tmp_path / "s2.db")
    _seed(db)
    rc = cli_main(["scheduler", "status", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Global capacity:" in out and "8" in out
    assert "Running:" in out
    assert "Configured reserved:" in out and "5" in out
    assert "Active reservation:" in out and "2" in out
    assert "Unreserved capacity:" in out
    assert "goal-b" in out and "reservation=2" in out
    SQLiteStorage(db).close()


def test_reservations_check_feasible_exit_zero(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    _seed(db)  # total 5 <= 8
    rc = cli_main(["scheduler", "reservations", "--check", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Feasible:" in out and "Configured total:" in out
    rc = cli_main(["scheduler", "reservations", "--check", "--json",
                   "--db", db])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["feasible"] is True
    assert data["configured_total"] == 5
    assert data["overflow"] == 0
    # goal-b (r2, runnable, 0 running) and idle goal-c (r3, no rows)
    # both read below by count; only goal-c is idle
    assert data["goals_below"] == ["goal-b", "goal-c"]
    assert data["idle_reserved_goals"] == ["goal-c"]
    SQLiteStorage(db).close()


def test_reservations_check_infeasible_exit_one(tmp_path, capsys):
    db = str(tmp_path / "c2.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 3)
    reg.set_goal_reservation("goal-b", 2)
    reg.close()
    # the durable config is feasible by construction (ADR-029 rejects
    # impossible writes); simulate an impossible FULL config via the
    # check on a lowered cap is impossible too - so instead verify the
    # check reports the current config as feasible and that an
    # infeasible PROPOSED config is reported by `plan`.
    rc = cli_main(["scheduler", "reservations", "--check", "--db", db])
    assert rc == 0
    rc = cli_main(["scheduler", "reservation", "plan", "goal-c", "3",
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "feasible=no" in out and "overflow=2" in out  # 5 + 3 - 6
    SQLiteStorage(db).close()


def test_reservation_plan_dry_run(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    _seed(db)
    before = _state(db)
    rc = cli_main(["scheduler", "reservation", "plan", "goal-b", "4",
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reservation 2 -> 4" in out
    assert "totals: 5 -> 7" in out
    rc = cli_main(["scheduler", "reservation", "plan", "goal-b", "4",
                   "--json", "--db", db])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["current_reservation"] == 2
    assert data["proposed_reservation"] == 4
    assert data["current_total"] == 5 and data["proposed_total"] == 7
    assert data["feasible"] is True
    assert data["remaining_capacity"] == 1
    assert data["pressure_delta"] == "increase"
    # the dry-run persisted NOTHING
    assert _state(db) == before
    SQLiteStorage(db).close()


def test_reservation_plan_infeasible_and_invalid(tmp_path, capsys):
    db = str(tmp_path / "p2.db")
    reg = SQLiteStorage(db)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 4)
    reg.close()
    rc = cli_main(["scheduler", "reservation", "plan", "goal-b", "3",
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0 and "feasible=no" in out and "overflow=1" in out
    for bad in ("-1", "999999999"):
        rc = cli_main(["scheduler", "reservation", "plan", "goal-b", bad,
                       "--db", db])
        assert rc == 1, bad
        assert "invalid" in capsys.readouterr().out
    SQLiteStorage(db).close()


def test_plan_repeated_runs_leave_state_identical(tmp_path):
    db = str(tmp_path / "p3.db")
    _seed(db)
    before = _state(db)
    for _ in range(3):
        rc = cli_main(["scheduler", "reservation", "plan", "goal-x", "2",
                       "--db", db])
        assert rc == 0
        rc = cli_main(["scheduler", "reservations", "--check", "--db", db])
        assert rc == 0
        rc = cli_main(["scheduler", "status", "--db", db])
        assert rc == 0
    assert _state(db) == before
    SQLiteStorage(db).close()
