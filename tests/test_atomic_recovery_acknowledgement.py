"""ADR-043: recovery acknowledgement is a conditional durable transition."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Barrier, Thread

import pytest

from arion.state.recovery import (
    MutationRecovery,
    RecoveryError,
    RecoveryStatus,
)
from arion.state.store import SQLiteStorage
from tests.test_task_lifecycle_fencing import _CountRead, _engine


def _record(identifier: str = "recovery-test") -> MutationRecovery:
    return MutationRecovery(
        recovery_id=identifier,
        task_id="task-test",
        goal_id=None,
        step_index=0,
        capability="test.mutate",
        action="mutate",
        resource=None,
        reason="uncertain mutation",
    )


def test_concurrent_acknowledgement_has_one_winner_and_event(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ack-race.db"
    left = _engine(db, _CountRead(), "lifecycle:read")
    right = _engine(db, _CountRead(), "lifecycle:read")
    recovery = left.storage.create_recovery(_record())
    barrier = Barrier(2)
    left_transition = left.storage.transition_recovery
    right_transition = right.storage.transition_recovery

    def left_at_barrier(record, expected):
        barrier.wait()
        return left_transition(record, expected)

    def right_at_barrier(record, expected):
        barrier.wait()
        return right_transition(record, expected)

    left.storage.transition_recovery = left_at_barrier
    right.storage.transition_recovery = right_at_barrier
    winners: list[str] = []
    errors: list[BaseException] = []

    def acknowledge(engine, actor: str) -> None:
        try:
            engine.acknowledge_recovery(recovery.recovery_id, actor=actor)
            winners.append(actor)
        except BaseException as exc:
            errors.append(exc)

    first = Thread(target=acknowledge, args=(left, "operator:left"))
    second = Thread(target=acknowledge, args=(right, "operator:right"))
    first.start(); second.start(); first.join(); second.join()

    durable = left.storage.get_recovery(recovery.recovery_id)
    events = [
        event for event in left.storage.list_events()
        if event.kind == "recovery.acknowledged"
    ]
    assert len(winners) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RecoveryError)
    assert "already acknowledged" in str(errors[0])
    assert durable.status is RecoveryStatus.ACKNOWLEDGED
    assert durable.acknowledged_by == winners[0]
    assert len(events) == 1
    assert events[0].detail["acknowledged_by"] == winners[0]
    left.shutdown(); right.shutdown()
    left.storage.close(); right.storage.close()


def test_stale_required_snapshot_cannot_reverse_acknowledgement(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stale-required.db"
    engine = _engine(db, _CountRead(), "lifecycle:read")
    stale_store = SQLiteStorage(db)
    recovery = engine.storage.create_recovery(_record())
    stale = stale_store.get_recovery(recovery.recovery_id)

    acknowledged = engine.acknowledge_recovery(
        recovery.recovery_id, actor="operator:alice"
    )
    stale.reason = "stale metadata refresh"
    with pytest.raises(RecoveryError, match="stale recovery status"):
        stale_store.update_recovery(stale)

    durable = engine.storage.get_recovery(recovery.recovery_id)
    assert acknowledged.status is RecoveryStatus.ACKNOWLEDGED
    assert durable.status is RecoveryStatus.ACKNOWLEDGED
    assert durable.acknowledged_by == "operator:alice"
    assert durable.reason == "uncertain mutation"
    engine.shutdown(); engine.storage.close(); stale_store.close()


def test_same_status_metadata_refresh_remains_compatible(db_path: str) -> None:
    storage = SQLiteStorage(db_path)
    recovery = storage.create_recovery(_record())
    recovery.reason = "bounded operator note"
    recovery.acknowledged_by = "forged metadata"

    storage.update_recovery(recovery)

    durable = storage.get_recovery(recovery.recovery_id)
    assert durable.status is RecoveryStatus.REQUIRED
    assert durable.reason == "bounded operator note"
    assert durable.acknowledged_by is None
    storage.close()


def test_forged_target_status_cannot_bypass_transition_api(db_path: str) -> None:
    storage = SQLiteStorage(db_path)
    recovery = storage.create_recovery(_record())
    forged = MutationRecovery.from_dict(recovery.to_dict())
    forged.status = RecoveryStatus.ACKNOWLEDGED
    forged.acknowledged_by = "forged"

    with pytest.raises(RecoveryError, match="stale recovery status"):
        storage.update_recovery(forged)

    durable = storage.get_recovery(recovery.recovery_id)
    assert durable.status is RecoveryStatus.REQUIRED
    assert durable.acknowledged_by is None
    storage.close()


def test_sqlite_abort_rolls_back_acknowledgement_and_event(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path / "ack-abort.db", _CountRead(), "lifecycle:read"
    )
    recovery = engine.storage.create_recovery(_record())
    engine.storage._conn.execute(
        "CREATE TRIGGER abort_recovery_ack BEFORE UPDATE OF status "
        "ON mutation_recoveries WHEN NEW.status='acknowledged' "
        "BEGIN SELECT RAISE(ABORT, 'ack unavailable'); END"
    )
    engine.storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        engine.acknowledge_recovery(
            recovery.recovery_id, actor="operator:alice"
        )

    durable = engine.storage.get_recovery(recovery.recovery_id)
    events = [
        event for event in engine.storage.list_events()
        if event.kind == "recovery.acknowledged"
    ]
    assert durable.status is RecoveryStatus.REQUIRED
    assert durable.acknowledged_at is None
    assert durable.acknowledged_by is None
    assert events == []
    engine.shutdown(); engine.storage.close()
