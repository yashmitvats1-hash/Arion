"""Ceiling CLI (ADR-031, Phase J) - tests first.

- `arion scheduler ceilings` (human + --json);
- `ceiling set|remove|enable|disable` with bounded validation and
  deterministic errors;
- `ceiling plan <goal> <n>` dry-run: provably mutation-free;
- `scheduler status` shows ceiling info; `reservations --check` reports
  goals at ceiling; `scheduler watch` renders ceiling events.
"""

from __future__ import annotations

import json

from arion.interfaces.cli import main as cli_main
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _seed(db_path: str) -> None:
    from datetime import datetime, timezone
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_ceiling("goal-b", 3, by="seed")
    real_now = datetime.now(timezone.utc).isoformat()
    row = reg.create(task_id="t-1", goal_id="goal-b", step_index=0,
                     scheduler_id="sched-1", now=real_now)
    reg.claim(row.work_id, "w", 60.0, real_now, 600.0,
              scheduler_id="sched-1")
    reg.close()


def test_ceilings_list_empty_and_json(db_path: str, capsys):
    SQLiteStorage(db_path).close()
    rc = cli_main(["scheduler", "ceilings", "--db", db_path])
    assert rc == 0
    assert "no goal ceilings configured" in capsys.readouterr().out
    rc = cli_main(["scheduler", "ceilings", "--json", "--db", db_path])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
    SQLiteStorage(db_path).close()


def test_ceiling_set_list_remove(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.close()
    rc = cli_main(["scheduler", "ceiling", "set", "goal-a", "5",
                   "--by", "cli-tester", "--db", db_path])
    assert rc == 0 and "ceiling=5" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    cfg = reg.get_goal_ceiling_config("goal-a")
    assert cfg["ceiling"] == 5 and cfg["updated_by"] == "cli-tester"
    reg.close()
    rc = cli_main(["scheduler", "ceilings", "--db", db_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "goal-a" in out and "ceiling=5" in out
    rc = cli_main(["scheduler", "ceiling", "remove", "goal-a", "--db", db_path])
    assert rc == 0 and "unbounded" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_ceiling("goal-a") is None
    reg.close()


def test_ceiling_set_validation_fails_closed(db_path: str, capsys):
    SQLiteStorage(db_path).close()
    for bad in ("0", "-1", "999999999"):
        rc = cli_main(["scheduler", "ceiling", "set", "goal-a", bad,
                       "--db", db_path])
        assert rc == 1, bad
        assert "invalid ceiling config" in capsys.readouterr().out
    SQLiteStorage(db_path).close()


def test_ceiling_enable_disable(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 4)
    reg.close()
    rc = cli_main(["scheduler", "ceiling", "disable", "goal-a", "--db", db_path])
    assert rc == 0 and "disabled" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_ceiling_config("goal-a")["enabled"] is False
    reg.close()
    rc = cli_main(["scheduler", "ceiling", "enable", "goal-a", "--db", db_path])
    assert rc == 0 and "enabled" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_ceiling_config("goal-a")["enabled"] is True
    reg.close()
    rc = cli_main(["scheduler", "ceiling", "disable", "goal-none", "--db",
                   db_path])
    assert rc == 1 and "no ceiling config" in capsys.readouterr().out


def test_ceiling_plan_dry_run_no_mutation(db_path: str, capsys):
    _seed(db_path)
    reg = SQLiteStorage(db_path)
    before = {
        "ceilings": reg.list_goal_ceilings(),
        "reservations": reg.list_goal_reservations(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
    }
    reg.close()
    rc = cli_main(["scheduler", "ceiling", "plan", "goal-b", "5",
                   "--db", db_path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ceiling 3 -> 5" in out and "floor<=ceiling=yes" in out
    rc = cli_main(["scheduler", "ceiling", "plan", "goal-b", "5",
                   "--json", "--db", db_path])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["current_ceiling"] == 3 and data["proposed_ceiling"] == 5
    assert data["headroom_delta"] == "increase"
    # an invalid pair (ceiling below the floor) is reported, rc 0
    rc = cli_main(["scheduler", "ceiling", "plan", "goal-b", "1",
                   "--db", db_path])
    assert rc == 0 and "floor<=ceiling=no" in capsys.readouterr().out
    # invalid input fails closed
    rc = cli_main(["scheduler", "ceiling", "plan", "goal-b", "0",
                   "--db", db_path])
    assert rc == 1 and "invalid ceiling plan" in capsys.readouterr().out
    # nothing was persisted by any of the planning calls
    reg = SQLiteStorage(db_path)
    after = {
        "ceilings": reg.list_goal_ceilings(),
        "reservations": reg.list_goal_reservations(),
        "credit": dict(reg._conn.execute(
            "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall()),
        "events": reg.scheduler_event_count(),
    }
    assert after == before
    reg.close()


def test_status_and_check_expose_ceiling_info(db_path: str, capsys):
    _seed(db_path)
    rc = cli_main(["scheduler", "status", "--json", "--db", db_path])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    goals = {g["goal_id"]: g for g in out["goals"]}
    assert goals["goal-b"]["ceiling"] == 3
    assert goals["goal-b"]["ceiling_headroom"] == 2  # 3 - 1 running
    assert out["ceiling_limited_goal_count"] == 1
    assert out["goals_at_ceiling"] == []
    rc = cli_main(["scheduler", "status", "--db", db_path])
    assert rc == 0 and "ceiling=3" in capsys.readouterr().out
    rc = cli_main(["scheduler", "reservations", "--check", "--json",
                   "--db", db_path])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and "goals_at_ceiling" in data
    SQLiteStorage(db_path).close()


def test_watch_renders_ceiling_events(db_path: str, capsys):
    _seed(db_path)
    rc = cli_main(["scheduler", "watch", "--type", "goal_ceiling_changed",
                   "--db", db_path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "goal_ceiling_changed" in out and "ceiling=3" in out
    SQLiteStorage(db_path).close()
