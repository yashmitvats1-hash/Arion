"""Durable per-goal scheduling weight registry (ADR-027, Phase A) - tests first.

- set/get/remove/list goal weights; deterministic default weight = 1;
- positive bounded weights only (0/negative/non-integer/oversized -> typed
  error, fail closed);
- enabled/disabled configuration (a disabled goal is never admitted);
- durable persistence across a store reopen;
- concurrent configuration access does not corrupt rows;
- goal isolation (one goal's config never leaks into another's).
"""

from __future__ import annotations

import threading

import pytest

from arion.state.scheduler_work import SchedulerRegistryError
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _mk(reg, goal_id="goal-1", task_id="t1", scheduler_id="sched-1", now=T0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=0,
                      scheduler_id=scheduler_id, now=now)


# --------------------------------------------------------------------------- #
# basic registry
# --------------------------------------------------------------------------- #


def test_default_weight_is_one(db_path: str):
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_weight("goal-none") == 1
    assert reg.get_goal_weight_config("goal-none") is None
    reg.close()


def test_set_and_get_weight(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 2, by="operator-1", now=T0)
    assert reg.get_goal_weight("goal-a") == 2
    cfg = reg.get_goal_weight_config("goal-a")
    assert cfg["weight"] == 2 and cfg["enabled"] is True
    assert cfg["updated_by"] == "operator-1"
    # other goals unaffected (isolation)
    assert reg.get_goal_weight("goal-b") == 1
    reg.close()


def test_update_weight(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 2, by="op-1", now=T0)
    reg.set_goal_weight("goal-a", 5, by="op-2", now=T0)
    assert reg.get_goal_weight("goal-a") == 5
    assert reg.get_goal_weight_config("goal-a")["updated_by"] == "op-2"
    reg.close()


def test_remove_weight_returns_to_default(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 3, by="op-1", now=T0)
    assert reg.remove_goal_weight("goal-a") is True
    assert reg.get_goal_weight("goal-a") == 1
    assert reg.get_goal_weight_config("goal-a") is None
    # idempotent remove
    assert reg.remove_goal_weight("goal-a") is False
    reg.close()


def test_list_goal_weights(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-b", 1, by="op", now=T0)
    reg.set_goal_weight("goal-a", 4, by="op", now=T0)
    reg.set_goal_weight("goal-c", 2, by="op", now=T0, enabled=False)
    rows = reg.list_goal_weights()
    assert [r["goal_id"] for r in rows] == ["goal-a", "goal-b", "goal-c"]
    by_id = {r["goal_id"]: r for r in rows}
    assert by_id["goal-a"]["weight"] == 4 and by_id["goal-a"]["enabled"] is True
    assert by_id["goal-c"]["enabled"] is False
    # bounded output: only configured goals, no engine objects
    for r in rows:
        assert set(r.keys()) == {"goal_id", "weight", "enabled",
                                 "updated_at", "updated_by"}
    reg.close()


# --------------------------------------------------------------------------- #
# validation (fail closed)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, -1, -100, 1.5, "x", None, "", 10001])
def test_invalid_weights_rejected(db_path: str, bad):
    reg = SQLiteStorage(db_path)
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_weight("goal-a", bad, by="op", now=T0)  # type: ignore[arg-type]
    # nothing was written
    assert reg.get_goal_weight_config("goal-a") is None
    assert reg.get_goal_weight("goal-a") == 1
    reg.close()


def test_weight_bound_is_bounded(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 10000, by="op", now=T0)  # max allowed
    assert reg.get_goal_weight("goal-a") == 10000
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_weight("goal-a", 10001, by="op", now=T0)
    reg.close()


def test_unknown_goal_operations_fail_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    # setting a weight for an unknown goal is fine (config is per goal_id);
    # but enabling/disabling an unknown goal returns None (no config)
    assert reg.set_goal_weight_enabled("goal-ghost", False) is None
    assert reg.get_goal_weight_config("goal-ghost") is None
    reg.close()


# --------------------------------------------------------------------------- #
# enabled / disabled
# --------------------------------------------------------------------------- #


def test_disable_and_re_enable(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 2, by="op", now=T0)
    cfg = reg.set_goal_weight_enabled("goal-a", False)
    assert cfg is not None and cfg["enabled"] is False
    assert reg.get_goal_weight_config("goal-a")["enabled"] is False
    # weight is retained while disabled
    assert reg.get_goal_weight("goal-a") == 2
    cfg2 = reg.set_goal_weight_enabled("goal-a", True)
    assert cfg2["enabled"] is True
    reg.close()


def test_disabled_goal_weight_config_marked(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 3, by="op", now=T0, enabled=False)
    cfg = reg.get_goal_weight_config("goal-a")
    assert cfg["weight"] == 3 and cfg["enabled"] is False
    reg.close()


# --------------------------------------------------------------------------- #
# durability
# --------------------------------------------------------------------------- #


def test_weights_survive_reopen(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 2, by="op", now=T0)
    reg.set_goal_weight("goal-b", 5, by="op", now=T0, enabled=False)
    reg.close()

    again = SQLiteStorage(db_path)
    assert again.get_goal_weight("goal-a") == 2
    assert again.get_goal_weight("goal-b") == 5
    assert again.get_goal_weight_config("goal-b")["enabled"] is False
    assert again.get_goal_weight("goal-unset") == 1
    again.close()


# --------------------------------------------------------------------------- #
# concurrent configuration access
# --------------------------------------------------------------------------- #


def test_concurrent_weight_configuration(db_path: str):
    """Concurrent set/get on many goals: every read returns one of the
    written values (never a torn/corrupt row)."""
    reg = SQLiteStorage(db_path)
    errors: list[Exception] = []
    done = threading.Barrier(4)

    def worker(goal, weight):
        try:
            for _ in range(20):
                reg.set_goal_weight(goal, weight, by="w", now=T0)
                got = reg.get_goal_weight(goal)
                assert got == weight, (goal, got, weight)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            done.wait(timeout=10)

    threads = [threading.Thread(target=worker, args=(f"goal-{i}", i + 1))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    reg.close()


def test_goal_isolation_under_weights(db_path: str):
    """One goal's weight never affects another's (each keyed by goal_id)."""
    reg = SQLiteStorage(db_path)
    for gid, w in (("goal-a", 2), ("goal-b", 7), ("goal-c", 1)):
        reg.set_goal_weight(gid, w, by="op", now=T0)
    assert [reg.get_goal_weight(f"goal-{c}") for c in "abc"] == [2, 7, 1]
    reg.remove_goal_weight("goal-b")
    assert reg.get_goal_weight("goal-a") == 2
    assert reg.get_goal_weight("goal-b") == 1
    assert reg.get_goal_weight("goal-c") == 1
    reg.close()
