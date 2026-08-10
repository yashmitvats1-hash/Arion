"""Engine-level mutation lock integration (ADR-021, Phases C/D/E/F/G/H).

Required ordering:
    plan -> authorization -> approval if required -> live re-authorization
        -> acquire mutation lock -> mutate -> verify -> release lock

- a lock is never acquired when authorization fails / approval is pending /
  an approval is stale;
- on contention the capability NEVER executes, the task fails durably
  (never falsely completed), contention is audited, the goal is durably
  BLOCKED (lock_contention) with no duplicate approvals and no recovery;
- locks are released on EVERY terminal path (success, mutation failure,
  verification failure);
- a crashed owner's stale lock is reclaimable via leases;
- an approval never implies lock ownership; recovery never clears/transfers
  locks;
- write and append share the same canonical resource lock; different
  resources lock independently; a lock never grants authorization.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
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
from arion.state.locks import MutationLockError, canonical_resource
from arion.state.models import (
    GoalStatus,
    PlanStep,
    StepStatus,
    TaskStatus,
    VerificationPolicy,
)
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


class AppendPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="append notes", capability="filesystem.append", action="append",
                     scope="filesystem:write",
                     params={"path": "notes.txt", "content": " world", "create": False},
                     verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


class FailingWrite(FilesystemWriteCapability):
    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        super().execute(action, dict(params))  # mutate, then fail
        raise CapabilityError("fsync failed after write")


class BadVerifyWrite(FilesystemWriteCapability):
    """Writes correctly but lies about the size -> verification fails."""

    def execute(self, action, params):
        out = super().execute(action, dict(params))
        out["size"] += 1
        return out


def _policy(allowed_scopes=None, boundaries=None):
    return ResourcePolicy(
        allowed_scopes=allowed_scopes if allowed_scopes is not None else {"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries=boundaries if boundaries is not None else {FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, planner=None, write_cap=None, append_cap=None,
            policy=None, lease_seconds=300.0, clock=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(write_cap or FilesystemWriteCapability(sandbox))
    registry.register(append_cap or FilesystemAppendCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = planner or WritePlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy or _policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        mutation_lock_lease_seconds=lease_seconds, lock_clock=clock,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _approve(engine, gid):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")


# ---------------------------------------------------------------------------
# Phase C: ordering - authorization BEFORE locking
# ---------------------------------------------------------------------------


def test_denied_scope_never_acquires_lock(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "deny.db", sb,
                                     policy=_policy(allowed_scopes={"filesystem:read"}))
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and "not permitted" in (task.error or "")
    assert engine.mutation_lock_store.list() == []  # no lock ever acquired
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.requested" not in kinds
    assert "permission.denied" in kinds
    assert not (sb / "notes.txt").exists()  # no mutation
    engine.storage.close()


def test_approval_pending_never_acquires_lock(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "pend.db", sb)
    gid = engine.submit_goal("write notes").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # approval pending
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert engine.mutation_lock_store.list() == []
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.requested" not in kinds
    engine.storage.close()


def test_stale_approval_never_acquires_lock(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, registry = _engine(tmp_path / "stale.db", sb)
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    # security-relevant change after approval: overwrite flips
    task = gm.task_history(gid)[-1]
    task.steps[0].params["overwrite"] = True
    engine.storage.save_task(task)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # fresh approval demanded
    assert engine.mutation_lock_store.list() == []
    assert "mutation.lock.requested" not in [e.kind for e in storage.list_events()]
    assert not (sb / "notes.txt").exists()  # no mutation
    engine.storage.close()


# ---------------------------------------------------------------------------
# Phase C: contention behavior
# ---------------------------------------------------------------------------


def test_contention_capability_never_executes_and_task_fails_durably(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "cont.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)

    # process B holds the lock on the same canonical resource
    engine_b, _, _, _ = _engine(db, sb)
    b_lock = engine_b.mutation_lock_store.acquire(
        FS, canonical_resource(FS, "notes.txt"), "filesystem.write", "write",
        "proc-b", lease_seconds=300, now=_now())
    assert b_lock is not None

    final = engine_a.run_goal(gid)
    task = gm_a.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "locked" in (task.error or "").lower()
    assert final.status == GoalStatus.BLOCKED  # durable lock_contention blocker
    assert any(b.get("type") == "lock_contention" for b in final.blockers)
    # the capability never executed
    assert not (sb / "notes.txt").exists()
    kinds = [e.kind for e in storage_a.list_events()]
    assert "mutation.lock.contended" in kinds
    assert "mutation.attempted" not in kinds  # NO capability execution
    assert "mutation.requires_recovery" not in kinds  # contention is not a mutation failure
    assert kinds.count("approval.requested") == 1  # no duplicate approval requests
    engine_a.storage.close()
    engine_b.storage.close()


def test_no_silent_retry_after_contention(tmp_path):
    """While the lock is held, run_goal stays BLOCKED without replanning."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "noretry.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    engine_b, _, _, _ = _engine(db, sb)
    engine_b.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write", "write",
                                         "proc-b", 300, now=_now())
    engine_a.run_goal(gid)
    versions_before = len(gm_a.plan_history(gid))
    for _ in range(3):
        assert engine_a.run_goal(gid).status == GoalStatus.BLOCKED
    assert len(gm_a.plan_history(gid)) == versions_before  # no replan loop
    assert len(engine_a.approval_store.list_requests()) == 1  # no duplicate approvals
    engine_a.storage.close()
    engine_b.storage.close()


