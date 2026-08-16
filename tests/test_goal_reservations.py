"""Per-goal capacity reservations (ADR-029, Phase A) - tests first.

Data model / validation:

- unconfigured goal -> reservation 0 (deterministic default);
- set/get/list config with enabled flag + actor metadata;
- fail closed: negative, float, bool, oversized, empty goal id;
- oversubscription: total of enabled reservations may never exceed the
  global cap when a cap is configured (REJECT, never normalize);
- lowering the global cap below the reservation total is rejected;
- no global cap -> reservation accepted (unbounded capacity); the
  admission gate is a no-op until a cap exists (documented behavior);
- enable/disable/remove semantics;
- durability across a store reopen;
- every config change emits goal_reservation_changed atomically.
"""

from __future__ import annotations

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


def test_unconfigured_goal_defaults_to_zero(db_path: str):
    reg = SQLiteStorage(db_path)
    assert reg.get_goal_reservation("goal-a") == 0
    assert reg.get_goal_reservation_config("goal-a") is None
    assert reg.list_goal_reservations() == []
    reg.close()


def test_set_get_list_reservation(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-a", 2, by="tester", now=T0)
    assert reg.get_goal_reservation("goal-a") == 2
    cfg = reg.get_goal_reservation_config("goal-a")
    assert cfg == {
        "goal_id": "goal-a",
        "reservation": 2,
        "enabled": True,
        "updated_at": T0,
        "updated_by": "tester",
    }
    rows = reg.list_goal_reservations()
    assert len(rows) == 1 and rows[0]["goal_id"] == "goal-a"
    reg.close()


def test_reservation_zero_is_explicit_floor_zero(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_reservation("goal-a", 0, now=T0)
    assert reg.get_goal_reservation("goal-a") == 0
    assert reg.get_goal_reservation_config("goal-a")["enabled"] is True
    reg.close()


# --------------------------------------------------------------------------- #
# fail-closed validation
# --------------------------------------------------------------------------- #


def test_negative_reservation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    for bad in (-1, -100):
        try:
            reg.set_goal_reservation("goal-a", bad)
        except SchedulerRegistryError:
            pass
        else:
            raise AssertionError(f"accepted reservation {bad}")
    assert reg.get_goal_reservation("goal-a") == 0
    reg.close()


def test_non_integer_reservation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    for bad in (1.5, True, "2", None):
        try:
            reg.set_goal_reservation("goal-a", bad)  # type: ignore[arg-type]
        except SchedulerRegistryError:
            pass
        else:
            raise AssertionError(f"accepted reservation {bad!r}")
    assert reg.get_goal_reservation("goal-a") == 0
    reg.close()


def test_oversized_reservation_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    try:
        reg.set_goal_reservation("goal-a", 10**9)
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("accepted unbounded reservation")
    assert reg.get_goal_reservation("goal-a") == 0
    reg.close()


def test_empty_goal_id_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    try:
        reg.set_goal_reservation("", 1)
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("accepted empty goal id")
    reg.close()


# --------------------------------------------------------------------------- #
# oversubscription policy: REJECT at configuration time
# --------------------------------------------------------------------------- #


def test_reservation_cannot_exceed_global_cap(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_reservation("goal-a", 4)  # exactly the cap is fine
    try:
        reg.set_goal_reservation("goal-b", 1)  # total 5 > 4
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("accepted oversubscribed total")
    assert reg.get_goal_reservation_config("goal-b") is None
    reg.close()


def test_total_of_enabled_reservations_cannot_exceed_cap(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 3)
    reg.set_goal_reservation("goal-b", 2)
    try:
        reg.set_goal_reservation("goal-c", 2)  # total 7 > 6
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("accepted total above the cap")
    assert reg.get_goal_reservation_config("goal-c") is None
    # raising the cap first makes the same config legal
    reg.set_scheduler_global_max(8)
    reg.set_goal_reservation("goal-c", 2)
    assert reg.get_goal_reservation("goal-c") == 2
    reg.close()


def test_disabled_reservations_do_not_count_toward_total(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_reservation("goal-a", 4, enabled=False)
    # a disabled reservation does not consume protected capacity
    reg.set_goal_reservation("goal-b", 4)
    assert reg.get_goal_reservation("goal-a") == 4
    assert reg.get_goal_reservation("goal-b") == 4
    # re-enabling would oversubscribe -> fails closed
    try:
        reg.set_goal_reservation_enabled("goal-a", True)
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("re-enable oversubscribed the cap")
    assert reg.get_goal_reservation_config("goal-a")["enabled"] is False
    reg.close()


def test_lowering_global_cap_below_total_fails_closed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 2)
    try:
        reg.set_scheduler_global_max(3)  # total 4 > 3
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("cap lowered below reservation total")
    assert reg.get_scheduler_global_max() == 6
    reg.close()


def test_no_global_cap_accepts_reservations(db_path: str):
    """No cap => unbounded capacity => any bounded reservation is accepted
    and stored; the admission gate is a no-op until a cap exists."""
    reg = SQLiteStorage(db_path)
    reg.set_goal_reservation("goal-a", 7)  # no cap configured
    assert reg.get_goal_reservation("goal-a") == 7
    assert reg.list_goal_reservations()[0]["reservation"] == 7
    # setting a cap below the total afterwards is rejected
    try:
        reg.set_scheduler_global_max(5)
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("cap set below reservation total")
    assert reg.get_scheduler_global_max() is None
    reg.close()


# --------------------------------------------------------------------------- #
# enable / disable / remove
# --------------------------------------------------------------------------- #


def test_enable_disable_remove(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 2, now=T0)
    cfg = reg.set_goal_reservation_enabled("goal-a", False)
    assert cfg is not None and cfg["enabled"] is False
    assert reg.get_goal_reservation("goal-a") == 2  # value kept
    cfg = reg.set_goal_reservation_enabled("goal-a", True)
    assert cfg["enabled"] is True
    assert reg.remove_goal_reservation("goal-a") is True
    assert reg.get_goal_reservation_config("goal-a") is None
    assert reg.remove_goal_reservation("goal-a") is False  # idempotent
    reg.close()


def test_update_reservation_value(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 1, now=T0)
    reg.set_goal_reservation("goal-a", 3, now=_iso_plus(T0, 1))
    cfg = reg.get_goal_reservation_config("goal-a")
    assert cfg["reservation"] == 3 and cfg["updated_at"] == _iso_plus(T0, 1)
    reg.close()


# --------------------------------------------------------------------------- #
# durability + telemetry
# --------------------------------------------------------------------------- #


def test_reservations_survive_reopen(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 2, by="tester", now=T0)
    reg.set_goal_reservation("goal-b", 1, enabled=False, now=_iso_plus(T0, 1))
    reg.close()

    reg2 = SQLiteStorage(db_path)
    assert reg2.get_scheduler_global_max() == 6
    assert reg2.get_goal_reservation("goal-a") == 2
    assert reg2.get_goal_reservation_config("goal-a")["updated_by"] == "tester"
    assert reg2.get_goal_reservation_config("goal-b")["enabled"] is False
    assert reg2.get_goal_reservation("goal-b") == 1
    reg2.close()


def test_config_change_emits_goal_reservation_changed(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-a", 2, now=T0)
    events = [e for e in reg.scheduler_events(
        event_type="goal_reservation_changed")]
    assert len(events) == 1
    assert events[0].detail["goal_id"] == "goal-a"
    assert events[0].detail["config"] == "goal_reservation"
    assert events[0].detail["outcome"] == "set"
    reg.remove_goal_reservation("goal-a")
    events = [e for e in reg.scheduler_events(
        event_type="goal_reservation_changed")]
    assert len(events) == 2 and events[-1].detail["outcome"] == "removed"
    reg.close()


def test_failed_config_write_emits_no_event(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_reservation("goal-a", 2, now=T0)
    try:
        reg.set_goal_reservation("goal-b", 1)  # total 3 > 2
    except SchedulerRegistryError:
        pass
    events = [e for e in reg.scheduler_events(
        event_type="goal_reservation_changed")]
    assert len(events) == 1  # only goal-a's successful write
    reg.close()
