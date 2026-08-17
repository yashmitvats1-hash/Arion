"""Approval queue retention / expiry tests (ADR-019, item 8).

- configurable expiration for stale PENDING approvals;
- explicit EXPIRED state;
- expired requests cannot be approved or denied (resolved) - typed error;
- expiration is idempotent;
- expired requests remain auditable (queue record + approval.expired event);
- cleanup/pruning never deletes audit history;
- CLI exposes pending/expired/resolved state clearly.
"""

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.approvals import ApprovalError, ApprovalStatus
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class WritePlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="write notes", capability="filesystem.write", action="write",
                     scope="filesystem:write",
                     params={"path": "notes.txt", "content": "hello", "overwrite": False},
                     verification=VerificationPolicy("write_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, ttl_seconds=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = WritePlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=_policy(),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
        approval_ttl_seconds=ttl_seconds,
    )
    return engine, gm, storage


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _pending(db_path, sandbox, ttl_seconds=None):
    engine, gm, storage = _engine(db_path, sandbox, ttl_seconds=ttl_seconds)
    goal = engine.submit_goal("write notes")
    engine.run_goal(goal.id)
    req = engine.approval_store.list_requests()[0]
    return engine, gm, storage, goal.id, req


def test_expired_state_exists():
    assert ApprovalStatus.EXPIRED.value == "expired"


def test_expired_request_cannot_be_approved(tmp_path):
    db = tmp_path / "e.db"
    sb = _sandbox(tmp_path)
    engine, gm, storage, gid, req = _pending(db, sb, ttl_seconds=60)
    # time advances past expiry
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    req_b = engine.approval_store.get_request(req.approval_id)
    assert req_b.status == ApprovalStatus.EXPIRED
    assert req_b.expired_at is not None
    with pytest.raises(ApprovalError, match="expired"):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    with pytest.raises(ApprovalError, match="expired"):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.DENIED)
    # audit: the event was emitted
    assert "approval.expired" in [e.kind for e in storage.list_events()]
    # the awaiting task fails durably with an explainable error; the goal is
    # unblocked (a later run_goal replans for FRESH authorization)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "approval expired" in (task.error or "")
    assert gm.get_goal(gid).status == GoalStatus.ACTIVE
    # a fresh run requests new authorization instead of honoring the stale one
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # new request queued
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_expiration_is_idempotent(tmp_path):
    db = tmp_path / "i.db"
    sb = _sandbox(tmp_path)
    engine, gm, storage, gid, req = _pending(db, sb, ttl_seconds=60)
    first = engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    assert req.approval_id in first
    second = engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    assert second == []  # already expired: no-op
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.expired") == 1
    engine.storage.close()


def _add_seconds(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()


def test_non_expired_request_not_marked(tmp_path):
    db = tmp_path / "n.db"
    sb = _sandbox(tmp_path)
    engine, gm, storage, gid, req = _pending(db, sb, ttl_seconds=3600)
    assert engine.expire_stale_approvals(now=req.created_at) == []
    assert engine.approval_store.get_request(req.approval_id).status == ApprovalStatus.PENDING
    # 59 minutes later it is still valid (created_at + ttl not yet reached)
    assert engine.expire_stale_approvals(now=_add_seconds(req.created_at, 3599)) == []
    assert engine.approval_store.get_request(req.approval_id).status == ApprovalStatus.PENDING
    # past the TTL it expires exactly once
    assert engine.expire_stale_approvals(now=_add_seconds(req.created_at, 3601)) == [req.approval_id]
    assert engine.approval_store.get_request(req.approval_id).status == ApprovalStatus.EXPIRED
    engine.storage.close()


def test_ttl_based_expiry_uses_real_clock(tmp_path):
    """With a tiny TTL, a real-time wait lets the request expire."""
    import time

    db = tmp_path / "t.db"
    sb = _sandbox(tmp_path)
    engine, gm, storage, gid, req = _pending(db, sb, ttl_seconds=0.05)
    time.sleep(0.12)
    marked = engine.expire_stale_approvals()
    assert req.approval_id in marked
    assert engine.approval_store.get_request(req.approval_id).status == ApprovalStatus.EXPIRED
    engine.storage.close()


def test_expiry_preserves_audit_history(tmp_path):
    """Pruning/cleanup must never delete the audit trail."""
    db = tmp_path / "a.db"
    sb = _sandbox(tmp_path)
    engine, gm, storage, gid, req = _pending(db, sb, ttl_seconds=60)
    audit_before = {e.id for e in storage.list_events()}
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    events_after = storage.list_events()
    # NOTHING was deleted or pruned: every pre-existing audit event survives,
    # and the expiry + task-failure audit entries were ADDED
    assert audit_before <= {e.id for e in events_after}
    kinds = [e.kind for e in events_after]
    assert "approval.expired" in kinds
    assert "task.failed" in kinds
    assert all("deleted" not in k and "pruned" not in k for k in kinds)
    # the queue record remains (auditable), only its status changed
    assert engine.approval_store.get_request(req.approval_id) is not None
    engine.storage.close()


def test_expired_approval_survives_restart(tmp_path):
    db = tmp_path / "sr.db"
    sb = _sandbox(tmp_path)
    engine_a, _, _, gid, req = _pending(db, sb, ttl_seconds=60)
    engine_a.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    engine_a.storage.close()

    engine_b, gm_b, storage_b = _engine(db, sb, ttl_seconds=60)
    req_b = engine_b.approval_store.get_request(req.approval_id)
    assert req_b.status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError, match="expired"):
        engine_b.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# CLI exposure
# ---------------------------------------------------------------------------


def _run(argv, capsys):
    rc = cli_main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_cli_approvals_exposes_expired_state(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "arion.db")
    sb = _sandbox(tmp_path)
    engine, _, _, gid, req = _pending(db, sb, ttl_seconds=60)
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    engine.storage.close()

    rc, out = _run(["approvals", "list", "--json", "--db", db], capsys)
    data = json.loads(out)
    assert data[0]["status"] == "expired"
    assert data[0]["expired_at"] is not None

    rc, out = _run(["approvals", "list", "--status", "expired", "--db", db], capsys)
    assert req.approval_id in out

    rc, out = _run(["approvals", "show", req.approval_id, "--db", db], capsys)
    assert "expired" in out

    # approving an expired request fails closed with a clear message
    rc, out = _run(["approvals", "approve", req.approval_id, "--db", db], capsys)
    assert rc == 1
    assert "expired" in out.lower()
