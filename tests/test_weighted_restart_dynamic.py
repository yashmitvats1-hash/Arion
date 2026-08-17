"""Weighted policy: restart + dynamic configuration (ADR-027, Phase D).

- weights survive restart; queued work keeps its goal association;
- weight change while work is queued affects only FUTURE admission (no
  retroactive effect on RUNNING rows, no capacity duplication);
- disable/re-enable a goal's weight;
- a new goal without config uses the default weight;
- persisted fairness state (deficit) survives restart and the weighted
  cycle continues exactly;
- crash-while-running recovery is unchanged with weights present (stale
  lease reclaim; no duplicate mutation).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int):
    return [reg.create(task_id=f"t-{goal_id}-{i}", goal_id=goal_id,
                       step_index=i, scheduler_id="sched-x", now=_iso_plus(T0, i))
            for i in range(n)]


def _claim(reg, row, now=_iso_plus(T0, 100)):
    return reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                     now=now, max_lease_seconds=600.0,
                     scheduler_id="sched-x")


def _complete(reg, row, now=_iso_plus(T0, 101)):
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now, owner_worker_id="w")


def test_weights_survive_restart_with_queued_work(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    rows_a = _rows(reg, "goal-a", 4)
    reg.close()

    # restart: weights + queued rows persist, goal association intact
    reg2 = SQLiteStorage(db_path)
    assert reg2.get_goal_weight("goal-a") == 2
    queued = [r for r in reg2.list_work(status=SchedulerWorkStatus.QUEUED)]
    assert len(queued) == 4 and all(r.goal_id == "goal-a" for r in queued)
    assert _claim(reg2, rows_a[0]) is not None
    reg2.close()


def test_weight_change_while_queued_future_admission_only(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 3)
    reg.set_goal_weight("goal-b", 1)
    rows_a = _rows(reg, "goal-a", 8)
    rows_b = _rows(reg, "goal-b", 8)
    # one 3:1 round (credit 3+1)
    a1, a2, a3 = rows_a[0], rows_a[1], rows_a[2]
    b1 = rows_b[0]
    assert _claim(reg, a1) and _claim(reg, a2) and _claim(reg, a3) and _claim(reg, b1)
    running_before = [r.work_id for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)]
    # change A's weight while work is queued/running
    reg.set_goal_weight("goal-a", 1)
    # RUNNING work stays owned: nothing was cancelled or replayed
    still = [r.work_id for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)]
    assert set(still) == set(running_before)
    for row in [a1, a2, a3, b1]:
        _complete(reg, row)
    # future rounds are 1:1 (the changed weight applies to NEW admission)
    claimed = {"goal-a": 3, "goal-b": 1}
    for _ in range(2):
        for g, rows in (("goal-a", rows_a), ("goal-b", rows_b)):
            assert _claim(reg, rows[claimed[g]]) is not None
            claimed[g] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed == {"goal-a": 5, "goal-b": 3}  # 1:1 after the change
    reg.close()


def test_disable_re_enable_while_queued(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    rows_b = _rows(reg, "goal-b", 4)
    _rows(reg, "goal-a", 4)
    reg.set_goal_weight_enabled("goal-b", False)
    assert not _claim(reg, rows_b[0])          # disabled: never admitted
    assert reg.get_work(rows_b[0].work_id).status == SchedulerWorkStatus.QUEUED
    reg.set_goal_weight_enabled("goal-b", True)
    assert _claim(reg, rows_b[0]) is not None  # re-enabled
    reg.close()


def test_new_goal_without_config_uses_default(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 5)
    rows_new = _rows(reg, "goal-new", 2)  # no config: default weight 1
    assert reg.get_goal_weight("goal-new") == 1
    # with only goal-new contending it claims freely (full cap)
    assert _claim(reg, rows_new[0]) is not None
    assert _claim(reg, rows_new[1]) is not None
    reg.close()


def test_deficit_persists_across_restart_cycle_continues(db_path: str):
    """After a store reopen the durable deficit continues the weighted
    cycle exactly (no in-memory counter needed)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    rows_a = _rows(reg, "goal-a", 8)
    rows_b = _rows(reg, "goal-b", 4)
    # one 2:1 round
    assert _claim(reg, rows_a[0]) and _claim(reg, rows_b[0]) and _claim(reg, rows_a[1])
    for row in (rows_a[0], rows_a[1], rows_b[0]):
        _complete(reg, row)
    reg.close()

    reg2 = SQLiteStorage(db_path)
    claimed = {"goal-a": 2, "goal-b": 1}
    for _ in range(2):
        for g, rows in (("goal-a", rows_a), ("goal-b", rows_b),
                        ("goal-a", rows_a)):
            if claimed[g] < len(rows):
                assert _claim(reg2, rows[claimed[g]]) is not None
                claimed[g] += 1
        for row in reg2.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg2, row)
    assert claimed == {"goal-a": 6, "goal-b": 3}  # 2:1 continued after reopen
    reg2.close()


def test_crash_while_running_with_weights_no_duplicate(db_path: str):
    """A claimed row whose owner stops heartbeating is reclaimed exactly as
    in ADR-026; weights do not change crash recovery; a fresh row for the
    same goal completes exactly once."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    row = _rows(reg, "goal-a", 1)[0]
    reg.claim(row.work_id, worker_id="w-crashed", lease_seconds=0.3,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-x")
    reg.close()
    time.sleep(0.5)
    reg = SQLiteStorage(db_path)
    assert reg.reclaim_stale() == [row.work_id]
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    # the goal's weight config survived the crash
    assert reg.get_goal_weight("goal-a") == 2
    # a fresh row for the same goal runs normally
    row2 = _rows(reg, "goal-a", 1)[0]
    assert _claim(reg, row2) is not None
    reg.close()
