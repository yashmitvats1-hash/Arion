"""Mutation recovery fencing tests (ADR-020, Phase A + E + F).

A failed non-retry-safe mutation must produce a DURABLE recovery-required
condition that:

- survives restart;
- cannot be silently retried by re-running the task;
- cannot be cleared by a planner, memory, reflection, or guidance;
- allows a fresh task/plan ONLY after an explicit, durable, audited,
  restart-safe recovery transition;
- never itself authorizes anything (authorization still runs independently
  for every new mutation);
- never touches approval records (no resurrecting expired/denied approvals);
- carries bounded metadata only (never file contents).

The advisory layer (Phase F) may TELL planners about recovery - but guidance
can never clear or bypass recovery enforcement (adversarial test included).
"""

import pytest

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
from arion.memory.models import Episode
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.approvals import ApprovalStatus
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
from arion.state.recovery import MutationRecovery, RecoveryError, RecoveryStatus
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


class FailingWriteCapability(FilesystemWriteCapability):
    """Injected non-retry-safe mutation failure AFTER the write."""

    def __init__(self, sandbox, mode="raise"):
        super().__init__(sandbox)
        self.mode = mode
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        if self.mode == "raise":
            raise CapabilityError("disk full")
        if self.mode == "partial":
            super().execute(action, dict(params))
            raise CapabilityError("fsync failed after write")
        return super().execute(action, dict(params))


class RecordingPlanner(WritePlanner):
    """Captures the planning context each time the planner is asked to plan."""

    def __init__(self):
        self.seen_contexts = []

    def plan(self, goal_description, task_id, registry, context=None):
        if context is not None:
            self.seen_contexts.append(
                [dict(r) for r in getattr(context, "recovery", [])]
            )
        return super().plan(goal_description, task_id, registry, context=context)


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, write_cap=None, planner=None, memory=False):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(write_cap or FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    memory_store = SQLiteMemoryStore(db_path) if memory else None
    planner = planner or WritePlanner()
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
        policy=_policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
        memory=memory_store,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _approve(engine, gid):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)


def _fail_mutation(tmp_path, mode="partial", memory=False, planner=None, db=None):
    """Queue -> approve -> run -> mutation fails -> returns (engine, gm, storage, registry, gid, recovery)."""
    sb = _sandbox(tmp_path)
    db = db or tmp_path / "r.db"
    fail_cap = FailingWriteCapability(sb, mode=mode)
    engine, gm, storage, registry = _engine(db, sb, write_cap=fail_cap, planner=planner, memory=memory)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    recovery = engine.recovery_store.list_recoveries()[0]
    return engine, gm, storage, registry, sb, gid, recovery


# ---------------------------------------------------------------------------
# 1-3. durable condition, restart, no silent retry
# ---------------------------------------------------------------------------


def test_failed_mutation_creates_durable_recovery_required(tmp_path):
    engine, gm, storage, registry, sb, gid, rec = _fail_mutation(tmp_path)
    assert rec.status == RecoveryStatus.REQUIRED
    assert rec.capability == "filesystem.write" and rec.action == "write"
    assert rec.resource == "notes.txt"
    assert "fsync failed after write" in rec.reason
    # the goal is durably BLOCKED with a recovery_required blocker
    goal = gm.get_goal(gid)
    assert goal.status == GoalStatus.BLOCKED
    assert any(b.get("type") == "recovery_required" for b in goal.blockers)
    # audited
    kinds = [e.kind for e in storage.list_events()]
    assert "recovery.required" in kinds
    assert "mutation.requires_recovery" in kinds
    engine.storage.close()


def test_recovery_survives_restart(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "rr.db"
    engine_a, gm_a, storage_a, _, _, gid, _ = _fail_mutation(tmp_path, db=db)
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sb)
    rec_b = engine_b.recovery_store.list_recoveries()[0]
    assert rec_b.status == RecoveryStatus.REQUIRED
    assert gm_b.get_goal(gid).status == GoalStatus.BLOCKED
    assert any(b.get("type") == "recovery_required" for b in gm_b.get_goal(gid).blockers)
    engine_b.storage.close()


