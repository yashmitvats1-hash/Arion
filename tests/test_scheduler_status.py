"""Scheduler status snapshot + query API (ADR-028, Phases C/D).

- scheduler_status() summarizes durable state (global max, running/queued
  counts, active/stale schedulers, running by scheduler, queued/running by
  goal, goal weights, DWRR credit, recent reclaim/failure counts);
- it is a READ-ONLY observation computed from durable state - never a
  cached authority;
- the read-only query API is bounded and filters correctly.
"""

from __future__ import annotations

from arion.state.models import utcnow
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, goal_id="goal-a", task_id="t1", scheduler_id="sched-1", now=T0,
        step=0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=step,
                      scheduler_id=scheduler_id, now=now)


def test_status_empty(db_path: str):
    reg = SQLiteStorage(db_path)
    st = reg.scheduler_status()
    assert st["global_max_concurrency"] is None
    assert st["running_count"] == 0 and st["queued_count"] == 0
    assert st["active_schedulers"] == 0 and st["stale_schedulers"] == 0
    assert st["recent_reclaim_count"] == 0 and st["recent_failure_count"] == 0
    assert st["running_by_scheduler"] == {} and st["queued_by_goal"] == {}
    assert st["running_by_goal"] == {} and st["goal_weights"] == []
    assert st["dwr_credit"] == {}
    reg.close()


def test_status_populated(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    reg.register_scheduler("sched-live", pid=1, lease_seconds=3600.0, now=T0)
    reg.register_scheduler("sched-stale", pid=2, lease_seconds=1.0, now=T0)
    a1 = _mk(reg, goal_id="goal-a", scheduler_id="sched-live")
    a2 = _mk(reg, goal_id="goal-a", scheduler_id="sched-live", now=_iso_plus(T0, 1))
    b1 = _mk(reg, goal_id="goal-b", scheduler_id="sched-live", now=_iso_plus(T0, 2))
    reg.claim(a1.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 3),
              max_lease_seconds=600.0, scheduler_id="sched-live")
    # sched-stale's registration lapsed
    st = reg.scheduler_status(now=_iso_plus(T0, 5))
    assert st["global_max_concurrency"] == 4
    assert st["running_count"] == 1 and st["queued_count"] == 2
    assert st["active_schedulers"] == 1 and st["stale_schedulers"] == 1
    assert st["running_by_scheduler"] == {"sched-live": 1}
    assert st["running_by_goal"] == {"goal-a": 1}
    assert st["queued_by_goal"] == {"goal-a": 1, "goal-b": 1}
    assert {r["goal_id"]: r["weight"] for r in st["goal_weights"]} == \
        {"goal-a": 2, "goal-b": 1}
    assert set(st["dwr_credit"].keys()) == {"goal-a", "goal-b"}
    reg.close()


def test_status_credit_and_recent_counts(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    a1 = _mk(reg, goal_id="goal-a", scheduler_id="sched-x")
    b1 = _mk(reg, goal_id="goal-b", scheduler_id="sched-x", now=_iso_plus(T0, 1))
    reg.claim(a1.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-x")
    reg.claim(b1.work_id, worker_id="w", lease_seconds=60.0, now=_iso_plus(T0, 2),
              max_lease_seconds=600.0, scheduler_id="sched-x")
    st = reg.scheduler_status(now=_iso_plus(T0, 3))
    assert st["dwr_credit"]["goal-a"] == 1  # 2 - 1 spent
    assert st["dwr_credit"]["goal-b"] == 0  # 1 - 1 spent
    # a failure + a reclaim increment the recent counts: complete b1 to
    # free capacity, claim a fresh row (goal-c, unconfigured = fresh DWRR
    # credit) with a short lease, fail a1, then let the fresh row's lease
    # lapse and reclaim it
    reg.mark_terminal(b1.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 3))
    # a1's failure frees its credit; then a fresh unconfigured goal's claim
    # triggers a refill round and succeeds with a short lease
    reg.mark_terminal(a1.work_id, SchedulerWorkStatus.FAILED,
                      error="boom", owner_worker_id="w", now=_iso_plus(T0, 3))
    c1 = _mk(reg, goal_id="goal-c", task_id="t-c", scheduler_id="sched-x",
             now=_iso_plus(T0, 3))
    assert reg.claim(c1.work_id, worker_id="w", lease_seconds=1.0,
                     now=_iso_plus(T0, 3), max_lease_seconds=600.0,
                     scheduler_id="sched-x") is not None
    reg.reclaim_stale(now=_iso_plus(T0, 5))  # c1's lease expired at T+4
    st2 = reg.scheduler_status(now=_iso_plus(T0, 6))
    assert st2["recent_failure_count"] >= 1
    assert st2["recent_reclaim_count"] >= 1
    reg.close()


def test_status_is_observation_not_authority(db_path: str):
    """Calling scheduler_status() must not change any state (no writes)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 2)
    row = _mk(reg, goal_id="goal-a")
    reg.claim(row.work_id, worker_id="w", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    before = reg.scheduler_status(now=_iso_plus(T0, 1))
    again = reg.scheduler_status(now=_iso_plus(T0, 1))
    assert before == again  # read-only, deterministic
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    assert reg.get_goal_weight("goal-a") == 2
    reg.close()


def test_query_api_by_work_and_type(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg, goal_id="goal-a")
    reg.claim(row.work_id, worker_id="w", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.heartbeat(row.work_id, "w", lease_seconds=60.0, now=_iso_plus(T0, 1),
                  max_lease_seconds=600.0)
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 2))
    by_work = reg.scheduler_events(work_id=row.work_id)
    assert [e.kind for e in by_work] == ["work.queued", "work.claimed",
                                         "work.heartbeat", "work.completed"]
    by_type = reg.scheduler_events(event_type="work.heartbeat")
    assert len(by_type) == 1 and by_type[0].work_id == row.work_id  # noqa
    since = reg.scheduler_events(since=_iso_plus(T0, 1))
    assert [e.kind for e in since] == ["work.heartbeat", "work.completed"]
    reg.close()
