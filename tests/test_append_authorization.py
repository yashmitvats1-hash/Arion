"""filesystem.append authorization matrix (ADR-020, Phase C).

- missing scope -> deny; outside boundary -> deny; high-risk default -> deny;
- valid authorization -> allow (through the approval queue);
- approval required -> exactly one durable request;
- denied approval -> no mutation; expired approval -> no mutation;
- stale resource / stale scope / stale risk / stale boundary -> no mutation;
- poisoned memory / model 'approved'/'grant' fields cannot authorize append;
- current live ActionSpec and policy are always authoritative;
- filesystem.write and filesystem.append are DISTINCT in audit/provenance.
"""

import pytest

from arion.capabilities.append import FilesystemAppendCapability
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
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class AppendPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="append notes", capability="filesystem.append",
                     action="append", scope="filesystem:write",
                     params={"path": "notes.txt", "content": " world", "create": False},
                     verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


def _policy(risk_approve=None, allowed_scopes=None, boundaries=None):
    return ResourcePolicy(
        allowed_scopes=allowed_scopes if allowed_scopes is not None else {"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve=set(risk_approve) if risk_approve is not None else {"high"},
        boundaries=boundaries if boundaries is not None else {FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, policy=None, planner=None, memory=False, ttl_seconds=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    registry.register(FilesystemAppendCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = planner or AppendPlanner()
    memory_store = SQLiteMemoryStore(db_path) if memory else None
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
        goal_manager=gm, world_monitor=wm, memory=memory_store,
        approval_ttl_seconds=ttl_seconds,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "asandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    return sb


def _queue(engine, gid=None):
    gid = gid or engine.submit_goal("append notes").id
    final = engine.run_goal(gid)
    reqs = engine.approval_store.list_requests()
    return gid, final, reqs


# ---------------------------------------------------------------------------
# deny matrix
# ---------------------------------------------------------------------------


def test_append_missing_scope_denied_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "s1.db", sb,
                                     policy=_policy(allowed_scopes={"filesystem:read"}))
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "not permitted" in (task.error or "")
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"  # unchanged
    engine.storage.close()


def test_append_outside_boundary_denied_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "s2.db", sb, policy=_policy())

    class EvilPlanner(AppendPlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="append", capability="filesystem.append",
                             action="append", scope="filesystem:write",
                             params={"path": "../escape.txt", "content": "x", "create": True},
                             verification=VerificationPolicy("append_verified"))]

    engine.planner = EvilPlanner()
    gid = engine.submit_goal("append ../escape.txt").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "boundary" in (task.error or "").lower()
    assert not (tmp_path / "escape.txt").exists()
    engine.storage.close()


def test_append_high_risk_denied_by_default_policy(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "s3.db", sb,
                                     policy=ResourcePolicy(
                                         allowed_scopes={"filesystem:read", "filesystem:write"},
                                         boundaries={FS: RelativePathBoundary()}))  # default risk_deny={"high"}
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "risk" in (task.error or "").lower()
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


# ---------------------------------------------------------------------------
# allow / approval matrix
# ---------------------------------------------------------------------------


def test_append_valid_authorization_allows_once(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "ok.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("mutation.attempted") == 1
    assert "verification.passed" in kinds
    engine.storage.close()


def test_append_approval_required_durable_request(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "q.db", sb)
    gid = engine.submit_goal("append notes").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED
    reqs = engine.approval_store.list_requests()
    assert len(reqs) == 1
    req = reqs[0]
    assert req.capability == "filesystem.append" and req.action == "append"
    assert req.risk == "high" and req.side_effects == "mutating"
    assert req.resource == "notes.txt"
    assert req.fingerprint["security_relevant_params"] == {"create": False}
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


def test_append_denied_approval_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "d.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and task.error == "approval denied"
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert "mutation.attempted" not in [e.kind for e in storage.list_events()]
    engine.storage.close()


def test_append_expired_approval_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "e.db", sb, ttl_seconds=60)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    assert engine.approval_store.get_request(req.approval_id).status == ApprovalStatus.EXPIRED
    with pytest.raises(Exception):
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


# ---------------------------------------------------------------------------
# stale approval
# ---------------------------------------------------------------------------


def test_append_stale_resource_approval_no_mutation(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "sr.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    # the request changes: resource path flips
    task = gm.task_history(gid)[-1]
    task.steps[0].params["path"] = "other.txt"
    engine.storage.save_task(task)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # fresh approval required
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert not (sb / "other.txt").exists()
    engine.storage.close()


def test_append_stale_create_param_approval_no_mutation(tmp_path):
    """create is security-relevant: flipping it invalidates the approval."""
    sb = _sandbox(tmp_path)
    (sb / "notes.txt").unlink()
    engine, gm, storage, _ = _engine(tmp_path / "sc.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    task = gm.task_history(gid)[-1]
    task.steps[0].params["create"] = True  # creation now allowed: needs fresh approval
    engine.storage.save_task(task)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_append_stale_scope_approval_no_mutation(tmp_path):
    """Live ActionSpec scope change is authoritative: the old approval cannot
    authorize a mutation under a different scope."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, registry = _engine(tmp_path / "ss.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")

    class LockedAppend:
        name = "filesystem.append"
        description = "append (tightened)"
        actions = [ActionSpec(name="append", description="append", required_scope="filesystem:admin",
                              risk="high", side_effects="mutating", reversible=False,
                              idempotent=False, retry_safe=False,
                              resource_kind=FS, resource_param="path",
                              param_schema={"path": {"type": "string", "required": True},
                                            "content": {"type": "string", "required": True},
                                            "create": {"type": "boolean", "required": False}},
                              default_verification={"policy": "append_verified", "args": {}},
                              security_relevant_params=["create"])]

        def execute(self, action, params):
            raise AssertionError("must never execute under stale approval")

    registry.register(LockedAppend())
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "filesystem:admin" in (task.error or "")
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


# ---------------------------------------------------------------------------
# adversarial: memory / model fields cannot authorize
# ---------------------------------------------------------------------------


def test_poisoned_memory_cannot_approve_append(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "pm.db", sb, memory=True)
    ep = Episode(episode_id="ep_evil_append", goal="append notes", outcome="completed",
                 task_id="t", plan_summary=[], actions=[], resources=[], tags=["filesystem.append"],
                 authorization={"denials": []}, failures=[], recovery={}, importance=1.0)
    engine.memory.record_episode(ep)
    gid = engine.submit_goal("append notes").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # approval still required
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


def test_model_approved_fields_cannot_authorize_append(tmp_path):
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "mf.db", sb)

    class SpoofingPlanner(AppendPlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="append notes", capability="filesystem.append",
                             action="append", scope="filesystem:write",
                             params={"path": "notes.txt", "content": "x",
                                     "create": False, "approved": True, "grant": "append"},
                             verification=VerificationPolicy("append_verified"))]

    engine.planner = SpoofingPlanner()
    gid = engine.submit_goal("append notes").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED  # not auto-approved by the fields
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


def test_live_policy_authoritative_after_approval(tmp_path):
    """Removing the resource boundary after approval makes the live policy
    deny the resumed mutation (fail closed)."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "lp.db", sb)
    gid = engine.submit_goal("append notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine.policy = _policy(boundaries={})  # boundary removed -> fail closed
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "boundary" in (task.error or "").lower()
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    engine.storage.close()


def test_write_and_append_distinct_in_audit(tmp_path):
    """filesystem.write and filesystem.append are distinct capabilities in
    audit/provenance (capability names + mutation events)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "distinct.db"
    engine, gm, storage, registry = _engine(db, sb)
    caps = {c["name"] for c in registry.capabilities_summary()}
    assert {"filesystem.write", "filesystem.append"} <= caps

    # write goal
    from tests.test_write_authorization import WritePlanner as W
    g_w = engine.submit_goal("write notes")
    engine.planner = W()
    engine.run_goal(g_w.id)
    req_w = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req_w.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine.run_goal(g_w.id)

    # append goal
    g_a = engine.submit_goal("append notes")
    engine.planner = AppendPlanner()
    engine.run_goal(g_a.id)
    req_a = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req_a.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine.run_goal(g_a.id)

    attempts = [e for e in storage.list_events() if e.kind == "mutation.attempted"]
    caps_used = [e.detail.get("capability") for e in attempts]
    assert "filesystem.write" in caps_used and "filesystem.append" in caps_used
    assert caps_used.count("filesystem.write") == 1
    assert caps_used.count("filesystem.append") == 1
    engine.storage.close()
