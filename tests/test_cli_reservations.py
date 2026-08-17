"""Reservation CLI (ADR-029, Phase G) - tests first.

- `arion scheduler reservations` (human + --json);
- `reservation set|remove|enable|disable` with bounded validation,
  deterministic errors, persistence across restart;
- planner/model/task metadata can never modify reservations (the CLI is
  the only configuration path besides the store API; there is no
  metadata path at all - proven by the adversarial suite).
"""

from __future__ import annotations

import json

from arion.interfaces.cli import main as cli_main
from arion.state.store import SQLiteStorage


def _cli(argv: list[str]) -> int:
    """Run the CLI in-process (like the ADR-028 CLI tests)."""
    return cli_main(argv)


def test_reservations_list_empty_and_json(db_path: str, capsys):
    rc = _cli(["scheduler", "reservations", "--db", db_path])
    assert rc == 0
    assert "no goal reservations configured" in capsys.readouterr().out
    rc = _cli(["scheduler", "reservations", "--json", "--db", db_path])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
    SQLiteStorage(db_path).close()


def test_reservation_set_and_list(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.close()
    rc = _cli(["scheduler", "reservation", "set", "goal-b", "2",
               "--by", "cli-tester", "--db", db_path])
    assert rc == 0
    assert "reservation=2" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    cfg = reg.get_goal_reservation_config("goal-b")
    assert cfg["reservation"] == 2 and cfg["updated_by"] == "cli-tester"
    reg.close()
    rc = _cli(["scheduler", "reservations", "--db", db_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "goal-b" in out and "reservation=2" in out and "reserved_capacity=2" in out


def test_reservation_set_json(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.close()
    rc = _cli(["scheduler", "reservation", "set", "goal-b", "3",
               "--json", "--db", db_path])
    assert rc == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["goal_id"] == "goal-b" and cfg["reservation"] == 3
    SQLiteStorage(db_path).close()


def test_reservation_set_validation_fails_closed(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.close()
    for bad in ("-1", "999999999"):
        rc = _cli(["scheduler", "reservation", "set", "goal-b", bad,
                   "--db", db_path])
        assert rc == 1, bad
        assert "invalid reservation config" in capsys.readouterr().out
    # oversubscription via CLI is rejected deterministically
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 3)
    reg.close()
    rc = _cli(["scheduler", "reservation", "set", "goal-b", "2",
               "--db", db_path])
    assert rc == 1
    assert "oversubscription" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_reservation_config("goal-b") is None
    reg.close()


def test_reservation_enable_disable_remove(db_path: str, capsys):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    reg.close()
    rc = _cli(["scheduler", "reservation", "disable", "goal-b", "--db", db_path])
    assert rc == 0 and "disabled" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_reservation_config("goal-b")["enabled"] is False
    reg.close()
    rc = _cli(["scheduler", "reservation", "enable", "goal-b", "--db", db_path])
    assert rc == 0 and "enabled" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_reservation_config("goal-b")["enabled"] is True
    reg.close()
    rc = _cli(["scheduler", "reservation", "remove", "goal-b", "--db", db_path])
    assert rc == 0 and "removed" in capsys.readouterr().out
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_reservation_config("goal-b") is None
    reg.close()
    # missing config: deterministic error, rc 1
    rc = _cli(["scheduler", "reservation", "remove", "goal-b", "--db", db_path])
    assert rc == 1 and "no reservation config" in capsys.readouterr().out
    rc = _cli(["scheduler", "reservation", "disable", "goal-b", "--db", db_path])
    assert rc == 1 and "no reservation config" in capsys.readouterr().out


def test_reservation_config_persists_across_cli_invocations(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.close()
    assert _cli(["scheduler", "reservation", "set", "goal-b", "2",
                 "--db", db_path]) == 0
    # a fresh CLI invocation (fresh engine + store) sees the config
    rc = _cli(["scheduler", "reservations", "--json", "--db", db_path])
    assert rc == 0
    rows = SQLiteStorage(db_path).list_goal_reservations()
    assert len(rows) == 1 and rows[0]["reservation"] == 2
    SQLiteStorage(db_path).close()


def test_reservation_cannot_be_set_through_work_metadata(db_path: str):
    """There is NO metadata path to reservations: creating work with any
    goal/task metadata never touches scheduler_goal_reservations."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    row = reg.create(task_id="t-1", goal_id="goal-b", step_index=0,
                     scheduler_id="sched-1", now="2026-01-01T00:00:00+00:00")
    assert reg.get_goal_reservation("goal-b") == 0
    assert reg.list_goal_reservations() == []
    reg.mark_running(row.work_id, "w", 60.0,
                     now="2026-01-01T00:00:00+00:00")
    assert reg.list_goal_reservations() == []
    reg.close()
