"""Scheduler telemetry: event model + durable storage (ADR-028, Phase A).

- typed event taxonomy (extending the existing AuditEvent vocabulary);
- bounded metadata, schema version, no secrets/payloads;
- durability across reopen;
- ATOMIC commit with the state transition: a claim success event can never
  outlive a rolled-back claim (no phantom success events);
- read-only bounded query API (filters + limits, no unbounded SELECT *);
- retention: explicit prune with bounded batch; authority tables untouched.
"""

from __future__ import annotations

import json
import time

import pytest

from arion.observability.events import AuditEvent
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"
KIND = "work.claimed"  # must be a legal scheduler event kind


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, goal_id="goal-a", task_id="t1", scheduler_id="sched-1", now=T0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=0,
                      scheduler_id=scheduler_id, now=now)


# --------------------------------------------------------------------------- #
# event model
# --------------------------------------------------------------------------- #


def test_scheduler_event_kinds_are_registered():
    from arion.observability.events import EVENT_KINDS

    for kind in ("scheduler.registered", "scheduler.heartbeat",
                 "scheduler.shutdown", "scheduler.abandoned",
                 "scheduler.config_changed", "work.queued", "work.claimed",
                 "work.claim_denied", "work.heartbeat", "work.reclaimed",
                 "work.handoff", "work.completed", "work.failed",
                 "capacity.denied", "scheduler_share.denied",
                 "goal_weight.denied", "goal_weight.refill"):
        assert kind in EVENT_KINDS, kind


def test_scheduler_event_bounded_and_no_secrets(db_path: str):
    reg = SQLiteStorage(db_path)
    ev = AuditEvent(
        kind="work.claimed",
        detail={"scheduler_id": "s-1", "worker_id": "w-1", "goal_id": "g-1",
                "task_id": "t-1", "work_id": "sw-1", "step_index": 0,
                "lease_expires_at": "x", "reason": "ok",
                "secret_token": "super-secret",  # must never persist
                "content": "file contents"},  # must never persist
    )
    reg.append_scheduler_event(ev)
    rows = reg.recent_scheduler_events(limit=10)
    assert len(rows) == 1
    d = rows[0].detail
    assert "secret_token" not in d and "content" not in d
    assert d["scheduler_id"] == "s-1" and d["work_id"] == "sw-1"
    assert d["schema_version"] == 1
    reg.close()


