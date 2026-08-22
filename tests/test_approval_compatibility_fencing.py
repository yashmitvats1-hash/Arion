"""ADR-044: compatibility approval writes cannot change authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arion.orchestration.authz import ApprovalOutcome
from arion.state.approvals import ApprovalError, ApprovalStatus
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage
from tests.test_atomic_approval import _close, _pending


def test_stale_pending_snapshot_cannot_reverse_atomic_approval(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stale-pending.db"
    engine, storage, capability, cognitive, task, request, _ = _pending(db)
    stale_store = SQLiteStorage(db)
    stale = stale_store.get_request(request.approval_id)

    engine.resolve_approval_request(
        request.approval_id,
        ApprovalOutcome.APPROVED,
        actor="operator:alice",
    )
    stale.summary = "stale pending refresh"
    with pytest.raises(ApprovalError, match="stale approval status"):
        stale_store.update_request(stale)

    durable = storage.get_request(request.approval_id)
    assert durable.status is ApprovalStatus.APPROVED
    assert durable.decision_actor == "operator:alice"
    assert storage.load_task(task.id).status is TaskStatus.RUNNING
    completed = engine.run_task(task.id)
    assert completed.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    stale_store.close(); _close(engine, storage, cognitive)


def test_forged_approved_object_cannot_bypass_update_api(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "forged-update.db"
    )
    request.status = ApprovalStatus.APPROVED
    request.decision_actor = "forged:writer"

    with pytest.raises(ApprovalError, match="stale approval status"):
        storage.update_request(request)

    durable = storage.get_request(request.approval_id)
    assert durable.status is ApprovalStatus.PENDING
    assert durable.decision_actor is None
    assert storage.load_task(task.id).status is TaskStatus.AWAITING_APPROVAL
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_request_only_transition_rejects_approved(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "forged-transition.db"
    )
    request.status = ApprovalStatus.APPROVED
    request.decision_actor = "forged:writer"

    with pytest.raises(ApprovalError, match="APPROVED requires atomic"):
        storage.transition_request(request, ApprovalStatus.PENDING)

    assert storage.get_request(request.approval_id).status is ApprovalStatus.PENDING
    assert storage.load_task(task.id).status is TaskStatus.AWAITING_APPROVAL
    assert capability.calls == 0
    _close(engine, storage, cognitive)


def test_same_status_summary_refresh_remains_compatible(
    tmp_path: Path,
) -> None:
    engine, storage, _, cognitive, _, request, _ = _pending(
        tmp_path / "summary.db"
    )
    request.summary = "updated bounded summary"
    request.decision_actor = "forged:ignored"

    storage.update_request(request)

    durable = storage.get_request(request.approval_id)
    assert durable.status is ApprovalStatus.PENDING
    assert durable.summary == "updated bounded summary"
    assert durable.decision_actor is None
    assert durable.decided_at is None
    _close(engine, storage, cognitive)


@pytest.mark.parametrize(
    "target",
    [ApprovalStatus.DENIED, ApprovalStatus.EXPIRED],
)
def test_request_only_cleanup_transitions_remain_valid(
    tmp_path: Path,
    target: ApprovalStatus,
) -> None:
    engine, storage, _, cognitive, _, request, _ = _pending(
        tmp_path / f"cleanup-{target.value}.db"
    )
    request.status = target
    request.decision_actor = "system:cleanup"
    request.decided_at = "2026-08-22T00:00:00+00:00"
    if target is ApprovalStatus.EXPIRED:
        request.expired_at = request.decided_at

    assert storage.transition_request(request, ApprovalStatus.PENDING)

    durable = storage.get_request(request.approval_id)
    assert durable.status is target
    assert durable.decision_actor == "system:cleanup"
    _close(engine, storage, cognitive)


def test_raw_legacy_approved_awaiting_row_still_reconciles(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive, task, request, _ = _pending(
        tmp_path / "legacy-approved.db"
    )
    # Historical pre-ADR-038 shape is data compatibility, not a current write
    # API. Inject it as a persisted legacy row and verify repair remains.
    storage._conn.execute(
        "UPDATE approval_requests SET status='approved', "
        "decision_actor='legacy-approver', decided_at=? "
        "WHERE approval_id=?",
        ("2026-08-22T00:00:00+00:00", request.approval_id),
    )
    storage._conn.commit()

    resolved = engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED,
        actor="operator:retry",
    )
    resumed = engine.run_task(task.id)

    assert resolved.status is ApprovalStatus.APPROVED
    assert resumed.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    mirror = next(
        record for record in resumed.approvals
        if record.get("approval_id") == request.approval_id
    )
    assert mirror["resolved_by"] == "legacy-approver"
    _close(engine, storage, cognitive)


def test_sqlite_abort_preserves_pending_summary_and_status(
    tmp_path: Path,
) -> None:
    engine, storage, _, cognitive, _, request, _ = _pending(
        tmp_path / "summary-abort.db"
    )
    original_summary = request.summary
    original_updated_at = request.updated_at
    request.summary = "new summary"
    storage._conn.execute(
        "CREATE TRIGGER abort_approval_summary BEFORE UPDATE OF summary "
        "ON approval_requests BEGIN SELECT RAISE(ABORT, 'summary unavailable'); END"
    )
    storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        storage.update_request(request)

    durable = storage.get_request(request.approval_id)
    assert durable.status is ApprovalStatus.PENDING
    assert durable.summary == original_summary
    assert durable.decision_actor is None
    assert request.updated_at == original_updated_at
    _close(engine, storage, cognitive)
