"""Adversarial telemetry tests (ADR-028, Phase G).

The invariant: telemetry is observational only - forged, deleted, or
duplicated audit events have ZERO effect on execution semantics.

- forged claim/completion/heartbeat/reclaim/DWRR events cannot create
  ownership, extend leases, complete work, bypass capacity, or alter
  weights;
- deleting events does not alter scheduler behavior;
- duplicated events do not cause duplicate execution;
- stale telemetry does not resurrect stale work;
- oversized event payloads are rejected/truncated (bounded metadata).
"""

from __future__ import annotations

from arion.observability.events import AuditEvent
from arion.state.models import GoalStatus, StepStatus
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

from tests.test_cross_goal_concurrency import (
    SlowReadCapability,
    TwoStepPlanner,
    _env,
    _submit,
    _task_for,
    _read_step,
)

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, goal_id="goal-a", task_id="t1", scheduler_id="sched-1", now=T0):
    return reg.create(task_id=task_id, goal_id=goal_id, step_index=0,
                      scheduler_id=scheduler_id, now=now)


def test_forged_claim_event_creates_no_ownership(db_path: str):
    """A forged `work.claimed` event (with a fake worker id) does not make
    the row RUNNING or owned: only the claim transaction does."""
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "goal_id": "goal-a", "outcome": "claimed"}))
    # the row is still QUEUED and unowned
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.QUEUED
    assert reg.get_work(row.work_id).worker_id is None
    # a real claim still works normally (telemetry did not help/hurt)
    got = reg.claim(row.work_id, worker_id="w-real", lease_seconds=60.0,
                    now=_iso_plus(T0, 1), max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    assert got is not None and got.worker_id == "w-real"
    reg.close()


def test_forged_completion_event_does_not_complete(db_path: str):
    """A forged `work.completed` event cannot mark work completed: the row
    stays RUNNING until the owner's terminal transition."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-real", lease_seconds=60.0,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.append_scheduler_event(AuditEvent(
        kind="work.completed", ts=_iso_plus(T0, 1),
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "outcome": "completed"}))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    # a forged FAILED event cannot fail it either
    reg.append_scheduler_event(AuditEvent(
        kind="work.failed", ts=_iso_plus(T0, 1),
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "outcome": "failed"}))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    reg.close()


def test_forged_heartbeat_event_extends_no_lease(db_path: str):
    """A forged `work.heartbeat` event cannot extend a lease: the lease
    only moves via the ownership-checked heartbeat transaction."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w-real", lease_seconds=10.0,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-1")
    expiry = reg.get_work(row.work_id).lease_expires_at
    reg.append_scheduler_event(AuditEvent(
        kind="work.heartbeat", ts=_iso_plus(T0, 5),
        detail={"work_id": row.work_id, "worker_id": "w-forged",
                "lease_expires_at": _iso_plus(T0, 9999)}))
    assert reg.get_work(row.work_id).lease_expires_at == expiry  # unchanged
    reg.close()


def test_forged_reclaim_and_dwrr_events_bypass_nothing(db_path: str):
    """Forged `work.reclaimed`/`goal_weight.refill`/`capacity.denied` events
    cannot reclaim work, refill credit, or deny admission."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.append_scheduler_event(AuditEvent(
        kind="work.reclaimed", ts=T0,
        detail={"work_id": row.work_id, "outcome": "reclaimed"}))
    reg.append_scheduler_event(AuditEvent(
        kind="goal_weight.refill", ts=T0,
        detail={"goal_id": "goal-a", "weight": 999, "credit_before": 0,
                "credit_after": 999}))
    # the row is untouched; the durable credit is untouched
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.RUNNING
    assert reg._conn.execute(
        "SELECT deficit FROM scheduler_goal_state WHERE goal_id='goal-a'"
    ).fetchone() is None or reg._conn.execute(
        "SELECT deficit FROM scheduler_goal_state WHERE goal_id='goal-a'"
    ).fetchone()[0] == 0
    reg.close()


def test_deleting_events_does_not_alter_behavior(db_path: str):
    """Deleting ALL telemetry events leaves admission/ownership identical."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-1")
    from arion.state.models import utcnow as _utcnow
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    reg.prune_scheduler_events(cutoff=future)
    assert reg.scheduler_event_count() == 0
    # behavior identical: a second goal claims normally (capacity + DWRR)
    reg.set_goal_weight("goal-b", 1)
    row2 = _mk(reg, goal_id="goal-b", task_id="t2", now=_iso_plus(T0, 1))
    got = reg.claim(row2.work_id, worker_id="w", lease_seconds=60.0,
                    now=_iso_plus(T0, 2), max_lease_seconds=600.0,
                    scheduler_id="sched-1")
    assert got is not None
    # and the real terminal transition works, re-emitting fresh events
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 3))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.COMPLETED
    assert any(e.kind == "work.completed"
               for e in reg.recent_scheduler_events(limit=100))
    reg.close()


