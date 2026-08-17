"""Telemetry crash/restart consistency (ADR-028, Phase H).

- a process crashes after a durable claim: the stale lease is reclaimed
  WITH its `work.reclaimed` event committed atomically (subprocess);
- a restarted process observes the full history;
- rollback leaves no phantom success event;
- committed events survive a reopen;
- an active scheduler's heartbeat does not falsely appear stale (its
  heartbeat events + live registration);
- abandoned scheduler events are distinguishable from normal shutdown.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arion.observability.events import AuditEvent
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_scheduler_multi_worker.py")
T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _future(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_subprocess_crash_claim_then_reclaim_with_event(tmp_path):
    """A subprocess claims a row then dies (crash-claimed). After the lease
    lapses, reclaim_stale marks it ABANDONED AND emits `work.reclaimed`
    atomically; the history is observable from a fresh store handle."""
    db = str(tmp_path / "t.db")
    store = SQLiteStorage(db)
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-shared")
    store.close()
    proc = subprocess.Popen([sys.executable, WORKER, "crash-claimed",
                             "--db", db, "--work-id", row.work_id,
                             "--lease", "0.5"],
                            stdout=subprocess.PIPE, text=True)
    out = json.loads(proc.stdout.readline().strip())
    assert out["status"] == "running"
    proc.wait(timeout=30)
    assert proc.returncode == 1
    # the claim event was committed by the (dying) process
    store = SQLiteStorage(db)
    claimed = [e for e in store.scheduler_events(work_id=row.work_id)
               if e.kind == "work.claimed"]
    assert len(claimed) == 1 and claimed[0].detail["worker_id"] == out["worker"]
    time.sleep(0.7)
    # reclaim is atomic WITH its event
    reclaimed = store.reclaim_stale()
    assert reclaimed == [row.work_id]
    events = [e for e in store.scheduler_events(work_id=row.work_id)
              if e.kind == "work.reclaimed"]
    assert len(events) == 1
    assert events[0].detail["reason"] == "lease_expired"
    # the restarted process observes the full history
    history = store.scheduler_events(work_id=row.work_id)
    assert [e.kind for e in history] == ["work.queued", "work.claimed",
                                         "work.reclaimed"]
    store.close()


def test_rollback_leaves_no_phantom_success_event(db_path: str):
    """A claim denied by the cap emits the DENIED event only - no phantom
    success event for the rolled-back transition."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(1)
    a = reg.create(task_id="t1", goal_id="g1", step_index=0,
                   scheduler_id="s", now=T0)
    b = reg.create(task_id="t2", goal_id="g2", step_index=0,
                   scheduler_id="s", now=_iso_plus(T0, 1))
    assert reg.claim(a.work_id, "w", 60.0, _iso_plus(T0, 2), 600.0,
                     scheduler_id="s") is not None
    assert reg.claim(b.work_id, "w", 60.0, _iso_plus(T0, 2), 600.0,
                     scheduler_id="s") is None
    kinds = [e.kind for e in reg.scheduler_events(work_id=b.work_id)]
    assert kinds == ["work.queued", "capacity.denied"]
    reg.close()


def test_committed_events_survive_reopen(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    row = reg.create(task_id="t1", goal_id="goal-a", step_index=0,
                     scheduler_id="s", now=T0)
    reg.claim(row.work_id, "w", 60.0, _iso_plus(T0, 1), 600.0, scheduler_id="s")
    reg.heartbeat(row.work_id, "w", 60.0, _iso_plus(T0, 2), 600.0)
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w", now=_iso_plus(T0, 3))
    reg.close()

    reg2 = SQLiteStorage(db_path)
    kinds = [e.kind for e in reg2.scheduler_events(work_id=row.work_id)]
    # the refill event precedes the claim (same transaction); the terminal
    # event is last
    assert kinds[-1] == "work.completed"
    assert "work.claimed" in kinds and "work.heartbeat" in kinds
    reg2.close()


def test_active_scheduler_heartbeat_not_stale(db_path: str):
    """A live scheduler heartbeats: its registration is not stale and its
    heartbeat events are present (distinguishable from an abandoned one)."""
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-live", pid=1, lease_seconds=60.0, now=T0)
    reg.heartbeat_scheduler("sched-live", lease_seconds=60.0,
                            now=_iso_plus(T0, 30), max_lease_seconds=300.0)
    st = reg.scheduler_status(now=_iso_plus(T0, 45))
    assert st["active_schedulers"] == 1 and st["stale_schedulers"] == 0
    hb = [e for e in reg.scheduler_events(scheduler_id="sched-live")
          if e.kind == "scheduler.heartbeat"]
    assert len(hb) == 1
    reg.close()


def test_abandoned_vs_shutdown_distinguishable(db_path: str):
    """`scheduler.abandoned` (dead registration) events are distinct from
    `scheduler.shutdown` (clean unregister)."""
    reg = SQLiteStorage(db_path)
    # clean shutdown
    reg.register_scheduler("sched-clean", pid=1, lease_seconds=60.0, now=T0)
    reg.unregister_scheduler("sched-clean")
    # abandoned queue (dead registration)
    reg.register_scheduler("sched-dead", pid=2, lease_seconds=0.01, now=T0)
    reg.create(task_id="t1", goal_id="g1", step_index=0,
               scheduler_id="sched-dead", now=_iso_plus(T0, 1))
    time.sleep(0.05)
    reg.abandon_foreign_queued("sched-mine")
    shutdowns = [e for e in reg.scheduler_events(scheduler_id="sched-clean")
                 if e.kind == "scheduler.shutdown"]
    abandoneds = [e for e in reg.scheduler_events()
                  if e.kind == "scheduler.abandoned"]
    assert len(shutdowns) == 1
    assert len(abandoneds) == 1
    assert abandoneds[0].detail["scheduler_id"] == "sched-dead"
    reg.close()


def test_prune_preserves_committed_events_after_cutoff(db_path: str):
    """Events newer than the cutoff survive pruning (no silent deletion of
    recent events)."""
    reg = SQLiteStorage(db_path)
    reg.append_scheduler_event(AuditEvent(kind="work.queued", ts=T0,
                                          detail={"work_id": "sw-old"}))
    reg.append_scheduler_event(AuditEvent(kind="work.queued",
                                          ts=_iso_plus(T0, 100),
                                          detail={"work_id": "sw-new"}))
    removed = reg.prune_scheduler_events(cutoff=_iso_plus(T0, 50))
    assert removed == 1
    remaining = reg.recent_scheduler_events(limit=10)
    assert [e.detail["work_id"] for e in remaining] == ["sw-new"]
    reg.close()
