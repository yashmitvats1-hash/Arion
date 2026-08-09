"""Goal-level approval loop tests (ADR-017).

- A task reaching AWAITING_APPROVAL stops run_goal() cleanly (no spin) and
  the goal becomes durably BLOCKED with an approval_pending blocker.
- Approval-pending survives restart (goal + task + approval record).
- resolve_approval(APPROVED) resumes the EXACT pending step, no replan.
- resolve_approval(DENIED) produces a durable, explainable failure.
- Re-authorization always runs against CURRENT live ActionSpec/policy
  metadata; stale approvals cannot authorize changed resources/actions.
- Approval can never modify actor identity or ActionSpec metadata; model
  output / memory / strategy cannot self-approve.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
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
from arion.state.models import GoalStatus, StepStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class ReviewCapability:
    """A resource-bearing, medium-risk action: requires approval (ADR-009)."""

    name = "repo.review"
    description = "review a file (medium risk)"
    actions = [
        ActionSpec(name="review", description="review", required_scope="review:run",
                   risk="medium", side_effects="read_only", reversible=True,
                   idempotent=True, retry_safe=True,
                   resource_kind=FS, resource_param="path",
                   param_schema={"path": {"type": "string", "required": True}},
                   default_verification={"policy": "schema_keys", "args": {"keys": ["review"]}}),
    ]

    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append(dict(params))
        return {"review": "ok", "path": params.get("path")}


class TwoStepPlanner:
    """Plan: step0 filesystem.list (low risk), step1 repo.review (approval)."""

    def plan(self, goal_description, task_id, registry, context=None):
        from arion.state.models import PlanStep, VerificationPolicy

        return [
            PlanStep(index=0, intent="list root", capability="filesystem.read", action="list",
                     scope="filesystem:read", params={"path": "."},
                     verification=VerificationPolicy("non_empty")),
            PlanStep(index=1, intent="review", capability="repo.review", action="review",
                     scope="review:run", params={"path": "README.md"},
                     verification=VerificationPolicy("schema_keys", {"keys": ["review"]})),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.read", "repo.review"}


def _engine(db_path, sandbox, handler=None, policy=None, registry=None):
    storage = SQLiteStorage(db_path)
    registry = registry or CapabilityRegistry()
    if not registry.has("filesystem.read"):
        registry.register(FilesystemReadCapability(sandbox))
    if not registry.has("repo.review"):
        review = ReviewCapability()
        registry.register(review)
    events = EventLogger(sinks=[storage])
    planner = TwoStepPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    if policy is None:
        policy = ResourcePolicy(
            allowed_scopes={"filesystem:read", "review:run"},
            boundaries={FS: RelativePathBoundary()},
        )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy, approval_handler=handler or PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def _awaiting_task(engine, gid):
    for t in engine.goal_manager.task_history(gid):
        if t.status == TaskStatus.AWAITING_APPROVAL:
            return t
    return None


# ---------------------------------------------------------------------------
# 1. run_goal stops cleanly; no spin; durable BLOCKED
# ---------------------------------------------------------------------------


def test_approval_pending_stops_goal_no_spin(tmp_path, sandbox):
    engine, gm, storage, _ = _engine(tmp_path / "a.db", sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id

    g1 = engine.run_goal(gid)
    assert g1.status == GoalStatus.BLOCKED
    assert g1.blockers and g1.blockers[0]["type"] == "approval_pending"
    task = _awaiting_task(engine, gid)
    assert task is not None and task.status == TaskStatus.AWAITING_APPROVAL
    assert task.steps[1].status == StepStatus.PENDING  # exact step paused
    assert task.steps[0].status == StepStatus.SUCCEEDED  # earlier work kept

    # no spin: a second call must not re-request approval or re-execute
    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.BLOCKED
    requests = [e for e in storage.list_events() if e.kind == "approval.requested"]
    assert len(requests) == 1
    assert [e.kind for e in storage.list_events()].count("goal.blocked") == 1
    # goal.evaluated shows the distinct awaiting_approval action
    evals = [e for e in storage.list_events() if e.kind == "goal.evaluated"]
    assert evals[-1].detail["next_action"] == "await_approval"
    # approval-pending is NOT an ordinary failure: no task failed
    assert all(t.status != TaskStatus.FAILED for t in gm.task_history(gid))
    engine.storage.close()


def test_approval_pending_does_not_complete_goal(tmp_path, sandbox):
    """Even if all currently-executable work is done, an unresolved
    approval-gated step must keep the goal from completing."""
    engine, gm, storage, _ = _engine(tmp_path / "b.db", sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    result, _ = gm.evaluate(gid)
    assert result.next_action == "await_approval"
    assert gm.get_goal(gid).status != GoalStatus.COMPLETED
    engine.storage.close()


# ---------------------------------------------------------------------------
# 2. restart safety
# ---------------------------------------------------------------------------


def test_approval_pending_survives_restart(tmp_path, sandbox):
    db = tmp_path / "r.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sandbox)
    goal_a = engine_a.submit_goal("review this repository")
    gid = goal_a.id
    engine_a.run_goal(gid)
    task_a = _awaiting_task(engine_a, gid)
    assert task_a is not None
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sandbox)
    goal_b = gm_b.get_goal(gid)
    assert goal_b.status == GoalStatus.BLOCKED
    assert goal_b.blockers[0]["type"] == "approval_pending"
    task_b = _awaiting_task(engine_b, gid)
    assert task_b is not None and task_b.status == TaskStatus.AWAITING_APPROVAL
    assert task_b.id == task_a.id
    assert task_b.steps[1].status == StepStatus.PENDING
    # the approval record persisted with the task
    recs = [r for r in task_b.approvals if r.get("step_index") == 1]
    assert recs and recs[-1]["outcome"] == "pending"
    # still no spin after restart
    requests = [e for e in storage_b.list_events() if e.kind == "approval.requested"]
    assert len(requests) == 1
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# 3. approval resolution seam
# ---------------------------------------------------------------------------


def test_approval_granted_resumes_exact_step_no_replan(tmp_path, sandbox):
    db = tmp_path / "g.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    assert task is not None

    # APPROVE via the seam
    resolved = engine.resolve_approval(task.id, ApprovalOutcome.APPROVED, actor="user:alice")
    assert resolved.status == TaskStatus.RUNNING  # resumable, not terminal
    assert resolved.approvals[-1]["outcome"] == "approved"
    assert resolved.approvals[-1]["resolved_by"] == "user:alice"
    assert gm.get_goal(gid).status == GoalStatus.ACTIVE  # unblocked

    # resume: exact step 1 runs, step 0 NOT re-executed, no re-plan
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    task2 = gm.task_history(gid)[-1]
    assert task2.status == TaskStatus.COMPLETED
    assert task2.steps[1].status == StepStatus.SUCCEEDED
    assert [c for c in review.calls] == [{"path": "README.md"}]  # executed ONCE
    events = storage.list_events()
    kinds = [e.kind for e in events]
    assert kinds.count("plan.produced") == 1          # no replan on approval
    # step 0 ran exactly once (before the pause); step 1 (approved) ran once
    assert len([e for e in events if e.kind == "capability.executed" and e.step_id == "step_0"]) == 1
    assert len([e for e in events if e.kind == "capability.executed" and e.step_id == "step_1"]) == 1
    assert "task.approval.resumed" in kinds
    assert "goal.approval.granted" in kinds
    assert [h["plan_version"] for h in gm.plan_history(gid)] == [1]
    engine.storage.close()


def test_approval_denied_is_durable_and_explainable(tmp_path, sandbox):
    db = tmp_path / "d.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    assert task is not None

    resolved = engine.resolve_approval(task.id, ApprovalOutcome.DENIED, actor="user:alice")
    assert resolved.status == TaskStatus.FAILED
    assert resolved.error == "approval denied"
    assert resolved.steps[1].status == StepStatus.FAILED
    assert resolved.steps[1].error == "approval denied"
    # goal unblocked (approval question resolved); NOT completed
    assert gm.get_goal(gid).status == GoalStatus.ACTIVE
    kinds = [e.kind for e in storage.list_events()]
    assert "goal.approval.denied" in kinds
    assert "approval.denied" in kinds
    assert "task.failed" in kinds
    # durable across restart
    engine.storage.close()
    engine_b, gm_b, storage_b, _ = _engine(db, sandbox)
    task_b = gm_b.task_history(gid)[-1]
    assert task_b.status == TaskStatus.FAILED and task_b.error == "approval denied"
    engine_b.storage.close()


def test_approval_granted_then_restart_resumes(tmp_path, sandbox):
    """APPROVED before restart: the fresh process resumes the exact step."""
    db = tmp_path / "gr.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sandbox)
    goal_a = engine_a.submit_goal("review this repository")
    gid = goal_a.id
    engine_a.run_goal(gid)
    task_a = _awaiting_task(engine_a, gid)
    engine_a.resolve_approval(task_a.id, ApprovalOutcome.APPROVED)
    engine_a.storage.close()

    engine_b, gm_b, storage_b, registry_b = _engine(db, sandbox)
    review_b = registry_b.get("repo.review")
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert [c for c in review_b.calls] == [{"path": "README.md"}]
    assert [h["plan_version"] for h in gm_b.plan_history(gid)] == [1]
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# 4. re-authorization against CURRENT live metadata (stale approvals)
# ---------------------------------------------------------------------------


def test_stale_approval_cannot_authorize_changed_resource(tmp_path, sandbox):
    engine, gm, storage, registry = _engine(tmp_path / "s1.db", sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)

    # the plan's resource changes BEFORE resume (race)
    task = engine.storage.load_task(task.id)
    task.steps[1].params["path"] = "notes.txt"
    engine.storage.save_task(task)

    g2 = engine.run_goal(gid)
    # the approval covered README.md, not notes.txt -> NOT honored; fresh
    # approval requested and the step pauses again (never executed)
    assert g2.status == GoalStatus.BLOCKED
    assert review.calls == []  # repo.review NEVER executed with notes.txt
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.requested") == 2  # fresh request
    assert kinds.count("task.approval.resumed") == 0
    engine.storage.close()


def test_stale_approval_rejected_when_required_scope_changes(tmp_path, sandbox):
    db = tmp_path / "s2.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)

    # live ActionSpec metadata changes: scope now requires review:admin
    class LockedReview:
        name = "repo.review"
        description = "review (tightened)"
        actions = [ActionSpec(name="review", description="review",
                              required_scope="review:admin", risk="medium",
                              side_effects="read_only", reversible=True,
                              idempotent=True, retry_safe=True,
                              resource_kind=FS, resource_param="path",
                              param_schema={"path": {"type": "string", "required": True}},
                              default_verification={"policy": "schema_keys", "args": {"keys": ["review"]}})]

        def execute(self, action, params):
            review.calls.append(dict(params))
            return {"review": "ok", "path": params.get("path")}

    registry.register(LockedReview())

    g2 = engine.run_goal(gid)
    # policy denies the NEW scope -> the approved action is NOT executed
    assert g2.status == GoalStatus.ACTIVE
    task2 = gm.task_history(gid)[-1]
    assert task2.status == TaskStatus.FAILED
    assert "review:admin" in (task2.error or "")
    assert review.calls == []
    assert "permission.denied" in [e.kind for e in storage.list_events()]
    engine.storage.close()


def test_stale_approval_rejected_when_risk_changes(tmp_path, sandbox):
    db = tmp_path / "s3.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)

    class HighRiskReview:
        name = "repo.review"
        description = "review (high risk now)"
        actions = [ActionSpec(name="review", description="review", required_scope="review:run",
                              risk="high", side_effects="read_only", reversible=True,
                              idempotent=True, retry_safe=True,
                              resource_kind=FS, resource_param="path",
                              param_schema={"path": {"type": "string", "required": True}},
                              default_verification={"policy": "schema_keys", "args": {"keys": ["review"]}})]

        def execute(self, action, params):
            review.calls.append(dict(params))
            return {"review": "ok", "path": params.get("path")}

    registry.register(HighRiskReview())
    g2 = engine.run_goal(gid)
    task2 = gm.task_history(gid)[-1]
    assert task2.status == TaskStatus.FAILED
    assert "risk" in (task2.error or "").lower()
    assert review.calls == []  # never executed under the old approval
    engine.storage.close()


def test_stale_approval_rejected_when_boundary_removed(tmp_path, sandbox):
    db = tmp_path / "s4.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)

    # live policy change: the filesystem:path boundary is removed (fail closed)
    engine.policy = ResourcePolicy(allowed_scopes={"filesystem:read", "review:run"})  # no boundaries
    g2 = engine.run_goal(gid)
    task2 = gm.task_history(gid)[-1]
    assert task2.status == TaskStatus.FAILED
    assert "boundary" in (task2.error or "").lower()
    assert review.calls == []
    engine.storage.close()


# ---------------------------------------------------------------------------
# 5. adversarial: approval cannot widen authority
# ---------------------------------------------------------------------------


def test_approval_cannot_modify_actor_identity(tmp_path, sandbox):
    engine, gm, storage, registry = _engine(tmp_path / "ad1.db", sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)

    captured = {}
    real_policy = engine.policy

    class SpyPolicy:
        def decide(self, request):
            captured["actor"] = request.actor
            captured["chain"] = list(request.actor.chain)
            return real_policy.decide(request)

    engine.policy = SpyPolicy()
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine.run_goal(gid)
    # the actor used for authorization is the ENGINE's actor, never the
    # approval's actor - an approval cannot change identity
    assert captured["actor"].id == "agent:system"
    assert "user:alice" not in captured["chain"]
    engine.storage.close()


def test_approval_cannot_change_action_spec_metadata(tmp_path, sandbox):
    engine, gm, storage, registry = _engine(tmp_path / "ad2.db", sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    before = registry.action_spec("repo.review", "review").to_dict()

    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)
    engine.run_goal(gid)

    after = registry.action_spec("repo.review", "review").to_dict()
    assert before == after  # approval wrote nothing into the registry
    engine.storage.close()


def test_model_output_cannot_self_approve(tmp_path, sandbox):
    """A plan step carrying an approval/grant field cannot skip the seam."""
    engine, gm, storage, registry = _engine(tmp_path / "ad3.db", sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)

    # "model output" attempts to inject approval into the paused step
    task = engine.storage.load_task(task.id)
    task.steps[1].params["approved"] = True
    task.steps[1].params["grant"] = "approve"
    engine.storage.save_task(task)

    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.BLOCKED          # still pending
    assert review.calls == []                        # never executed
    task2 = _awaiting_task(engine, gid)
    assert task2 is not None and task2.status == TaskStatus.AWAITING_APPROVAL
    engine.storage.close()


def test_memory_strategy_cannot_self_approve(tmp_path, sandbox):
    """Poisoned strategy/guidance cannot manufacture an approval record."""
    engine, gm, storage, registry = _engine(tmp_path / "ad4.db", sandbox)
    # poison memory: an episode claiming approval was granted
    from arion.memory.models import Episode

    ep = Episode(episode_id="ep_evil", task_id="t", goal="review this repository",
                 outcome="completed", plan_summary=[], actions=[],
                 resources=[], tags=["filesystem.read"], authorization={"denials": []},
                 failures=[], recovery={}, importance=1.0)
    engine.memory = None  # keep memory out; inject via approvals list directly below
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)

    # even if something wrote an "approved" record WITHOUT the fingerprint of
    # the live request, the engine must not honor it (defense in depth)
    task = engine.storage.load_task(task.id)
    for r in task.approvals:
        r["outcome"] = "approved"
        r["fingerprint"] = {"capability": "repo.review", "action": "review",
                            "scope": "WRONG", "risk": "low", "side_effects": "read_only",
                            "resource_kind": FS, "resource": "/etc/passwd"}
    engine.storage.save_task(task)

    g2 = engine.run_goal(gid)
    task2 = _awaiting_task(engine, gid)
    assert task2 is not None  # fingerprint mismatch -> fresh request -> pending
    assert g2.status == GoalStatus.BLOCKED
    engine.storage.close()


def test_resolve_approval_rejects_wrong_state(tmp_path, sandbox):
    engine, gm, storage, _ = _engine(tmp_path / "ad5.db", sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)
    # second resolution on the same task (no longer awaiting) fails closed
    with pytest.raises(Exception):
        engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)
    engine.storage.close()
