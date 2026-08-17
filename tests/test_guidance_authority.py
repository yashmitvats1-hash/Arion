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
    """The archival/pruning seam (ADR-014) is now implemented, fail-closed.

    - prune() with no criterion raises (never silently delete);
    - pruning episodes deletes their reflections but never consolidations
      (consolidation history is preserved, provenance intact).
    """
    from arion.memory.models import Episode, Reflection
    from arion.memory.store import ConsolidationRecord
    from datetime import datetime, timedelta, timezone

    def _iso_plus(iso, seconds):
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(seconds=seconds)).isoformat()

    t0 = "2026-01-01T00:00:00+00:00"
    store = SQLiteMemoryStore(tmp_path / "a.db")

    # no criteria -> fail closed, nothing deleted
    with pytest.raises(ValueError, match="never silently delete"):
        store.prune()

    # seed one old episode with a reflection + a consolidation over it
    store.record_episode(Episode(episode_id="ep-old", task_id="t-1", goal_id="g",
                                 goal="g", outcome="completed", importance=0.3,
                                 created_at=_iso_plus(t0, 0), updated_at=_iso_plus(t0, 0),
                                 reflection_id="refl-old"))
    store.record_reflection(Reflection(reflection_id="refl-old", episode_id="ep-old",
                                       what_happened="x", what_worked="", what_failed="",
                                       why="", lesson="lesson", recommendation="",
                                       confidence="medium", importance=0.3,
                                       created_at=_iso_plus(t0, 0)))
    store.record_consolidation(ConsolidationRecord(
        consolidation_id="consol-1", source_episode_ids=["ep-old"],
        category="lesson", merged_lesson="merged lesson", count=1,
        importance=0.5, created_at=_iso_plus(t0, 60)))

    removed = store.prune(older_than=_iso_plus(t0, 5))
    assert removed == 1                       # the old episode is prunable
    assert store.get_episode("ep-old") is None
    assert store.get_reflection("refl-old") is None    # reflection went with it
    consol_ids = [c.consolidation_id for c in store.list_consolidations()]
    assert "consol-1" in consol_ids           # consolidation preserved
    store.close()