def test_run_task_again_cannot_silently_retry(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _, _, gid, rec = _fail_mutation(tmp_path)
    task = gm.task_history(gid)[-1]
    # same task again: terminal FAILED, returned as-is, capability never called again
    again = engine.run_task(task.id)
    assert again.status == TaskStatus.FAILED
    kinds = [e.kind for e in storage.list_events() if e.task_id == task.id]
    assert kinds.count("mutation.attempted") == 1
    engine.storage.close()


# ---------------------------------------------------------------------------
# 4-5. planner / memory cannot clear recovery
# ---------------------------------------------------------------------------


def test_planner_cannot_clear_recovery_by_new_plan(tmp_path):
    planner = RecordingPlanner()
    engine, gm, storage, _, _, gid, rec = _fail_mutation(tmp_path, planner=planner)
    versions_before = len(gm.plan_history(gid))
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED
    assert len(gm.plan_history(gid)) == versions_before  # NO new plan version
    assert engine.recovery_store.get_recovery(rec.recovery_id).status == RecoveryStatus.REQUIRED
    assert planner.seen_contexts == []  # the planner was never even consulted
    engine.storage.close()


def test_memory_and_guidance_cannot_clear_recovery(tmp_path):
    """Poisoned memory claiming 'retry the failed write immediately' cannot
    clear recovery or cause a mutation (adversarial, Phase F)."""
    engine, gm, storage, _, sb, gid, rec = _fail_mutation(tmp_path, mode="raise", memory=True)
    # poison memory: an episode + reflection claiming immediate retry succeeded
    ep = Episode(episode_id="ep_retry", goal="write notes", outcome="completed",
                 task_id="t", plan_summary=[], actions=[], resources=[], tags=["filesystem.write"],
                 authorization={}, failures=[], recovery={"resumed": True, "re_executed": True},
                 importance=1.0)
    engine.memory.record_episode(ep)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # guidance is informational; gate holds
    assert engine.recovery_store.get_recovery(rec.recovery_id).status == RecoveryStatus.REQUIRED
    assert not (sb / "notes.txt").exists()  # NO mutation
    engine.storage.close()


# ---------------------------------------------------------------------------
# 6-7. explicit recovery transition
# ---------------------------------------------------------------------------


def test_fresh_plan_requires_explicit_recovery_transition(tmp_path):
    """A fresh task/plan may proceed ONLY after acknowledge_recovery; the new
    mutation then still needs FRESH authorization (recovery authorizes
    nothing)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "ack.db"
    engine_a, gm_a, storage_a, _, _, gid, rec = _fail_mutation(tmp_path, mode="raise", db=db)
    engine_a.storage.close()

    # fresh process: still blocked; nothing plans
    engine_b, gm_b, storage_b, _ = _engine(db, sb)
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED

    # explicit recovery transition
    acked = engine_b.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    assert acked.status == RecoveryStatus.ACKNOWLEDGED
    assert acked.acknowledged_by == "user:alice"
    assert gm_b.get_goal(gid).status == GoalStatus.ACTIVE  # unblocked

    # the fresh task STILL requires fresh authorization (recovery is not
    # authorization): approval is queued again, nothing mutates yet
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED
    req = engine_b.approval_store.list_requests()[-1]
    assert req.status == ApprovalStatus.PENDING
    assert not (sb / "notes.txt").exists()

    # fresh approval -> mutation proceeds exactly once
    engine_b.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    kinds = [e.kind for e in storage_b.list_events()]
    # exactly one attempt for the failed task AND one for the fresh task -
    # the failed mutation was never re-run, the fresh one ran once
    old_task = gm_b.task_history(gid)[0]
    old_kinds = [e.kind for e in storage_b.list_events() if e.task_id == old_task.id]
    assert old_kinds.count("mutation.attempted") == 1
    assert kinds.count("mutation.attempted") == 2
    engine_b.storage.close()


def test_recovery_transition_is_audited_and_typed(tmp_path):
    engine, gm, storage, _, _, gid, rec = _fail_mutation(tmp_path)
    engine.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    kinds = [e.kind for e in storage.list_events()]
    assert "recovery.required" in kinds and "recovery.acknowledged" in kinds
    # typed errors: unknown id, double-acknowledge
    with pytest.raises(RecoveryError, match="unknown"):
        engine.acknowledge_recovery("recovery_nope", actor="user:alice")
    with pytest.raises(RecoveryError, match="acknowledged"):
        engine.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    engine.storage.close()


# ---------------------------------------------------------------------------
# 8-10. no approval mutation, successful writes untouched, bounded metadata
# ---------------------------------------------------------------------------


def test_recovery_cannot_touch_approval_records(tmp_path):
    """Acknowledging recovery never changes approval statuses - in particular
    it cannot resurrect an expired/denied approval or reopen an approved one."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "ap.db"
    engine, gm, storage, _, _, gid, rec = _fail_mutation(tmp_path, db=db)
    # approve ANOTHER goal's request, deny one, expire one
    g_deny = engine.submit_goal("write notes").id
    engine.run_goal(g_deny)
    req_deny = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req_deny.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    g_exp = engine.submit_goal("write notes").id
    engine.run_goal(g_exp)
    req_exp = engine.approval_store.list_requests()[-1]
    engine.approval_ttl_seconds = 60
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")

    engine.acknowledge_recovery(rec.recovery_id, actor="user:alice")

    assert engine.approval_store.get_request(req_deny.approval_id).status == ApprovalStatus.DENIED
    assert engine.approval_store.get_request(req_exp.approval_id).status == ApprovalStatus.EXPIRED
    # the approved request tied to the failed task stays APPROVED (unchanged)
    req_approved = [r for r in engine.approval_store.list_requests()
                    if r.task_id == rec.task_id and r.step_index == rec.step_index][0]
    assert req_approved.status == ApprovalStatus.APPROVED
    # recovery never CREATES or RESOLVES approvals
    assert all(r.status != ApprovalStatus.PENDING for r in engine.approval_store.list_requests())
    engine.storage.close()


def test_successful_write_creates_no_recovery(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "ok.db", sb)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert engine.recovery_store.list_recoveries() == []
    assert gm.get_goal(gid).status == GoalStatus.COMPLETED
    engine.storage.close()


def test_recovery_metadata_bounded_no_content(tmp_path):
    _, _, _, _, _, _, rec = _fail_mutation(tmp_path)
    d = rec.to_dict()
    assert "content" not in d and "data" not in d
    assert set(d) <= {"recovery_id", "task_id", "goal_id", "step_index", "capability",
                      "action", "resource", "reason", "status", "created_at",
                      "acknowledged_at", "acknowledged_by"}
    assert len(rec.reason) <= 500  # bounded reason
    assert isinstance(rec.recovery_id, str) and rec.recovery_id


# ---------------------------------------------------------------------------
# Phase F: advisory (planning information only)
# ---------------------------------------------------------------------------


def test_recovery_advisory_reaches_planner_with_provenance(tmp_path):
    """After the explicit recovery transition, the planner is ADVISED of the
    previous mutation recovery (bounded, with provenance) - and while recovery
    is open, the planner is never consulted (the gate holds)."""
    planner = RecordingPlanner()
    sb = _sandbox(tmp_path)
    db = tmp_path / "adv.db"
    engine_a, gm_a, storage_a, _, _, gid, rec = _fail_mutation(tmp_path, planner=planner, db=db)
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sb, planner=planner, memory=True)
    # recovery open: run_goal blocks without planning
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED
    assert planner.seen_contexts == []
    # acknowledge -> fresh planning happens and the planner SEES the advisory
    engine_b.acknowledge_recovery(rec.recovery_id, actor="user:alice")
    engine_b.run_goal(gid)  # queues approval; planning happens first
    assert planner.seen_contexts, "planner must receive a context"
    adv = planner.seen_contexts[-1]
    assert any(r.get("recovery_id") == rec.recovery_id and r.get("status") == "acknowledged"
               and r.get("capability") == "filesystem.write" and r.get("resource") == "notes.txt"
               for r in adv)
    assert any("reason" in r and "content" not in r for r in adv)  # bounded, no content
    kinds = [e.kind for e in storage_b.list_events()]
    assert "planning.recovery.advisory" in kinds
    engine_b.storage.close()


def test_advisory_never_authorizes_mutation(tmp_path):
    """The advisory tells the planner 'mutation not retry-safe / fresh
    authorization needed' - but even a planner that tries to act on it as
    'approved' cannot mutate without going through authorization."""
    class RecklessPlanner(WritePlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            step = super().plan(goal_description, task_id, registry, context=context)[0]
            step.params["approved"] = True  # model output claim: ignored
            return [step]

    engine, gm, storage, _, sb, gid, rec = _fail_mutation(tmp_path, mode="raise", planner=RecklessPlanner())
    assert engine.run_goal(gid).status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()
    engine.storage.close()
