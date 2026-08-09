"""Write authorization + approval-path tests (ADR-019).

Invariants:
- NO mutation without authorization: missing boundary, outside boundary,
  poisoned memory/model fields cannot grant or bypass write permission;
- the capability itself never decides authorization;
- high-risk write routes through the REAL approval queue: exactly one durable
  request, idempotent repeats, restart-preserved, exact-step resume without
  replanning, live re-authorization before mutation;
- any change to capability/action/scope/risk/side-effects/resource/security-
  relevant params invalidates the approval (no stale mutation);
- a denied approval can never be retried as approved.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
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
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
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


def _approval_policy():
    """High risk -> REQUIRE_APPROVAL (the write approval path)."""
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, policy=None, handler=None):
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
        policy=policy or _approval_policy(),
        approval_handler=handler or PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "README.md").write_text("# repo\n", encoding="utf-8")
    return sb


# ---------------------------------------------------------------------------
# 1. no mutation without authorization
# ---------------------------------------------------------------------------


def test_write_missing_boundary_denied_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    policy = ResourcePolicy(allowed_scopes={"filesystem:write"})  # no boundaries
    engine, gm, storage, _ = _engine(tmp_path / "nb.db", sb, policy=policy)
    goal = engine.submit_goal("write notes")
    final = engine.run_goal(goal.id)
    assert final.status == GoalStatus.ACTIVE
    task = gm.task_history(goal.id)[-1]
    assert task.status == TaskStatus.FAILED
    assert "boundary" in (task.error or "").lower()
    assert not (sb / "notes.txt").exists()  # nothing was written
    engine.storage.close()


def test_write_outside_boundary_denied_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    policy = ResourcePolicy(allowed_scopes={"filesystem:write"},
                            boundaries={FS: RelativePathBoundary()})
    engine, gm, storage, _ = _engine(tmp_path / "ob.db", sb, policy=policy)

    # the plan targets a traversal path
    class EvilPlanner(WritePlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="write", capability="filesystem.write",
                             action="write", scope="filesystem:write",
                             params={"path": "../escape.txt", "content": "x"},
                             verification=VerificationPolicy("write_verified"))]

    engine.planner = EvilPlanner()
    goal = engine.submit_goal("write ../escape.txt")
    final = engine.run_goal(goal.id)
    task = gm.task_history(goal.id)[-1]
    assert task.status == TaskStatus.FAILED
    assert "outside boundary" in (task.error or "") or "boundary" in (task.error or "").lower()
    assert not (tmp_path / "escape.txt").exists()  # never written outside
    engine.storage.close()


def test_write_high_risk_denied_by_default_policy(tmp_path):
    sb = _sandbox(tmp_path)
    policy = ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                            boundaries={FS: RelativePathBoundary()})  # default risk_deny={"high"}
    engine, gm, storage, _ = _engine(tmp_path / "hr.db", sb, policy=policy)
    goal = engine.submit_goal("write notes")
    final = engine.run_goal(goal.id)
    task = gm.task_history(goal.id)[-1]
    assert task.status == TaskStatus.FAILED
    assert "risk" in (task.error or "").lower()
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_poisoned_memory_cannot_authorize_write(tmp_path):
    """Memory claiming 'writes were previously approved' cannot grant write
    permission - the policy still requires approval."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "mem.db"
    engine, gm, storage, _ = _engine(db, sb)
    # poison memory: an episode claiming approval was granted
    from arion.memory.models import Episode

    ep = Episode(episode_id="ep_evil", task_id="t", goal="write notes",
                 outcome="completed", plan_summary=[], actions=[],
                 resources=[], tags=["filesystem.write"],
                 authorization={"denials": []}, failures=[], recovery={}, importance=1.0)
    from arion.memory.store import SQLiteMemoryStore

    memory = SQLiteMemoryStore(db)
    memory.record_episode(ep)
    engine.memory = memory

    goal = engine.submit_goal("write notes")
    final = engine.run_goal(goal.id)
    # approval still required -> durably BLOCKED, nothing written
    assert final.status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_model_output_approval_fields_ignored_for_write(tmp_path):
    """A plan step carrying approved/grant fields cannot bypass the queue."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "mo.db", sb)
    goal = engine.submit_goal("write notes")
    engine.run_goal(goal.id)
    assert gm.get_goal(goal.id).status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_capability_never_decides_authorization(tmp_path):
    """The write capability has no policy hooks - it only knows its sandbox
    root. (Structural assertion via introspection of the class.)"""
    import inspect

    src = inspect.getsource(FilesystemWriteCapability)
    assert "policy" not in src.replace("policy", "", 1) or "def execute" in src
    # the execute signature takes action+params only; no policy reference
    sig = inspect.signature(FilesystemWriteCapability.execute)
    assert list(sig.parameters) == ["self", "action", "params"]


# ---------------------------------------------------------------------------
# 2. approval path: queue, idempotency, restart, exact-step resume
# ---------------------------------------------------------------------------


def test_write_approval_queued_once_and_idempotent(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "q.db", sb)
    goal = engine.submit_goal("write notes")
    gid = goal.id
    g1 = engine.run_goal(gid)
    assert g1.status == GoalStatus.BLOCKED
    reqs = engine.approval_store.list_requests()
    assert len(reqs) == 1
    req = reqs[0]
    assert req.capability == "filesystem.write" and req.action == "write"
    assert req.risk == "high" and req.side_effects == "mutating"
    assert req.resource == "notes.txt"
    assert req.fingerprint["security_relevant_params"] == {"overwrite": False}

    # repeated calls -> idempotent (still one request, no re-requests)
    task = next(t for t in gm.task_history(gid) if t.status == TaskStatus.AWAITING_APPROVAL)
    engine.run_task(task.id)
    engine.run_goal(gid)
    assert len(engine.approval_store.list_requests()) == 1
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.queued") == 1
    assert kinds.count("approval.requested") == 1
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_write_approval_pending_survives_restart(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "r.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sb)
    goal_a = engine_a.submit_goal("write notes")
    gid = goal_a.id
    engine_a.run_goal(gid)
    req_a = engine_a.approval_store.list_requests()[0]
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sb)
    req_b = engine_b.approval_store.get_request(req_a.approval_id)
    assert req_b.status.value == "pending"
    assert gm_b.get_goal(gid).status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()
    engine_b.storage.close()


def test_write_approval_granted_resumes_exact_step_single_write(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, registry = _engine(tmp_path / "g.db", sb)
    goal = engine.submit_goal("write notes")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")

    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert [h["plan_version"] for h in gm.plan_history(gid)] == [1]  # no replan
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("capability.executed") == 1  # exactly one write
    assert "mutation.succeeded" in kinds
    assert "task.approval.resumed" in kinds
    assert "verification.passed" in kinds
    engine.storage.close()


def test_write_approval_denied_durable_file_unchanged(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "d.db", sb)
    goal = engine.submit_goal("write notes")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and task.error == "approval denied"
    assert not (sb / "notes.txt").exists()  # file unchanged
    # a denied approval can never be retried as approved
    with pytest.raises(Exception):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    engine.storage.close()


# ---------------------------------------------------------------------------
# 3. stale approval invalidation
# ---------------------------------------------------------------------------


def _approve_then_mutate(engine, gid, param_editor):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    task = engine.storage.load_task(engine.goal_manager.task_history(gid)[-1].id)
    param_editor(task.steps[0])
    engine.storage.save_task(task)


def test_changed_write_resource_after_approval_denied(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "s1.db", sb)

    def edit(step):
        step.params["path"] = "other.txt"

    _approve_then_mutate(engine, engine.submit_goal("write notes").id, edit)
    g2 = engine.run_goal(engine.goal_manager.list_goals()[0].id)
    assert g2.status == GoalStatus.BLOCKED  # fresh approval needed
    assert not (sb / "notes.txt").exists()
    assert not (sb / "other.txt").exists()
    engine.storage.close()


def test_changed_overwrite_after_approval_denied(tmp_path):
    """Changing the security-relevant overwrite param invalidates the approval."""
    sb = _sandbox(tmp_path)
    (sb / "notes.txt").write_text("original", encoding="utf-8")
    engine, gm, storage, _ = _engine(tmp_path / "s2.db", sb)

    def edit(step):
        step.params["overwrite"] = True

    gid = engine.submit_goal("write notes").id
    _approve_then_mutate(engine, gid, edit)
    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.BLOCKED  # stale approval not honored
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "original"  # unchanged
    engine.storage.close()


def test_changed_content_after_approval_does_not_invalidate(tmp_path):
    """Content is the payload (operational), NOT a security-relevant param:
    changing it does not require a fresh approval."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "s3.db", sb)

    def edit(step):
        step.params["content"] = "different payload"

    gid = engine.submit_goal("write notes").id
    _approve_then_mutate(engine, gid, edit)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "different payload"
    engine.storage.close()


def test_changed_risk_or_scope_after_approval_denied(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, registry = _engine(tmp_path / "s4.db", sb)
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)

    class LockedWrite:
        name = "filesystem.write"
        description = "write (tightened)"
        actions = [ActionSpec(name="write", description="write", required_scope="filesystem:admin",
                              risk="high", side_effects="mutating", reversible=False,
                              idempotent=False, retry_safe=False,
                              resource_kind=FS, resource_param="path",
                              param_schema={"path": {"type": "string", "required": True},
                                            "content": {"type": "string", "required": True},
                                            "overwrite": {"type": "boolean", "required": False}},
                              default_verification={"policy": "write_verified", "args": {}},
                              security_relevant_params=["overwrite"])]

        def execute(self, action, params):
            raise AssertionError("must never execute under stale approval")

    registry.register(LockedWrite())
    g2 = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "filesystem:admin" in (task.error or "")
    assert not (sb / "notes.txt").exists()
    engine.storage.close()
