"""Guidance/authority invariants (architecture directive #4).

Memory-driven plan transformation must NEVER alter:
  - authorization metadata (scope)
  - actor identity
  - ActionSpec metadata (risk, side effects, resource kind, ...)
  - resource boundaries
  - risk / approval decisions
  - capability registration

The planner may change WHAT is proposed; the registry + policy decide what is
permitted, unchanged by any guidance.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.guidance import (
    MemoryGuidance,
    apply_guidance_to_steps,
    registry_resource_param,
)
from arion.memory.reflector import DeterministicReflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    Actor,
    AuthorizationRequest,
    PathPrefixBoundary,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, policy, actor=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events, policy=policy,
        actor=actor or Actor.agent("system"),
        memory=SQLiteMemoryStore(db_path), reflector=DeterministicReflector(),
    ), registry, storage


def _seed_denied_and_prefer(db_path, sandbox):
    """Seed one denied episode (README.md) + one completed (notes.txt)."""
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/", "."])}),
        memory=SQLiteMemoryStore(db_path), reflector=DeterministicReflector(),
    )
    # denied: README.md outside the public/ boundary
    goal = engine.submit_goal("inspect this repository")
    task = engine.create_task(goal)
    task.steps = [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                           scope="filesystem:read", params={"path": "README.md"},
                           verification=VerificationPolicy("non_empty"))]
    engine.storage.save_task(task)
    engine.run_task(task.id)
    # completed: public/notes.txt is inside the public/ boundary
    public_dir = sandbox / "public"
    public_dir.mkdir(exist_ok=True)
    (public_dir / "notes.txt").write_text("public notes\n", encoding="utf-8")
    goal2 = engine.submit_goal("read notes")
    task2 = engine.create_task(goal2)
    task2.steps = [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                            scope="filesystem:read", params={"path": "public/notes.txt"},
                            verification=VerificationPolicy("non_empty"))]
    engine.storage.save_task(task2)
    engine.run_task(task2.id)
    engine.storage.close()


def test_guidance_cannot_alter_authorization_metadata(tmp_path, sandbox):
    db = tmp_path / "g.db"
    _seed_denied_and_prefer(db, sandbox)
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/", "."])})
    engine, registry, storage = _engine(db, sandbox, policy)

    # ActionSpec snapshot BEFORE planning
    spec_before = registry.action_spec("filesystem.read", "read")
    before = {k: getattr(spec_before, k) for k in
              ("required_scope", "risk", "side_effects", "resource_kind", "resource_param")}

    # run a task whose plan gets guidance applied
    task = engine.execute_goal("inspect this repository")
    assert task.status == TaskStatus.COMPLETED  # guidance re-targeted to public/notes.txt
    # step scope still from registry (authorization metadata untouched by guidance)
    assert all(s.scope == "filesystem:read" for s in task.steps)

    spec_after = registry.action_spec("filesystem.read", "read")
    after = {k: getattr(spec_after, k) for k in before}
    assert after == before  # ActionSpec metadata unchanged

    # guidance applied at plan level cannot carry scope overrides
    guidance = [MemoryGuidance(guidance_id="g", category="prefer", capability="filesystem.read",
                               action="read", resource="notes.txt", episode_id="ep_x")]
    tr = apply_guidance_to_steps(
        [PlanStep(index=0, intent="r", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"path": "README.md"})],
        guidance,
        resource_param_resolver=lambda c, a: registry_resource_param(registry, c, a),
    )
    assert tr.transformed[0].scope == "filesystem:read"  # scope unchanged
    storage.close()


def test_guidance_cannot_change_actor_identity(tmp_path, sandbox):
    db = tmp_path / "g2.db"
    _seed_denied_and_prefer(db, sandbox)
    policy = ResourcePolicy(allowed_scopes={"filesystem:read"}, boundaries={FS: PathPrefixBoundary(["public/", "."])})
    engine, _, storage = _engine(db, sandbox, policy, actor=Actor.user("alice").delegated("arion"))
    assert engine.actor.id == "agent:arion"
    engine.execute_goal("inspect this repository")
    assert engine.actor.id == "agent:arion"          # unchanged by guidance
    assert engine.actor.chain == ("user:alice", "agent:arion")
    storage.close()


def test_guidance_cannot_change_boundaries_or_risk_or_approval(tmp_path, sandbox):
    db = tmp_path / "g3.db"
    _seed_denied_and_prefer(db, sandbox)
    engine, registry, storage = _engine(
        db, sandbox,
        ResourcePolicy(allowed_scopes={"filesystem:read", "risk:run", "medium:run"},
                       boundaries={FS: PathPrefixBoundary(["public/", "."])}),
    )
    # run a task with guidance to ensure the loop exercised memory
    engine.execute_goal("inspect this repository")

    # resource boundary answer unchanged
    req = AuthorizationRequest(
        actor=Actor.agent("system"), task_id="t", step_index=0,
        capability="filesystem.read", action="read", scope="filesystem:read",
        params={"path": "README.md"}, resource="README.md",
        resource_kind="filesystem:path", risk="low", side_effects="read_only",
    )
    assert engine.policy.decide(req).outcome.value == "deny"  # still outside public/

    # risk answer unchanged (high risk denied, medium requires approval)
    req_high = AuthorizationRequest(
        actor=Actor.agent("system"), task_id="t", step_index=0,
        capability="risk.tool", action="run", scope="risk:run",
        params={}, resource=None, resource_kind=None,
        risk="high", side_effects="irreversible",
    )
    assert engine.policy.decide(req_high).outcome.value == "deny"
    req_med = AuthorizationRequest(
        actor=Actor.agent("system"), task_id="t", step_index=0,
        capability="medium.tool", action="run", scope="medium:run",
        params={}, resource=None, resource_kind=None,
        risk="medium", side_effects="mutating",
    )
    assert engine.policy.decide(req_med).outcome.value == "require_approval"
    storage.close()


def test_guidance_cannot_register_capabilities(tmp_path, sandbox):
    db = tmp_path / "g4.db"
    _seed_denied_and_prefer(db, sandbox)
    engine, registry, storage = _engine(db, sandbox, ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/", "."])}))
    before_list = registry.list()
    engine.execute_goal("inspect this repository")
    assert registry.list() == before_list  # no capabilities added/removed
    assert not registry.has("shell.exec")
    storage.close()


def test_consolidation_does_not_bind_storage_seam_raises(tmp_path):
    """The archival/pruning seam exists but intentionally does nothing yet."""
    store = SQLiteMemoryStore(tmp_path / "a.db")
    with pytest.raises(NotImplementedError, match="not yet implemented"):
        store.prune()
    store.close()
