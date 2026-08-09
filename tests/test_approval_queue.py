"""Persistent approval queue tests (ADR-018, Phase A).

- ApprovalRequest domain model + durable SQLite persistence (restart-safe).
- Exactly one durable request per task/step/authorization-fingerprint; no
  duplicate pending requests; repeated calls idempotent.
- resolve_approval / resolve_approval_request resolve the durable record and
  reuse the exact-step resume path.
- Fail closed: unknown id, already-resolved, wrong task/step, stale
  fingerprint, denied stays denied.
- Bounded audit events (approval.queued / approval.granted / approval.denied).
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
from arion.state.approvals import ApprovalError, ApprovalRequest, ApprovalStatus
from arion.state.models import GoalStatus, StepStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class ReviewCapability:
    name = "repo.review"
    description = "review (medium risk)"
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


def _engine(db_path, sandbox, handler=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
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
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "review:run"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=handler or PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def _awaiting_task(engine, gid):
    for t in engine.goal_manager.task_history(gid):
        if t.status == TaskStatus.AWAITING_APPROVAL:
            return t
    return None


# ---------------------------------------------------------------------------
# 1. persistence across restart
# ---------------------------------------------------------------------------


def test_approval_request_persists_across_restart(tmp_path, sandbox):
    db = tmp_path / "q.db"
    engine_a, gm_a, storage_a, _ = _engine(db, sandbox)
    goal_a = engine_a.submit_goal("review this repository")
    gid = goal_a.id
    engine_a.run_goal(gid)
    task_a = _awaiting_task(engine_a, gid)
    assert task_a is not None
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sandbox)
    reqs = engine_b.approval_store.list_requests()
    assert len(reqs) == 1
    req = reqs[0]
    assert req.status == ApprovalStatus.PENDING
    assert req.task_id == task_a.id
    assert req.goal_id == gid
    assert req.capability == "repo.review" and req.action == "review"
    assert req.scope == "review:run" and req.risk == "medium"
    assert req.resource_kind == FS and req.resource == "README.md"
    assert req.requester_actor == "agent:system"
    assert req.actor_chain and req.actor_chain[-1] == "agent:system"
    assert req.summary and len(req.summary) <= 300  # bounded human-readable
    assert req.fingerprint
    # goal + task still durably blocked/awaiting
    assert gm_b.get_goal(gid).status == GoalStatus.BLOCKED
    assert _awaiting_task(engine_b, gid) is not None
    engine_b.storage.close()


def test_request_model_roundtrip(tmp_path, sandbox):
    db = tmp_path / "m.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    engine.run_goal(goal.id)
    req = engine.approval_store.list_requests()[0]
    d = req.to_dict()
    assert d["approval_id"] == req.approval_id
    assert d["status"] == "pending"
    assert set(d) >= {"approval_id", "task_id", "goal_id", "capability", "action",
                      "scope", "risk", "side_effects", "resource_kind", "resource",
                      "summary", "status", "requester_actor", "actor_chain",
                      "params_keys", "fingerprint", "decision_actor", "decided_at",
                      "created_at", "updated_at", "step_index"}
    engine.storage.close()


# ---------------------------------------------------------------------------
# 2. duplicate prevention + idempotency
# ---------------------------------------------------------------------------


def test_no_duplicate_pending_requests_same_fingerprint(tmp_path, sandbox):
    db = tmp_path / "dup.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    assert len(engine.approval_store.list_requests()) == 1

    # repeated direct run_task calls must not create duplicate requests
    engine.run_task(task.id)
    engine.run_task(task.id)
    reqs = engine.approval_store.list_requests()
    assert len(reqs) == 1
    assert reqs[0].status == ApprovalStatus.PENDING
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.queued") == 1
    assert kinds.count("approval.requested") == 1
    engine.storage.close()


def test_new_fingerprint_creates_new_request(tmp_path, sandbox):
    db = tmp_path / "nf.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    task = _awaiting_task(engine, gid)
    req0 = engine.approval_store.list_requests()[0]
    assert req0.resource == "README.md"

    # approve, then the plan's resource changes before resume -> the fresh
    # request has a different authorization fingerprint -> NEW queue record
    engine.resolve_approval_request(req0.approval_id, ApprovalOutcome.APPROVED)
    task = engine.storage.load_task(task.id)
    task.steps[1].params["path"] = "notes.txt"
    engine.storage.save_task(task)
    engine.run_goal(gid)
    reqs = engine.approval_store.list_requests()
    assert len(reqs) == 2
    assert reqs[0].resource == "README.md"
    assert reqs[1].resource == "notes.txt"
    engine.storage.close()


# ---------------------------------------------------------------------------
# 3. resolve by id / idempotency / invalid transitions
# ---------------------------------------------------------------------------


def test_resolve_approval_request_by_id(tmp_path, sandbox):
    db = tmp_path / "rid.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]

    resolved = engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.decision_actor == "user:alice"
    assert resolved.decided_at is not None
    # durable
    req2 = engine.approval_store.get_request(req.approval_id)
    assert req2.status == ApprovalStatus.APPROVED
    # goal unblocked; task resumable; exact-step resume completes
    assert gm.get_goal(gid).status == GoalStatus.ACTIVE
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert [c for c in review.calls] == [{"path": "README.md"}]
    assert [h["plan_version"] for h in gm.plan_history(gid)] == [1]
    engine.storage.close()


def test_unknown_approval_id_typed_error(tmp_path, sandbox):
    engine, _, _, _ = _engine(tmp_path / "u.db", sandbox)
    with pytest.raises(ApprovalError):
        engine.resolve_approval_request("approval_nope", ApprovalOutcome.APPROVED)


def test_already_resolved_typed_error(tmp_path, sandbox):
    db = tmp_path / "ar.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    with pytest.raises(ApprovalError, match="already"):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    with pytest.raises(ApprovalError, match="already"):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.DENIED)
    engine.storage.close()


def test_wrong_task_step_typed_error(tmp_path, sandbox):
    """Resolving an approval whose task is no longer awaiting fails closed."""
    db = tmp_path / "wt.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    # resolve once via the task entry point
    task = _awaiting_task(engine, gid)
    engine.resolve_approval(task.id, ApprovalOutcome.APPROVED)
    # a second resolution attempt for the same task is rejected
    with pytest.raises(Exception):
        engine.resolve_approval(task.id, ApprovalOutcome.DENIED)
    engine.storage.close()


def test_denied_approval_remains_durably_denied(tmp_path, sandbox):
    db = tmp_path / "dd.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    resolved = engine.resolve_approval_request(req.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    assert resolved.status == ApprovalStatus.DENIED
    assert resolved.decision_actor == "user:alice"
    # durable + terminal: cannot flip it back
    engine.storage.close()
    engine_b, gm_b, storage_b, _ = _engine(db, sandbox)
    req_b = engine_b.approval_store.get_request(req.approval_id)
    assert req_b.status == ApprovalStatus.DENIED
    task = gm_b.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and task.error == "approval denied"
    with pytest.raises(ApprovalError, match="already"):
        engine_b.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# 4. stale approval rejection (queue-backed)
# ---------------------------------------------------------------------------


def test_stale_approval_cannot_execute_changed_security_relevant_param(tmp_path, sandbox):
    """A security-relevant parameter change after approval forces a FRESH
    request; the capability is never executed under the stale approval."""
    db = tmp_path / "stale.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    review = registry.get("repo.review")
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)

    # the plan's resource changes before resume
    task = engine.storage.load_task(gm.task_history(gid)[-1].id)
    task.steps[1].params["path"] = "notes.txt"
    engine.storage.save_task(task)

    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.BLOCKED
    assert review.calls == []  # never executed with notes.txt
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.requested") == 2  # fresh request for new fingerprint
    assert kinds.count("task.approval.resumed") == 0
    engine.storage.close()


# ---------------------------------------------------------------------------
# 5. audit events
# ---------------------------------------------------------------------------


def test_approval_queue_audit_events(tmp_path, sandbox):
    db = tmp_path / "ev.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    kinds = [e.kind for e in storage.list_events()]
    assert "approval.queued" in kinds
    assert "approval.requested" in kinds
    # bounded detail only - never raw params values / secrets
    queued = [e for e in storage.list_events() if e.kind == "approval.queued"][0]
    assert "params_keys" not in queued.detail  # keys are metadata, not secrets
    assert "summary" not in queued.detail or len(queued.detail["summary"]) <= 300

    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    kinds = [e.kind for e in storage.list_events()]
    assert "approval.granted" in kinds
    engine.storage.close()


def test_approval_expired_event_kind_registered(tmp_path, sandbox):
    """approval.expired is a canonical audit kind (ADR-019 expiration)."""
    from arion.observability.events import EVENT_KINDS
    assert "approval.expired" in EVENT_KINDS
    assert "goal.approval.expired" in EVENT_KINDS
    # mutation audit vocabulary (ADR-019 non-retry-safe writes)
    assert "mutation.attempted" in EVENT_KINDS
    assert "mutation.succeeded" in EVENT_KINDS
    assert "mutation.failed" in EVENT_KINDS
    assert "mutation.requires_recovery" in EVENT_KINDS
