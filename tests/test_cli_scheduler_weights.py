"""CLI per-goal weight commands (ADR-027, Phase F).

`arion scheduler weights` (list), `scheduler weight set <goal> <w>`,
`scheduler weight remove <goal>`, `scheduler weight enable|disable <goal>`:

- validation (invalid weights / malformed input fail closed, exit 1);
- persistence (durable across engine restarts);
- bounded, secret-free output; multiple goals; --json.
"""

from __future__ import annotations

import json

from arion.interfaces.cli import main as cli_main

from tests.test_cross_goal_concurrency import _env, TwoStepPlanner, _read_step


def _seed(tmp_path, db_name="cli.db"):
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name=db_name)
    return env


def test_cli_weights_list_empty(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "weights", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no goal weights configured" in out
    rc2 = cli_main(["scheduler", "weights", "--json", "--db", db])
    assert json.loads(capsys.readouterr().out) == []
    env.engine.storage.close()


def test_cli_weight_set_persists_and_lists(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "weight", "set", "goal-a", "2", "--db", db])
    assert rc == 0
    assert "weight=2" in capsys.readouterr().out
    rc = cli_main(["scheduler", "weight", "set", "goal-b", "5",
                   "--by", "user:ops", "--db", db])
    assert rc == 0
    rc = cli_main(["scheduler", "weights", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "goal-a" in out and "weight=2" in out
    assert "goal-b" in out and "weight=5" in out
    assert "user:ops" in out
    # durable: a fresh engine on the same db sees the config
    env2 = _seed(tmp_path, db_name="cli.db")
    assert env2.engine.scheduler_registry.get_goal_weight("goal-a") == 2
    assert env2.engine.scheduler_registry.get_goal_weight("goal-b") == 5
    env2.engine.storage.close()
    env.engine.storage.close()


def test_cli_weight_set_validation_fails_closed(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    # non-integer input fails at parse time (exit 2); out-of-range weights
    # fail in the store (exit 1) - both fail closed
    for bad in ("0", "-3"):
        rc = cli_main(["scheduler", "weight", "set", "goal-a", bad, "--db", db])
        assert rc == 1, bad
        assert "invalid weight config" in capsys.readouterr().out
    for bad in ("abc", "1.5"):
        try:
            cli_main(["scheduler", "weight", "set", "goal-a", bad, "--db", db])
            assert False, f"argparse should have rejected {bad}"
        except SystemExit as exc:
            assert exc.code != 0
    # nothing was persisted
    assert env.engine.scheduler_registry.get_goal_weight("goal-a") == 1
    env.engine.storage.close()


def test_cli_weight_remove_and_enable_disable(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    cli_main(["scheduler", "weight", "set", "goal-a", "3", "--db", db])
    capsys.readouterr()
    # remove -> default restored
    rc = cli_main(["scheduler", "weight", "remove", "goal-a", "--db", db])
    assert rc == 0
    assert "default 1 restored" in capsys.readouterr().out
    assert env.engine.scheduler_registry.get_goal_weight("goal-a") == 1
    # remove again -> fail closed (no config)
    rc = cli_main(["scheduler", "weight", "remove", "goal-a", "--db", db])
    assert rc == 1
    # set + disable
    cli_main(["scheduler", "weight", "set", "goal-a", "4", "--db", db])
    capsys.readouterr()
    rc = cli_main(["scheduler", "weight", "disable", "goal-a", "--db", db])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out
    assert env.engine.scheduler_registry.get_goal_weight_config(
        "goal-a")["enabled"] is False
    rc = cli_main(["scheduler", "weight", "enable", "goal-a", "--db", db])
    assert rc == 0
    assert env.engine.scheduler_registry.get_goal_weight_config(
        "goal-a")["enabled"] is True
    # enable/disable on an unconfigured goal fails closed
    rc = cli_main(["scheduler", "weight", "disable", "goal-ghost", "--db", db])
    assert rc == 1
    assert "no weight config" in capsys.readouterr().out
    env.engine.storage.close()


def test_cli_weight_set_disable_flag(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "weight", "set", "goal-a", "2",
                   "--disable", "--db", db])
    assert rc == 0
    cfg = env.engine.scheduler_registry.get_goal_weight_config("goal-a")
    assert cfg["enabled"] is False and cfg["weight"] == 2
    env.engine.storage.close()


def test_cli_weights_json_output_bounded(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    cli_main(["scheduler", "weight", "set", "goal-x", "7", "--db", db])
    capsys.readouterr()
    rc = cli_main(["scheduler", "weights", "--json", "--db", db])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"goal_id", "weight", "enabled",
                                   "updated_at", "updated_by"}
    assert rows[0]["weight"] == 7
    env.engine.storage.close()
