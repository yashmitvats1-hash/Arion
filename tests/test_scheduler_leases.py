"""Durable lease/ownership primitives (ADR-026, Phase A) - tests first.

The registry gains real ownership:

- scheduler registration (unique id, lease, heartbeat, expiry, unregister);
- atomic claim/claim_next (exactly one owner under race);
- worker ownership leases on RUNNING rows (bounded, monotonic, checked);
- heartbeats (ownership-checked, monotonic, bounded, stale-rejected);
- stale-owner rejection on terminal transitions and handoff;
- release_and_claim_next atomic handoff (release_and_select_next-style);
- optional cross-process global capacity enforced at claim time;
- abandon_foreign_queued now keyed on registration LIVENESS (a live peer's
  queue is never abandoned).
"""

from __future__ import annotations

import threading
import time

import pytest

from arion.state.scheduler_work import (
    SchedulerRegistryError,
    SchedulerStateError,
    SchedulerWorkStatus,
)
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _mk(reg, task_id="task_a", step_index=0, scheduler_id="sched-1", now=T0):
    return reg.create(task_id=task_id, goal_id=None, step_index=step_index,
                      scheduler_id=scheduler_id, now=now)


# --------------------------------------------------------------------------- #
# scheduler registration
# --------------------------------------------------------------------------- #


def test_register_scheduler_and_liveness(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-1", pid=111, lease_seconds=60.0, now=T0)
    assert reg.scheduler_registration_live("sched-1", now=T0)
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 30))
    assert not reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 61))
    assert not reg.scheduler_registration_live("sched-nope", now=T0)
    reg.close()


def test_heartbeat_scheduler_extends_registration(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-1", pid=111, lease_seconds=60.0, now=T0)
    assert reg.heartbeat_scheduler("sched-1", lease_seconds=60.0, now=_iso_plus(T0, 30),
                                   max_lease_seconds=600.0)
    # extended to 30+60
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 89))
    assert not reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 91))
    # unknown scheduler: no-op (False), never an error
    assert not reg.heartbeat_scheduler("sched-ghost", lease_seconds=60.0, now=T0,
                                       max_lease_seconds=600.0)
    reg.close()


def test_heartbeat_scheduler_bounded_and_monotonic(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-1", pid=111, lease_seconds=60.0, now=T0)
    # forged FUTURE heartbeat: the lease would have lapsed -> no extension
    assert not reg.heartbeat_scheduler("sched-1", lease_seconds=60.0,
                                       now="2099-01-01T00:00:00+00:00",
                                       max_lease_seconds=300.0)
    # an in-window heartbeat extends (30+60)
    assert reg.heartbeat_scheduler("sched-1", lease_seconds=60.0,
                                   now=_iso_plus(T0, 30), max_lease_seconds=300.0)
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 89))
    assert not reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 91))
    # Repeated in-window heartbeats use a sliding bound: each extension is at
    # most max_lease beyond its heartbeat, so a live scheduler can remain live.
    for t in (89, 148, 207, 266):
        assert reg.heartbeat_scheduler("sched-1", lease_seconds=60.0,
                                       now=_iso_plus(T0, t),
                                       max_lease_seconds=300.0)
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 325))
    assert not reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 327))
    # Once the renewed lease lapsed, no heartbeat can resurrect it.
    assert not reg.heartbeat_scheduler("sched-1", lease_seconds=60.0,
                                       now=_iso_plus(T0, 327),
                                       max_lease_seconds=300.0)
    # An old but still syntactically valid timestamp never shrinks the lease.
    assert reg.heartbeat_scheduler("sched-1", lease_seconds=60.0, now=T0,
                                   max_lease_seconds=300.0)
    assert reg.scheduler_registration_live("sched-1", now=_iso_plus(T0, 325))
    reg.close()