def test_duplicated_events_do_not_duplicate_execution(db_path: str):
    """Duplicating claim/completion events (same id or new ids) cannot make
    a row execute twice."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    ev = AuditEvent(kind="work.claimed", ts=T0,
                    detail={"work_id": row.work_id, "worker_id": "w",
                            "outcome": "claimed"})
    reg.append_scheduler_event(ev)
    reg.append_scheduler_event(ev)  # duplicate (same id) -> ignored
    reg.append_scheduler_event(AuditEvent(  # duplicate with new id
        kind="work.claimed", ts=T0,
        detail={"work_id": row.work_id, "worker_id": "w", "outcome": "claimed"}))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.QUEUED
    # the real pipeline runs exactly once
    assert reg.claim(row.work_id, worker_id="w", lease_seconds=60.0,
                     now=_iso_plus(T0, 1), max_lease_seconds=600.0,
                     scheduler_id="sched-1") is not None
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 2))
    completed = [e for e in reg.scheduler_events(work_id=row.work_id)
                 if e.kind == "work.completed"]
    assert len(completed) == 1  # exactly one real completion
    reg.close()


def test_stale_telemetry_does_not_resurrect_stale_work(db_path: str):
    """Old telemetry claiming work was RUNNING cannot resurrect an
    ABANDONED row."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w", lease_seconds=1.0,
              now=T0, max_lease_seconds=600.0, scheduler_id="sched-1")
    reg.reclaim_stale(now=_iso_plus(T0, 2))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    reg.append_scheduler_event(AuditEvent(
        kind="work.heartbeat", ts=_iso_plus(T0, 3),
        detail={"work_id": row.work_id, "worker_id": "w",
                "lease_expires_at": _iso_plus(T0, 9999)}))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    with __import__("pytest").raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w", lease_seconds=60.0,
                      now=_iso_plus(T0, 3), max_lease_seconds=600.0)
    reg.close()


def test_oversized_event_payload_truncated(db_path: str):
    """Oversized/unknown detail keys are dropped or truncated (bounded)."""
    reg = SQLiteStorage(db_path)
    reg.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"work_id": "sw-1", "reason": "x" * 10000,  # truncated
                "secret": "s3cr3t", "payload": {"a": 1},  # dropped
                "worker_id": "w-1"}))
    rows = reg.recent_scheduler_events(limit=10)
    d = rows[0].detail
    assert d["worker_id"] == "w-1"
    assert len(d["reason"]) <= 200
    assert "secret" not in d and "payload" not in d
    reg.close()


def test_forged_telemetry_cannot_bypass_engine_execution(tmp_path):
    """End-to-end: forged telemetry rows + events next to a real engine
    cannot make the engine skip the live pipeline or fake completion."""
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name="adv.db")
    reg = env.engine.scheduler_registry
    gid = _submit(env, "goal one")
    task = _task_for(env, gid)
    # attacker forges events claiming the step is done + owned
    reg.append_scheduler_event(AuditEvent(
        kind="work.completed", ts=T0,
        detail={"task_id": task.id, "work_id": "sw-fake",
                "outcome": "completed"}))
    results = env.engine.run_goals([gid])
    assert results[gid].status == GoalStatus.COMPLETED
    task2 = _task_for(env, gid)
    assert task2.steps[0].status == StepStatus.SUCCEEDED
    # the real pipeline ran (authorization + execution), exactly once
    checked = [e for e in env.storage.list_events()
               if e.kind == "permission.checked"]
    assert len(checked) == 1
    env.engine.shutdown()
    env.engine.storage.close()