def test_after_release_other_process_proceeds_via_own_authorization(tmp_path):
    """B releases; A replans, re-authorizes, acquires, mutates exactly once."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "free.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    engine_b, _, _, _ = _engine(db, sb)
    b_lock = engine_b.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write",
                                                  "write", "proc-b", 300, now=_now())
    assert engine_a.run_goal(gid).status == GoalStatus.BLOCKED

    engine_b.mutation_lock_store.release(b_lock.lock_id, "proc-b")
    # the goal unblocks (lock gone) and replans -> a NEW task needs its OWN
    # fresh authorization before it may lock and mutate
    assert engine_a.run_goal(gid).status == GoalStatus.BLOCKED  # fresh approval queued
    fresh = engine_a.approval_store.list_requests()[-1]
    assert fresh.task_id != gm_a.task_history(gid)[-2].id
    engine_a.resolve_approval_request(fresh.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine_a.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert engine_a.mutation_lock_store.list() == []  # released after success
    kinds = [e.kind for e in storage_a.list_events()]
    assert kinds.count("mutation.lock.acquired") == 1
    assert kinds.count("mutation.lock.released") == 1
    assert kinds.count("mutation.attempted") == 1
    engine_a.storage.close()
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# Phase D: release on every terminal path
# ---------------------------------------------------------------------------


def test_success_releases_lock(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "ok.db", sb)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    assert gm.get_goal(gid).status == GoalStatus.COMPLETED
    assert engine.mutation_lock_store.list() == []  # released
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.acquired" in kinds and "mutation.lock.released" in kinds
    engine.storage.close()


def test_mutation_failure_releases_lock_and_records_recovery(tmp_path):
    sb = _sandbox(tmp_path)
    fail_cap = FailingWrite(sb)
    engine, gm, storage, _ = _engine(tmp_path / "mf.db", sb, write_cap=fail_cap)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    assert gm.task_history(gid)[-1].status == TaskStatus.FAILED
    assert "recovery required" in (gm.task_history(gid)[-1].error or "")
    assert engine.mutation_lock_store.list() == []  # released on failure
    rec = engine.recovery_store.list_recoveries()[0]
    assert rec.status.value == "required"
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.released" in kinds
    assert "mutation.lock.acquired" in kinds
    engine.storage.close()


def test_verification_failure_releases_lock(tmp_path):
    sb = _sandbox(tmp_path)
    bad = BadVerifyWrite(sb)
    engine, gm, storage, _ = _engine(tmp_path / "vf.db", sb, write_cap=bad)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and "verification" in (task.error or "")
    assert engine.mutation_lock_store.list() == []  # released on verification failure
    assert engine.recovery_store.list_recoveries()  # recovery required
    engine.storage.close()


def test_crashed_owner_reclaimable_after_expiry(tmp_path):
    """Process exits while holding the lock; another process can reclaim it
    after the lease expires (deterministic injectable clock)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "crash.db"
    now = {"t": _now()}

    engine_a, _, storage_a, _ = _engine(db, sb, lease_seconds=60, clock=lambda: now["t"])
    lock_a = engine_a.mutation_lock_store.acquire(
        FS, "notes.txt", "filesystem.write", "write", engine_a._lock_owner(), 60, now=now["t"])
    # process A crashes WITHOUT releasing
    engine_a.storage.close()

    # before expiry: process B cannot acquire, cannot reclaim
    engine_b, _, storage_b, _ = _engine(db, sb, lease_seconds=60, clock=lambda: now["t"])
    with pytest.raises(MutationLockError):
        engine_b.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write", "write",
                                             engine_b._lock_owner(), 60, now=now["t"])
    # after expiry: reclaim works and B acquires
    now["t"] = _plus(now["t"], 61)
    reclaimed = engine_b.reclaim_stale_locks()
    assert lock_a.lock_id in reclaimed
    final = engine_b.run_goal(engine_b.submit_goal("write notes").id)
    _approve(engine_b, final.id)  # B proceeds through ITS OWN authorization
    engine_b.run_goal(final.id)
    assert engine_b.mutation_lock_store.list() == []
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# Phase E: leases at engine level
# ---------------------------------------------------------------------------