def test_unregister_scheduler(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-1", pid=111, lease_seconds=60.0, now=T0)
    reg.unregister_scheduler("sched-1")
    assert not reg.scheduler_registration_live("sched-1", now=T0)
    # idempotent
    reg.unregister_scheduler("sched-1")
    reg.close()


# --------------------------------------------------------------------------- #
# claim / claim_next: atomic ownership
# --------------------------------------------------------------------------- #


def test_claim_transitions_queued_to_running_with_owner(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    claimed = reg.claim(row.work_id, worker_id="worker:1:abc", lease_seconds=60.0,
                        now=T0, max_lease_seconds=600.0)
    assert claimed.status == SchedulerWorkStatus.RUNNING
    assert claimed.worker_id == "worker:1:abc"
    assert claimed.started_at == T0
    assert claimed.lease_expires_at == _iso_plus(T0, 60)
    # double claim fails closed
    with pytest.raises(SchedulerStateError):
        reg.claim(row.work_id, worker_id="worker:2:def", lease_seconds=60.0,
                  now=T0, max_lease_seconds=600.0)
    reg.close()


def test_claim_next_oldest_queued_for_scheduler(db_path: str):
    reg = SQLiteStorage(db_path)
    a = _mk(reg, task_id="t1", scheduler_id="sched-a", now=T0)
    b = _mk(reg, task_id="t2", scheduler_id="sched-a", now=_iso_plus(T0, 1))
    _mk(reg, task_id="t3", scheduler_id="sched-b", now=_iso_plus(T0, 2))
    got = reg.claim_next("sched-a", worker_id="w1", lease_seconds=60.0,
                         now=_iso_plus(T0, 3), max_lease_seconds=600.0)
    assert got is not None and got.work_id == a.work_id  # oldest first
    got2 = reg.claim_next("sched-a", worker_id="w1", lease_seconds=60.0,
                          now=_iso_plus(T0, 3), max_lease_seconds=600.0)
    assert got2 is not None and got2.work_id == b.work_id
    # scheduler-b's row is never claimable by sched-a
    assert reg.claim_next("sched-a", worker_id="w1", lease_seconds=60.0,
                          now=_iso_plus(T0, 3), max_lease_seconds=600.0) is None
    assert reg.get_work(a.work_id).status == SchedulerWorkStatus.RUNNING
    reg.close()


def test_two_threads_racing_claim_next_exactly_one_owner(db_path: str):
    """Two store handles (two 'processes') racing for one queued item:
    exactly one claim succeeds."""
    reg = SQLiteStorage(db_path)
    row = _mk(reg, scheduler_id="sched-shared", now=T0)
    reg2 = SQLiteStorage(db_path)
    results = []

    def race(store, worker):
        try:
            got = store.claim_next("sched-shared", worker_id=worker, lease_seconds=60.0,
                                   now=_iso_plus(T0, 1), max_lease_seconds=600.0)
            results.append((worker, got.work_id if got else None))
        except Exception as exc:  # pragma: no cover - unexpected
            results.append((worker, f"ERR:{exc}"))

    threads = [threading.Thread(target=race, args=(reg, "w-a")),
               threading.Thread(target=race, args=(reg2, "w-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    winners = [w for w, wid in results if wid == row.work_id]
    assert len(winners) == 1, results
    final = reg.get_work(row.work_id)
    assert final.status == SchedulerWorkStatus.RUNNING
    assert final.worker_id == winners[0]
    reg.close()
    reg2.close()


def test_claim_after_reclaim_requires_fresh_claim(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=1.0, now=T0,
              max_lease_seconds=600.0)
    reg.reclaim_stale(now=_iso_plus(T0, 2))
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    # a stale owner cannot re-claim; the row is terminal
    with pytest.raises(SchedulerStateError):
        reg.claim(row.work_id, worker_id="w1", lease_seconds=60.0,
                  now=_iso_plus(T0, 3), max_lease_seconds=600.0)
    reg.close()


# --------------------------------------------------------------------------- #
# heartbeats (work rows)
# --------------------------------------------------------------------------- #


def test_heartbeat_extends_lease_and_checks_owner(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    hb = reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                       now=_iso_plus(T0, 30), max_lease_seconds=600.0)
    assert hb.lease_expires_at == _iso_plus(T0, 90)
    # wrong owner cannot heartbeat
    with pytest.raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w-evil", lease_seconds=60.0,
                      now=_iso_plus(T0, 31), max_lease_seconds=600.0)
    reg.close()


def test_heartbeat_monotonic_and_bounded(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=120.0)
    # forged FUTURE heartbeat: the lease would have lapsed at that time ->
    # rejected (a forged/future heartbeat must not extend ownership)
    with pytest.raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                      now="2099-01-01T00:00:00+00:00", max_lease_seconds=120.0)
    # a heartbeat with a timestamp BEFORE start is a forged/past heartbeat
    with pytest.raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                      now="2025-01-01T00:00:00+00:00", max_lease_seconds=120.0)
    # A legitimate in-window heartbeat extends with a sliding per-renewal cap.
    hb = reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                       now=_iso_plus(T0, 10), max_lease_seconds=120.0)
    assert hb.lease_expires_at == _iso_plus(T0, 70)
    hb2 = reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                        now=_iso_plus(T0, 69), max_lease_seconds=120.0)
    assert hb2.lease_expires_at == _iso_plus(T0, 129)
    hb3 = reg.heartbeat(row.work_id, "w1", lease_seconds=1000.0,
                        now=_iso_plus(T0, 119), max_lease_seconds=120.0)
    assert hb3.lease_expires_at == _iso_plus(T0, 239)
    # An older heartbeat that would shrink the lease never shrinks it.
    hb4 = reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                        now=_iso_plus(T0, 10), max_lease_seconds=120.0)
    assert hb4.lease_expires_at == _iso_plus(T0, 239)
    reg.close()