def test_events_durable_across_reopen(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.append_scheduler_event(AuditEvent(kind="scheduler.registered",
                                          detail={"scheduler_id": "s-1"}))
    reg.append_scheduler_event(AuditEvent(kind="work.claimed",
                                          detail={"work_id": "sw-1"}))
    reg.close()

    reg2 = SQLiteStorage(db_path)
    rows = reg2.recent_scheduler_events(limit=10)
    assert [r.kind for r in rows] == ["scheduler.registered", "work.claimed"]
    reg2.close()


# --------------------------------------------------------------------------- #
# atomicity: rollback leaves no phantom event
# --------------------------------------------------------------------------- #


def test_claim_rollback_leaves_no_phantom_event(db_path: str):
    """A claim that fails (state rolls back) must not leave a success
    event: the event commits atomically with the state transition."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(1)
    a = _mk(reg, task_id="t1", scheduler_id="sched-x")
    b = _mk(reg, task_id="t2", scheduler_id="sched-x", now=_iso_plus(T0, 1))
    got = reg.claim(a.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 10), max_lease_seconds=600.0,
                    scheduler_id="sched-x")
    assert got is not None
    # second claim is denied by the cap: the denied event exists, and
    # there is NO phantom `work.claimed` event for b
    got2 = reg.claim(b.work_id, worker_id="w", lease_seconds=60.0,
                     now=_iso_plus(T0, 10), max_lease_seconds=600.0,
                     scheduler_id="sched-x")
    assert got2 is None
    claimed_events = [e for e in reg.recent_scheduler_events(limit=100)
                      if e.kind == "work.claimed"]
    denied_events = [e for e in reg.recent_scheduler_events(limit=100)
                     if e.kind in ("work.claim_denied", "capacity.denied",
                                   "scheduler_share.denied", "goal_weight.denied")]
    assert len(claimed_events) == 1  # only the successful claim
    assert len(denied_events) == 1
    assert denied_events[0].detail["reason"] == "capacity"
    reg.close()


# --------------------------------------------------------------------------- #
# query API (bounded, read-only)
# --------------------------------------------------------------------------- #


def _seed_events(reg):
    for i, kind in enumerate(("scheduler.registered", "work.queued",
                              "work.claimed", "work.heartbeat",
                              "work.completed")):
        reg.append_scheduler_event(AuditEvent(
            kind=kind, ts=_iso_plus(T0, i),
            detail={"scheduler_id": "s-1", "goal_id": "g-1",
                    "task_id": "t-1", "work_id": f"sw-{i}", "step_index": 0}))


def test_query_filters(db_path: str):
    reg = SQLiteStorage(db_path)
    _seed_events(reg)
    reg.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=_iso_plus(T0, 99),
        detail={"scheduler_id": "s-2", "goal_id": "g-2",
                "task_id": "t-2", "work_id": "sw-99", "step_index": 1}))
    assert len(reg.scheduler_events(scheduler_id="s-2")) == 1
    assert len(reg.scheduler_events(goal_id="g-2")) == 1
    assert len(reg.scheduler_events(work_id="sw-3")) == 1
    assert len(reg.scheduler_events(event_type="work.claimed")) == 2
    assert len(reg.scheduler_events(since=_iso_plus(T0, 3))) == 3
    # ordering: oldest first
    rows = reg.recent_scheduler_events(limit=100)
    assert rows[0].kind == "scheduler.registered"
    assert rows[-1].kind == "work.claimed"
    reg.close()


def test_query_limits_bounded(db_path: str):
    reg = SQLiteStorage(db_path)
    for i in range(50):
        reg.append_scheduler_event(AuditEvent(
            kind="work.heartbeat", ts=_iso_plus(T0, i),
            detail={"scheduler_id": "s-1", "work_id": f"sw-{i}"}))
    assert len(reg.recent_scheduler_events(limit=10)) == 10
    assert len(reg.recent_scheduler_events(limit=1000)) == 50
    with pytest.raises(ValueError):
        reg.recent_scheduler_events(limit=5000)  # beyond the bounded max
    with pytest.raises(ValueError):
        reg.scheduler_events(limit=0)
    reg.close()


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #


def test_prune_older_than_cutoff(db_path: str):
    reg = SQLiteStorage(db_path)
    for i in range(5):
        reg.append_scheduler_event(AuditEvent(
            kind="work.heartbeat", ts=_iso_plus(T0, i * 10),
            detail={"work_id": f"sw-{i}"}))
    removed = reg.prune_scheduler_events(cutoff=_iso_plus(T0, 25))
    assert removed == 3  # ts 0, 10, 20 < 25
    rows = reg.recent_scheduler_events(limit=100)
    assert [r.detail["work_id"] for r in rows] == ["sw-3", "sw-4"]
    reg.close()


def test_prune_bounded_batch_and_idempotent(db_path: str):
    reg = SQLiteStorage(db_path)
    for i in range(60):
        reg.append_scheduler_event(AuditEvent(
            kind="work.heartbeat", ts=T0,
            detail={"work_id": f"sw-{i}"}))
    # batch size 40: the loop drains all 60 (bounded batches internally)
    assert reg.prune_scheduler_events(cutoff=_iso_plus(T0, 1),
                                      batch_size=40) == 60
    assert reg.prune_scheduler_events(cutoff=_iso_plus(T0, 1)) == 0
    reg.close()


def test_prune_never_touches_authority_tables(db_path: str):
    """Pruning events must not affect scheduler state/authority tables."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.register_scheduler("sched-1", pid=1, lease_seconds=60.0, now=T0)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0, scheduler_id="sched-1")
    # instrumentation emitted registration/queued/claim events; pruning
    # removes them but the authority tables must remain intact
    assert reg.prune_scheduler_events(cutoff=_iso_plus(T0, 9999)) == 4
    # authority tables intact and behavior unchanged
    assert reg.get_scheduler_global_max() == 3
    assert reg.get_goal_weight("goal-a") == 2
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 30))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    # a NEW claim still works (pruning did not disturb the claim path)
    row2 = _mk(reg, task_id="t2", now=_iso_plus(T0, 1))
    assert reg.claim(row2.work_id, worker_id="w", lease_seconds=60.0,
                     now=_iso_plus(T0, 2), max_lease_seconds=600.0,
                     scheduler_id="sched-1") is not None
    reg.close()


def test_event_count_observable(db_path: str):
    reg = SQLiteStorage(db_path)
    for i in range(7):
        reg.append_scheduler_event(AuditEvent(kind="work.heartbeat",
                                              detail={"work_id": f"sw-{i}"}))
    assert reg.scheduler_event_count() == 7
    oldest = reg.oldest_scheduler_event()
    assert oldest is not None and oldest.detail["work_id"] == "sw-0"
    reg.close()