def test_engine_lease_active_not_reclaimed_until_expiry(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "lease.db"
    now = {"t": _now()}
    engine, gm, storage, _ = _engine(db, sb, lease_seconds=60, clock=lambda: now["t"])
    lock = engine.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write", "write",
                                              "proc-a", 60, now=now["t"])
    # active: nothing reclaimed, still contended
    now["t"] = _plus(now["t"], 59)
    assert engine.reclaim_stale_locks() == []
    assert engine.mutation_lock_store.get(lock.lock_id) is not None
    # expired: reclaimed exactly once
    now["t"] = _plus(now["t"], 2)
    reclaimed = engine.reclaim_stale_locks()
    assert lock.lock_id in reclaimed
    assert engine.reclaim_stale_locks() == []  # idempotent
    engine.storage.close()


# ---------------------------------------------------------------------------
# Phase F: write + append matrix
# ---------------------------------------------------------------------------


def test_write_and_append_contend_on_same_resource(tmp_path):
    """filesystem.write and filesystem.append mutate the same file: their
    locks contend."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "wa.db"
    now = _now()
    engine_w, _, _, _ = _engine(db, sb)
    lock_w = engine_w.mutation_lock_store.acquire(
        FS, canonical_resource(FS, "notes.txt"), "filesystem.write", "write", "proc-w", 300, now=now)
    with pytest.raises(MutationLockError):
        engine_w.mutation_lock_store.acquire(
            FS, canonical_resource(FS, "notes.txt"), "filesystem.append", "append", "proc-a", 300, now=now)
    assert lock_w is not None
    engine_w.storage.close()


def test_different_resources_lock_independently(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "diff.db"
    engine, gm, storage, _ = _engine(db, sb)
    a = engine.mutation_lock_store.acquire(FS, "a.txt", "filesystem.write", "write", "p1", 300, now=_now())
    b = engine.mutation_lock_store.acquire(FS, "b.txt", "filesystem.write", "write", "p2", 300, now=_now())
    assert a.lock_id != b.lock_id
    assert len(engine.mutation_lock_store.list()) == 2
    engine.storage.close()


def test_lock_never_grants_authorization(tmp_path):
    """A owns a lock; B has NO authorization for the resource - B's permission
    check fails BEFORE the lock is even requested (lock is not permission)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "authz.db"
    engine_a, _, _, _ = _engine(db, sb)
    engine_a.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write", "write",
                                         "proc-a", 300, now=_now())
    engine_b, gm_b, storage_b, _ = _engine(db, sb, policy=_policy(allowed_scopes={"filesystem:read"}))
    gid = engine_b.submit_goal("write notes").id
    engine_b.run_goal(gid)
    task = gm_b.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and "not permitted" in (task.error or "")
    kinds = [e.kind for e in storage_b.list_events()]
    assert "mutation.lock.requested" not in kinds  # authorization failed first
    assert not (sb / "notes.txt").exists()
    engine_a.storage.close()
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# Phase G: approval never implies lock ownership
# ---------------------------------------------------------------------------


def test_approved_task_still_contends_when_lock_taken(tmp_path):
    """Approval granted while no lock exists; another process holds the lock;
    the approved task resumes -> live re-authorization ok -> lock acquisition
    still protects the mutation."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "g.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    gid = engine_a.submit_goal("write notes").id
    engine_a.run_goal(gid)
    req = engine_a.approval_store.list_requests()[-1]
    engine_a.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")

    # another process mutates (holds the lock) before A resumes
    engine_b, _, _, _ = _engine(db, sb)
    engine_b.mutation_lock_store.acquire(FS, "notes.txt", "filesystem.write", "write",
                                         "proc-b", 300, now=_now())

    final = engine_a.run_goal(gid)
    task = gm_a.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "locked" in (task.error or "").lower()
    assert final.status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()  # B never wrote; A never wrote
    kinds = [e.kind for e in storage_a.list_events()]
    assert kinds.count("approval.requested") == 1  # approval not duplicated
    assert "mutation.lock.contended" in kinds
    engine_a.storage.close()
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# Phase H: recovery interaction
# ---------------------------------------------------------------------------


def test_recovery_does_not_clear_or_transfer_lock(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "rec.db"
    fail_cap = FailingWrite(sb)
    engine_a, gm_a, storage_a, _ = _engine(db, sb, write_cap=fail_cap)
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    engine_a.run_goal(gid)
    # lock was released on the failure
    assert engine_a.mutation_lock_store.list() == []
    rec = engine_a.recovery_store.list_recoveries()[0]
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sb)  # restart
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED  # recovery gate
    assert engine_b.recovery_store.list_recoveries()[0].recovery_id == rec.recovery_id
    engine_b.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    # a future task must acquire a FRESH lock: acquire works (none held) and
    # is released on the next failure
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED  # fresh approval
    req = engine_b.approval_store.list_requests()[-1]
    engine_b.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_b.run_goal(gid)
    assert engine_b.mutation_lock_store.list() == []  # fresh lock released after failure
    fresh_task = gm_b.task_history(gid)[-1]
    kinds = [e.kind for e in storage_b.list_events() if e.task_id == fresh_task.id]
    assert kinds.count("mutation.lock.acquired") == 1
    assert kinds.count("mutation.lock.released") == 1
    engine_b.storage.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plus(iso: str, seconds: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()