def test_heartbeat_after_expiry_is_rejected_stale_owner_cannot_resurrect(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=10.0, now=T0,
              max_lease_seconds=600.0)
    with pytest.raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                      now=_iso_plus(T0, 11), max_lease_seconds=600.0)
    reg.reclaim_stale(now=_iso_plus(T0, 11))
    with pytest.raises(SchedulerStateError):
        reg.heartbeat(row.work_id, "w1", lease_seconds=60.0,
                      now=_iso_plus(T0, 12), max_lease_seconds=600.0)
    reg.close()


# --------------------------------------------------------------------------- #
# stale-owner rejection on terminal transitions
# --------------------------------------------------------------------------- #


def test_mark_terminal_requires_current_owner(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    # the owner completes fine
    done = reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                             now=_iso_plus(T0, 5), owner_worker_id="w1")
    assert done.status == SchedulerWorkStatus.COMPLETED

    row2 = _mk(reg, task_id="t2")
    reg.claim(row2.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    # a stale/foreign worker cannot complete or fail the row
    with pytest.raises(SchedulerStateError):
        reg.mark_terminal(row2.work_id, SchedulerWorkStatus.COMPLETED,
                          now=_iso_plus(T0, 5), owner_worker_id="w-evil")
    with pytest.raises(SchedulerStateError):
        reg.mark_terminal(row2.work_id, SchedulerWorkStatus.FAILED,
                          now=_iso_plus(T0, 5), owner_worker_id="w-evil")
    # the row is still RUNNING (nothing happened)
    assert reg.get_work(row2.work_id).status == SchedulerWorkStatus.RUNNING
    reg.close()


def test_stale_owner_cannot_complete_after_reclaim(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    reg.claim(row.work_id, worker_id="w1", lease_seconds=1.0, now=T0,
              max_lease_seconds=600.0)
    reg.reclaim_stale(now=_iso_plus(T0, 2))  # lease expired -> ABANDONED
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    # the old owner tries to report completion: rejected (row is terminal)
    with pytest.raises(SchedulerStateError):
        reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                          now=_iso_plus(T0, 3), owner_worker_id="w1")
    reg.close()


def test_mark_running_lease_is_bounded_when_max_given(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg)
    running = reg.mark_running(row.work_id, worker_id="w-forged", lease_seconds=1e9,
                               now=T0, max_lease_seconds=60.0)
    assert running.lease_expires_at == _iso_plus(T0, 60)
    # a forged enormous lease is reclaimable after the cap
    reclaimed = reg.reclaim_stale(now=_iso_plus(T0, 61))
    assert reclaimed == [row.work_id]
    reg.close()


# --------------------------------------------------------------------------- #
# release_and_claim_next atomic handoff
# --------------------------------------------------------------------------- #


def test_release_and_claim_next_handoff(db_path: str):
    reg = SQLiteStorage(db_path)
    a = _mk(reg, task_id="t1", scheduler_id="sched-h", now=T0)
    b = _mk(reg, task_id="t2", scheduler_id="sched-h", now=_iso_plus(T0, 1))
    reg.claim(a.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    terminal, nxt = reg.release_and_claim_next(
        a.work_id, owner_worker_id="w1", status=SchedulerWorkStatus.COMPLETED,
        error=None, scheduler_id="sched-h", worker_id="w1",
        lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
    assert terminal.status == SchedulerWorkStatus.COMPLETED
    assert nxt is not None and nxt.work_id == b.work_id
    assert nxt.status == SchedulerWorkStatus.RUNNING
    assert reg.get_work(a.work_id).status == SchedulerWorkStatus.COMPLETED
    reg.close()


def test_release_and_claim_next_requires_owner(db_path: str):
    reg = SQLiteStorage(db_path)
    a = _mk(reg, scheduler_id="sched-h", now=T0)
    reg.claim(a.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    with pytest.raises(SchedulerStateError):
        reg.release_and_claim_next(
            a.work_id, owner_worker_id="w-evil", status=SchedulerWorkStatus.COMPLETED,
            error=None, scheduler_id="sched-h", worker_id="w1",
            lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
    # nothing changed: still RUNNING, owned by w1
    assert reg.get_work(a.work_id).status == SchedulerWorkStatus.RUNNING
    assert reg.get_work(a.work_id).worker_id == "w1"
    reg.close()


def test_release_and_claim_next_no_work_returns_none(db_path: str):
    reg = SQLiteStorage(db_path)
    a = _mk(reg, scheduler_id="sched-h", now=T0)
    reg.claim(a.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    terminal, nxt = reg.release_and_claim_next(
        a.work_id, owner_worker_id="w1", status=SchedulerWorkStatus.FAILED,
        error="boom", scheduler_id="sched-h", worker_id="w1",
        lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
    assert terminal.status == SchedulerWorkStatus.FAILED
    assert terminal.error == "boom"
    assert nxt is None
    reg.close()


def test_handoff_race_exactly_one_winner(db_path: str):
    """Two processes race to hand off the same running row: only the true
    owner succeeds, and the handoff claims the next item exactly once."""
    reg = SQLiteStorage(db_path)
    reg2 = SQLiteStorage(db_path)
    a = _mk(reg, scheduler_id="sched-h", now=T0)
    b = _mk(reg, task_id="t2", scheduler_id="sched-h", now=_iso_plus(T0, 1))
    reg.claim(a.work_id, worker_id="w1", lease_seconds=60.0, now=T0,
              max_lease_seconds=600.0)
    outcomes = []

    def race(store, owner):
        try:
            _, nxt = store.release_and_claim_next(
                a.work_id, owner_worker_id=owner, status=SchedulerWorkStatus.COMPLETED,
                error=None, scheduler_id="sched-h", worker_id=owner,
                lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
            outcomes.append((owner, nxt.work_id if nxt else None))
        except SchedulerStateError:
            outcomes.append((owner, "REJECTED"))

    threads = [threading.Thread(target=race, args=(reg, "w1")),
               threading.Thread(target=race, args=(reg2, "w-evil"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(outcomes) == [("w-evil", "REJECTED"), ("w1", b.work_id)]
    assert reg.get_work(b.work_id).status == SchedulerWorkStatus.RUNNING
    assert reg.get_work(b.work_id).worker_id == "w1"
    reg.close()
    reg2.close()


# --------------------------------------------------------------------------- #
# global cross-process capacity
# --------------------------------------------------------------------------- #


def test_global_capacity_config(db_path: str):
    reg = SQLiteStorage(db_path)
    assert reg.get_scheduler_global_max() is None
    reg.set_scheduler_global_max(2)
    assert reg.get_scheduler_global_max() == 2
    reg.set_scheduler_global_max(4)
    assert reg.get_scheduler_global_max() == 4
    with pytest.raises(SchedulerRegistryError):
        reg.set_scheduler_global_max(0)
    reg.close()


def test_claim_next_enforces_global_capacity(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    a = _mk(reg, task_id="t1", scheduler_id="sched-a", now=T0)
    b = _mk(reg, task_id="t2", scheduler_id="sched-a", now=_iso_plus(T0, 1))
    c = _mk(reg, task_id="t3", scheduler_id="sched-b", now=_iso_plus(T0, 2))
    assert reg.claim_next("sched-a", "w1", 60.0, _iso_plus(T0, 3), 600.0).work_id == a.work_id
    assert reg.claim_next("sched-b", "w2", 60.0, _iso_plus(T0, 3), 600.0).work_id == c.work_id
    # capacity (2) exhausted: no further claim from ANY scheduler
    assert reg.claim_next("sched-a", "w1", 60.0, _iso_plus(T0, 3), 600.0) is None
    assert reg.get_work(b.work_id).status == SchedulerWorkStatus.QUEUED
    reg.close()


def test_fair_share_prevents_scheduler_monopoly(db_path: str):
    """With two schedulers holding queued work and global cap 2, the fair
    share (ceil(2/2)=1) prevents one scheduler from claiming both slots: a
    peer's queued step gets the next free slot."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    a1 = _mk(reg, task_id="t1", scheduler_id="sched-a", now=T0)
    a2 = _mk(reg, task_id="t2", scheduler_id="sched-a", now=_iso_plus(T0, 1))
    b1 = _mk(reg, task_id="t3", scheduler_id="sched-b", now=_iso_plus(T0, 2))
    # A claims its first row
    assert reg.claim(a1.work_id, "w-a", 60.0, _iso_plus(T0, 3), 600.0,
                     scheduler_id="sched-a") is not None
    # A cannot claim its second row while B has queued work (fair share 1)
    assert reg.claim(a2.work_id, "w-a", 60.0, _iso_plus(T0, 3), 600.0,
                     scheduler_id="sched-a") is None
    assert reg.get_work(a2.work_id).status == SchedulerWorkStatus.QUEUED
    # B gets the next free slot
    assert reg.claim(b1.work_id, "w-b", 60.0, _iso_plus(T0, 3), 600.0,
                     scheduler_id="sched-b") is not None
    # once B's work completes, A may use the freed capacity (share recalculates)
    reg.mark_terminal(b1.work_id, SchedulerWorkStatus.COMPLETED,
                      owner_worker_id="w-b", now=_iso_plus(T0, 4))
    assert reg.claim(a2.work_id, "w-a", 60.0, _iso_plus(T0, 4), 600.0,
                     scheduler_id="sched-a") is not None
    reg.close()


def test_single_active_scheduler_uses_full_cap(db_path: str):
    """With only ONE scheduler holding work, the full cap is available
    (ADR-025 behavior preserved under the fair-share rule)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    a1 = _mk(reg, task_id="t1", scheduler_id="sched-a", now=T0)
    a2 = _mk(reg, task_id="t2", scheduler_id="sched-a", now=_iso_plus(T0, 1))
    assert reg.claim(a1.work_id, "w-a", 60.0, _iso_plus(T0, 2), 600.0,
                     scheduler_id="sched-a") is not None
    assert reg.claim(a2.work_id, "w-a", 60.0, _iso_plus(T0, 2), 600.0,
                     scheduler_id="sched-a") is not None
    assert reg.get_work(a2.work_id).status == SchedulerWorkStatus.RUNNING
    reg.close()


def test_expired_running_reclaimed_in_claim_transaction_frees_capacity(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(1)
    a = _mk(reg, task_id="t1", scheduler_id="sched-a", now=T0)
    b = _mk(reg, task_id="t2", scheduler_id="sched-a", now=_iso_plus(T0, 1))
    reg.claim_next("sched-a", "w1", 10.0, T0, 600.0)  # lease expires at T0+10
    # at T0+11 the running lease expired: the claim transaction reclaims it
    # lazily and the next claim succeeds (capacity freed)
    got = reg.claim_next("sched-a", "w1", 60.0, _iso_plus(T0, 11), 600.0)
    assert got is not None and got.work_id == b.work_id
    assert reg.get_work(a.work_id).status == SchedulerWorkStatus.ABANDONED
    reg.close()


# --------------------------------------------------------------------------- #
# abandon_foreign_queued keyed on registration liveness
# --------------------------------------------------------------------------- #


def test_foreign_queued_abandoned_only_when_registration_dead(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.register_scheduler("sched-live", pid=1, lease_seconds=3600.0, now=T0)
    live = _mk(reg, task_id="t1", scheduler_id="sched-live", now=T0)
    dead = _mk(reg, task_id="t2", scheduler_id="sched-dead", now=_iso_plus(T0, 1))
    # a third engine starts: only the DEAD scheduler's queue is abandoned
    count = reg.abandon_foreign_queued("sched-mine", now=_iso_plus(T0, 2))
    assert count == 1
    assert reg.get_work(live.work_id).status == SchedulerWorkStatus.QUEUED
    assert reg.get_work(dead.work_id).status == SchedulerWorkStatus.ABANDONED
    # once the live registration expires, its queue becomes abandonable
    count2 = reg.abandon_foreign_queued("sched-mine", now=_iso_plus(T0, 3601))
    assert count2 == 1
    assert reg.get_work(live.work_id).status == SchedulerWorkStatus.ABANDONED
    reg.close()


def test_unregistered_foreign_queue_abandoned(db_path: str):
    reg = SQLiteStorage(db_path)
    row = _mk(reg, scheduler_id="sched-never-registered", now=T0)
    assert reg.abandon_foreign_queued("sched-mine", now=_iso_plus(T0, 1)) == 1
    assert reg.get_work(row.work_id).status == SchedulerWorkStatus.ABANDONED
    reg.close()


def test_list_work_step_index_filter(db_path: str):
    reg = SQLiteStorage(db_path)
    _mk(reg, task_id="t1", step_index=0, now=T0)
    _mk(reg, task_id="t1", step_index=1, now=_iso_plus(T0, 1))
    rows = reg.list_work(task_id="t1", step_index=1)
    assert len(rows) == 1 and rows[0].step_index == 1
    reg.close()
