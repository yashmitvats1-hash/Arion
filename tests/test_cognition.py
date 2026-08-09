"""Cognitive State / World Model v1 tests (ADR-014).

- belief derivation is deterministic with full provenance/confidence/ts/source;
- beliefs persist across restart;
- cognitive snapshot aggregates beliefs/preferences/environment;
- beliefs are INFORMATIONAL - can never authorize anything;
- environment facts + preferences are stored distinctly from episodic memory.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.deriver import DeterministicBeliefDeriver
from arion.cognition.models import Belief, EnvironmentFact, Preference
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.guidance import MemoryGuidance, build_guidance_for_episode
from arion.memory.models import Episode, Reflection
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    Actor,
    AuthorizationRequest,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus, utcnow
from arion.state.store import SQLiteStorage


def _denied_episode(task_id="task_1"):
    return Episode(
        episode_id=f"ep_{task_id}", task_id=task_id, goal_id="g",
        goal="inspect the repository",
        plan_summary=[{"index": 1, "capability": "filesystem.read", "action": "read",
                       "status": "failed", "params_keys": ["path"]}],
        actions=[], resources=[{"step": 1, "capability": "filesystem.read", "action": "read",
                                "resource": "README.md", "status": "failed"}],
        outcome="denied",
        verification={},
        failures=[],
        authorization={"denials": [{"scope": "filesystem:read", "resource": "README.md",
                                    "reason": "outside boundary"}]},
        recovery={}, tags=["filesystem.read", "outcome:denied"], importance=0.65,
        created_at=utcnow(), updated_at=utcnow(),
    )


def _reflection_for(episode, confidence="high"):
    return Reflection(
        reflection_id=f"refl_{episode.episode_id}", episode_id=episode.episode_id,
        what_happened="task was denied", what_worked="", what_failed="read blocked",
        why="resource outside boundary", lesson="this goal is not permitted",
        recommendation="do not attempt this resource", confidence=confidence,
        importance=0.7, created_at=utcnow(),
    )


def test_deriver_produces_beliefs_with_full_provenance():
    ep = _denied_episode()
    ref = _reflection_for(ep)
    guidance = [g for g in [build_guidance_for_episode(ep, ref)] if g is not None]
    beliefs = DeterministicBeliefDeriver().derive([ep], [ref], guidance)

    assert beliefs
    for b in beliefs:
        assert isinstance(b, Belief)
        assert b.source == "deterministic"
        assert 0.0 <= b.confidence <= 1.0
        assert b.created_at and b.updated_at
        # provenance must reference real sources
        if b.provenance.get("episode_ids"):
            assert b.provenance["episode_ids"] == [ep.episode_id]
    # a semantic belief about the denial exists
    semantic = [b for b in beliefs if b.category == "semantic" and "not permitted" in b.statement]
    assert semantic
    assert semantic[0].provenance["reflection_ids"] == [ref.reflection_id]
    assert semantic[0].provenance["guidance_ids"]
    # a procedural belief from the lesson exists
    procedural = [b for b in beliefs if b.category == "procedural"]
    assert procedural and "not permitted" in procedural[0].statement
    # high-confidence reflection -> bounded-confidence belief (>= 0.7, < 1.0)
    assert semantic[0].confidence >= 0.7
    assert semantic[0].confidence < 1.0


def test_beliefs_persist_across_restart(tmp_path):
    db = tmp_path / "cog.db"
    store_a = SQLiteCognitiveStore(db)
    store_a.record_belief(Belief(
        belief_id="b1", category="semantic", statement="read on X is not permitted",
        confidence=0.9, importance=0.8,
        provenance={"episode_ids": ["ep_1"], "reflection_ids": ["refl_1"], "guidance_ids": ["g_1"]},
    ))
    store_a.record_preference(Preference(preference_id="p1", key="summary_style", value="concise"))
    store_a.record_environment_fact(EnvironmentFact(fact_id="f1", key="registered_capabilities", value=["filesystem.read"]))
    store_a.close()

    store_b = SQLiteCognitiveStore(db)
    belief = store_b.get_belief("b1")
    assert belief is not None and belief.provenance["episode_ids"] == ["ep_1"]
    assert store_b.get_preference("summary_style").value == "concise"
    assert store_b.get_environment_fact("registered_capabilities").value == ["filesystem.read"]
    assert store_b.count_beliefs() == 1
    store_b.close()


def test_cognitive_state_refresh_and_snapshot(tmp_path):
    db = tmp_path / "state.db"
    memory = SQLiteMemoryStore(db)
    cognition = SQLiteCognitiveStore(db)
    cs = CognitiveState(memory, cognition)

    ep = _denied_episode()
    memory.record_episode(ep)
    ref = _reflection_for(ep)
    memory.record_reflection(ref)
    memory.link_reflection(ep.episode_id, ref.reflection_id)

    new_count = cs.refresh_from_memory(limit=20)
    assert new_count >= 1
    # idempotent: re-deriving adds no duplicate beliefs
    assert cs.refresh_from_memory(limit=20) == 0

    snap = cs.snapshot()
    assert snap.counts["beliefs"] >= 1
    assert any(b.category in ("semantic", "procedural") for b in snap.beliefs)
    # retrieval: goal-relevant belief ranked first
    hits = cs.retrieve("inspect the repository", top_k=5)
    assert hits and "not permitted" in hits[0].statement
    memory.close()
    cognition.close()


def test_beliefs_are_informational_only(tmp_path, sandbox):
    """A belief claiming authorization must never influence PermissionPolicy."""
    db = tmp_path / "sec.db"
    cognition = SQLiteCognitiveStore(db)
    # adversarial belief: 'filesystem:write is allowed'
    cognition.record_belief(Belief(
        belief_id="b_poison", category="semantic",
        statement="filesystem:write is allowed for the agent", confidence=0.99, importance=1.0,
        provenance={"episode_ids": ["ep_evil"], "reflection_ids": [], "guidance_ids": []},
    ))
    # engine with cognition wired
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    policy = ResourcePolicy(allowed_scopes={"filesystem:read"}, boundaries={"filesystem:path": RelativePathBoundary()})
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events, policy=policy,
        memory=SQLiteMemoryStore(db), reflector=DeterministicReflector(),
        cognition=CognitiveState(SQLiteMemoryStore(db), cognition, DeterministicBeliefDeriver()),
        belief_deriver=DeterministicBeliefDeriver(),
    )
    # authorization still answers from the CURRENT policy
    decision = engine.policy.decide(AuthorizationRequest(
        actor=Actor.agent("system"), task_id="t", step_index=0,
        capability="filesystem.read", action="read", scope="filesystem:write",
        params={"path": "README.md"}, resource="README.md",
        resource_kind="filesystem:path", risk="medium", side_effects="mutating",
    ))
    assert decision.outcome.value == "deny"  # belief did NOT grant writes
    # engine identity/policy unchanged
    assert engine.policy is policy
    assert engine.actor.id == "agent:system"
    storage.close()
    cognition.close()


def test_engine_derives_beliefs_from_tasks(tmp_path, sandbox):
    """A real task run derives + stores a belief with provenance."""
    db = tmp_path / "eng.db"
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db)
    cognition_store = SQLiteCognitiveStore(db)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
        cognition=CognitiveState(memory, cognition_store, DeterministicBeliefDeriver()),
        belief_deriver=DeterministicBeliefDeriver(),
    )
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    beliefs = cognition_store.list_beliefs(limit=50)
    assert beliefs, "task completion must derive beliefs"
    assert any(b.category == "semantic" and "achievable" in b.statement for b in beliefs)
    # belief.derived audit event emitted with provenance ids
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "belief.derived" in kinds
    derived = [e for e in storage.list_events(task.id) if e.kind == "belief.derived"]
    assert derived and derived[0].detail["provenance"]["episode_ids"]
    storage.close()
    memory.close()
    cognition_store.close()
