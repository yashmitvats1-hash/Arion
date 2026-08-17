"""Per-goal concurrency ceilings (ADR-031, Phase A) - tests first.

Data model / validation:

- unconfigured goal -> ceiling None (unbounded; never an invented int);
- set/get/list config with enabled flag + actor metadata;
- fail closed: 0, negative, float, bool, oversized, empty goal id;
- remove/disable returns the goal to unbounded;
- durability across reopen;
- floor/ceiling pair validation (R <= C) is atomic in ALL directions;
- sum(ceilings) does NOT need to fit the global cap;
- every config change emits goal_ceiling_changed atomically.
"""

from __future__ import annotations

import pytest

from arion.state.scheduler_work import SchedulerRegistryError
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


# --------------------------------------------------------------------------- #
# defaults + basic config
# --------------------------------------------------------------------------- #


def test_unconfigured_goal_is_unbounded(db_path: str):
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_ceiling("goal-a") is None
    assert reg.get_goal_ceiling_config("goal-a") is None
    assert reg.list_goal_ceilings() == []
    reg.close()


def test_set_get_list_ceiling(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_ceiling("goal-a", 5, by="tester", now=T0)
    assert reg.get_goal_ceiling("goal-a") == 5
    cfg = reg.get_goal_ceiling_config("goal-a")
    assert cfg == {
        "goal_id": "goal-a",
        "ceiling": 5,
        "enabled": True,
        "updated_at": T0,
        "updated_by": "tester",
    }
    rows = reg.list_goal_ceilings()
    assert len(rows) == 1 and rows[0]["goal_id"] == "goal-a"
    reg.close()


def test_ceiling_validation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    for bad in (0, -1, 1.5, True, "5", None, 10**9):
        with pytest.raises(SchedulerRegistryError):
            reg.set_goal_ceiling("goal-a", bad)  # type: ignore[arg-type]
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_ceiling("", 2)
    assert reg.get_goal_ceiling("goal-a") is None
    reg.close()


def test_remove_and_disable_return_to_unbounded(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_ceiling("goal-a", 5, now=T0)
    cfg = reg.set_goal_ceiling_enabled("goal-a", False)
    assert cfg is not None and cfg["enabled"] is False
    # a disabled ceiling does not bind, but the value is kept
    assert reg.get_goal_ceiling("goal-a") == 5
    cfg = reg.set_goal_ceiling_enabled("goal-a", True)
    assert cfg["enabled"] is True
    assert reg.remove_goal_ceiling("goal-a") is True
    assert reg.get_goal_ceiling("goal-a") is None  # unbounded again
    assert reg.remove_goal_ceiling("goal-a") is False  # idempotent
    reg.close()


def test_ceiling_survives_reopen(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_ceiling("goal-a", 3, by="tester", now=T0)
    reg.set_goal_ceiling("goal-b", 1, enabled=False, now=_iso_plus(T0, 1))
    reg.close()

    reg2 = SQLiteStorage(db_path)
    assert reg2.get_goal_ceiling("goal-a") == 3
    assert reg2.get_goal_ceiling_config("goal-a")["updated_by"] == "tester"
    assert reg2.get_goal_ceiling_config("goal-b")["enabled"] is False
    assert reg2.get_goal_ceiling("goal-b") == 1
    reg2.close()


# --------------------------------------------------------------------------- #
# floor + ceiling composition (R <= C, atomic in all directions)
# --------------------------------------------------------------------------- #


def test_ceiling_below_floor_rejected(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 4)
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_ceiling("goal-a", 3)  # R=4 > C=3
    assert reg.get_goal_ceiling_config("goal-a") is None  # no partial write
    assert reg.get_goal_reservation("goal-a") == 4  # floor untouched
    reg.close()


def test_floor_above_ceiling_rejected(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_ceiling("goal-a", 3)
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_reservation("goal-a", 4)  # R=4 > C=3
    assert reg.get_goal_reservation_config("goal-a") is None
    assert reg.get_goal_ceiling("goal-a") == 3
    reg.close()


def test_enable_directions_validate_pair(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 4, enabled=False)
    reg.set_goal_ceiling("goal-a", 3, enabled=False)
    # enabling the ceiling with a disabled floor is fine...
    reg.set_goal_ceiling_enabled("goal-a", True)
    # ...but enabling the floor would violate R <= C -> fail closed
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_reservation_enabled("goal-a", True)
    assert reg.get_goal_reservation_config("goal-a")["enabled"] is False
    # raising the ceiling first makes the pair valid
    reg.set_goal_ceiling("goal-a", 5)
    reg.set_goal_reservation_enabled("goal-a", True)
    assert reg.get_goal_reservation_config("goal-a")["enabled"] is True
    # setting an enabled ceiling below an enabled floor fails closed
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_ceiling("goal-a", 3)  # C=3 < R=4
    # a DISABLED ceiling may be written below the floor, but ENABLING it
    # re-validates the pair and fails closed
    reg.set_goal_ceiling_enabled("goal-a", False)
    reg.set_goal_ceiling("goal-a", 3, enabled=False)
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_ceiling_enabled("goal-a", True)
    assert reg.get_goal_ceiling_config("goal-a")["enabled"] is False
    reg.close()


def test_valid_pairs_accepted(db_path: str):
    reg = SQLiteStorage(db_path)
    for r, c in ((2, 5), (5, 5), (0, 3), (1, 1)):
        reg.set_goal_reservation(f"goal-{r}-{c}", r)
        reg.set_goal_ceiling(f"goal-{r}-{c}", c)
    assert reg.get_goal_reservation("goal-2-5") == 2
    assert reg.get_goal_ceiling("goal-2-5") == 5
    reg.close()


def test_sum_of_ceilings_need_not_fit_global_cap(db_path: str):
    """Ceilings are maximums, not reservations: cap 8 with A ceiling 8 +
    B ceiling 8 is valid (floors still must sum <= cap)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_ceiling("goal-a", 8)
    reg.set_goal_ceiling("goal-b", 8)
    assert reg.get_goal_ceiling("goal-a") == 8
    assert reg.get_goal_ceiling("goal-b") == 8
    # floors still bounded by the cap
    reg.set_goal_reservation("goal-a", 4)
    reg.set_goal_reservation("goal-b", 3)
    with pytest.raises(SchedulerRegistryError):
        reg.set_goal_reservation("goal-c", 2)  # floors 9 > 8
    reg.close()


# --------------------------------------------------------------------------- #
# durability + telemetry
# --------------------------------------------------------------------------- #


def test_config_change_emits_goal_ceiling_changed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_ceiling("goal-a", 2, now=T0)
    events = [e for e in reg.scheduler_events(
        event_type="goal_ceiling_changed")]
    assert len(events) == 1
    assert events[0].detail["goal_id"] == "goal-a"
    assert events[0].detail["config"] == "goal_ceiling"
    assert events[0].detail["outcome"] == "set"
    reg.remove_goal_ceiling("goal-a")
    events = [e for e in reg.scheduler_events(
        event_type="goal_ceiling_changed")]
    assert len(events) == 2 and events[-1].detail["outcome"] == "removed"
    reg.close()


def test_failed_config_write_emits_no_event(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 4)
    try:
        reg.set_goal_ceiling("goal-a", 2)  # R > C: rejected
    except SchedulerRegistryError:
        pass
    events = [e for e in reg.scheduler_events(
        event_type="goal_ceiling_changed")]
    assert len(events) == 0  # no event for the rolled-back write
    reg.close()
