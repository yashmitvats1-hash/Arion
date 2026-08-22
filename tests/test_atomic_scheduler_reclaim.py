"""ADR-042: single-row scheduler reclaim is atomically lease-fenced."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arion.interfaces.cli import _scheduler_command
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-08-22T00:00:00+00:00"
BEFORE_EXPIRY = "2026-08-22T00:00:09+00:00"
AFTER_OLD_EXPIRY = "2026-08-22T00:00:11+00:00"


def _running(store: SQLiteStorage, *, lease: float = 10.0):
    row = store.create(
        task_id="task-shared",
        goal_id="goal-shared",
        step_index=0,
        scheduler_id="scheduler-owner",
        now=T0,
    )
    return store.claim(
        row.work_id,
        worker_id="worker-owner",
        lease_seconds=lease,
        max_lease_seconds=120,
        scheduler_id="scheduler-owner",
        now=T0,
    )


def test_generic_abandon_rejects_live_running_row(db_path: str) -> None:
    store = SQLiteStorage(db_path)
    row = _running(store)

    with pytest.raises(SchedulerStateError, match="still valid"):
        store.mark_terminal(
            row.work_id,
            SchedulerWorkStatus.ABANDONED,
            now=BEFORE_EXPIRY,
        )

    durable = store.get_work(row.work_id)
    assert durable.status is SchedulerWorkStatus.RUNNING
    assert durable.worker_id == "worker-owner"
    store.close()


def test_atomic_single_reclaim_requires_expired_running_lease(
    db_path: str,
) -> None:
    store = SQLiteStorage(db_path)
    row = _running(store)

    with pytest.raises(SchedulerStateError, match="still valid"):
        store.reclaim_work(row.work_id, now=BEFORE_EXPIRY)
    reclaimed = store.reclaim_work(row.work_id, now=AFTER_OLD_EXPIRY)

    assert reclaimed.status is SchedulerWorkStatus.ABANDONED
    events = store.scheduler_events(work_id=row.work_id)
    reclaimed_events = [event for event in events if event.kind == "work.reclaimed"]
    assert len(reclaimed_events) == 1
    assert reclaimed_events[0].detail["reason"] == "lease_expired"
    with pytest.raises(SchedulerStateError, match="only RUNNING"):
        store.reclaim_work(row.work_id, now=AFTER_OLD_EXPIRY)
    store.close()


def test_renewal_between_stale_view_and_reclaim_wins(
    tmp_path,
) -> None:
    db = tmp_path / "renew-race.db"
    reclaimer = SQLiteStorage(db)
    owner = SQLiteStorage(db)
    row = _running(reclaimer)
    stale_view = reclaimer.get_work(row.work_id)
    assert stale_view.lease_expires_at == "2026-08-22T00:00:10+00:00"

    renewed = owner.heartbeat(
        row.work_id,
        "worker-owner",
        lease_seconds=60,
        max_lease_seconds=120,
        now=BEFORE_EXPIRY,
    )
    assert renewed.lease_expires_at == "2026-08-22T00:01:09+00:00"

    with pytest.raises(SchedulerStateError, match="still valid"):
        reclaimer.reclaim_work(row.work_id, now=AFTER_OLD_EXPIRY)

    durable = owner.get_work(row.work_id)
    assert durable.status is SchedulerWorkStatus.RUNNING
    assert durable.lease_expires_at == renewed.lease_expires_at
    reclaimer.close(); owner.close()


def test_cli_reclaim_uses_atomic_store_decision(tmp_path, capsys) -> None:
    db = tmp_path / "cli-race.db"
    cli_store = SQLiteStorage(db)
    owner = SQLiteStorage(db)
    row = _running(cli_store)

    class RenewBeforeAtomicReclaim:
        def reclaim_work(self, work_id, now=None):
            owner.heartbeat(
                work_id,
                "worker-owner",
                lease_seconds=60,
                max_lease_seconds=120,
                now=BEFORE_EXPIRY,
            )
            return cli_store.reclaim_work(work_id, now=now)

        def __getattr__(self, name):
            return getattr(cli_store, name)

    engine = SimpleNamespace(
        scheduler_registry=RenewBeforeAtomicReclaim(),
        _lock_now=lambda: AFTER_OLD_EXPIRY,
    )
    args = SimpleNamespace(
        scheduler_command="reclaim",
        work_id=row.work_id,
        json=False,
    )

    rc = _scheduler_command(args, engine)

    assert rc == 1
    assert "still valid" in capsys.readouterr().out
    assert owner.get_work(row.work_id).status is SchedulerWorkStatus.RUNNING
    cli_store.close(); owner.close()


@pytest.mark.parametrize(
    "terminal",
    [SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED],
)
def test_expired_owner_cannot_complete_or_fail(
    db_path: str, terminal: SchedulerWorkStatus,
) -> None:
    store = SQLiteStorage(db_path)
    row = _running(store)

    with pytest.raises(SchedulerStateError, match="lease expired"):
        store.mark_terminal(
            row.work_id,
            terminal,
            owner_worker_id="worker-owner",
            now=AFTER_OLD_EXPIRY,
        )

    assert store.get_work(row.work_id).status is SchedulerWorkStatus.RUNNING
    store.close()


def test_live_owner_completion_remains_compatible(db_path: str) -> None:
    store = SQLiteStorage(db_path)
    row = _running(store)

    completed = store.mark_terminal(
        row.work_id,
        SchedulerWorkStatus.COMPLETED,
        owner_worker_id="worker-owner",
        now=BEFORE_EXPIRY,
    )

    assert completed.status is SchedulerWorkStatus.COMPLETED
    store.close()


def test_expired_handoff_cannot_complete_or_claim_next(db_path: str) -> None:
    store = SQLiteStorage(db_path)
    current = _running(store)
    queued = store.create(
        task_id="task-next",
        goal_id="goal-shared",
        step_index=0,
        scheduler_id="scheduler-owner",
        now=T0,
    )

    with pytest.raises(SchedulerStateError, match="lease expired"):
        store.release_and_claim_next(
            current.work_id,
            owner_worker_id="worker-owner",
            status=SchedulerWorkStatus.COMPLETED,
            error=None,
            scheduler_id="scheduler-owner",
            worker_id="worker-owner",
            lease_seconds=60,
            now=AFTER_OLD_EXPIRY,
            max_lease_seconds=120,
        )

    assert store.get_work(current.work_id).status is SchedulerWorkStatus.RUNNING
    assert store.get_work(queued.work_id).status is SchedulerWorkStatus.QUEUED
    store.close()


def test_queued_abandonment_remains_compatible(db_path: str) -> None:
    store = SQLiteStorage(db_path)
    queued = store.create(
        task_id="task-queued",
        goal_id="goal-queued",
        step_index=0,
        scheduler_id="scheduler-dead",
        now=T0,
    )

    abandoned = store.mark_terminal(
        queued.work_id, SchedulerWorkStatus.ABANDONED,
        now=AFTER_OLD_EXPIRY,
    )

    assert abandoned.status is SchedulerWorkStatus.ABANDONED
    store.close()
