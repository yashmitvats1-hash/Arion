"""LEARNING ACCEPTANCE GATE (from-memory-to-learning milestone).

The milestone is successful only if prior experience changes a subsequent
planning decision in a measurable way:

  WITHOUT memory:  Goal -> Plan A  (filesystem.read -> previously failing resource)
  WITH memory:     Goal -> retrieved prior experience -> reflection/lesson
                   -> Plan B      (filesystem.read -> safe resource / alternative)

Also covers: the complete failure feedback loop (2+ cycles, no duplicate-lesson
pileup), provenance, and planning.memory.influence observability. All offline.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class DenyReadmeBoundary:
    """A boundary that allows everything except README.md (denies the resource)."""

    def allows(self, resource: str) -> bool:
        return resource != "README.md"


def _engine(db_path, sandbox, boundary=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    policy = ResourcePolicy(boundaries={FS: boundary or RelativePathBoundary()})
    memory = SQLiteMemoryStore(db_path)
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events, policy=policy,
        memory=memory, reflector=DeterministicReflector(),
    ), registry


def _read_notes_task(engine):
    goal = engine.submit_goal("read the notes file")
    task = engine.create_task(goal)
    task.steps = [PlanStep(index=0, intent="read notes", capability="filesystem.read", action="read",
                           scope="filesystem:read", params={"path": "notes.txt"},
                           verification=VerificationPolicy("schema_keys", {"keys": ["content"]}))]
    engine.storage.save_task(task)
    return engine.run_task(task.id)


def test_prior_experience_changes_planning_decision(tmp_path, sandbox):
    """THE ACCEPTANCE GATE: Plan A != Plan B, meaningfully (resource changed)."""
    db = tmp_path / "learn.db"
    engine, registry = _engine(db, sandbox, boundary=DenyReadmeBoundary())
    planner = DeterministicPlanner()

    # ---- WITHOUT memory: Plan A reads README.md ----
    plan_a = planner.plan("inspect this repository", "task_a", registry, context=None)
    assert plan_a[1].params["path"] == "README.md"

    # ---- SEED prior experience ----
    # 1) a DENIED episode (README.md outside boundary)
    t1 = engine.execute_goal("inspect this repository")
    assert t1.status == TaskStatus.FAILED
    assert "outside boundary" in (t1.error or "")
    # 2) a COMPLETED episode (notes.txt worked)
    t2 = _read_notes_task(engine)
    assert t2.status == TaskStatus.COMPLETED

    # ---- WITH memory: retrieved experience + reflection -> guidance -> Plan B ----
    retriever = MemoryRetriever(engine.memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    assert ctx.episodes, "relevant prior experience must be retrieved"
    plan_b = planner.plan("inspect this repository", "task_b", registry, context=ctx)

    # MEANINGFUL difference: the read step re-targeted away from the denied resource
    assert plan_b[1].params["path"] == "notes.txt"
    assert plan_a[1].params["path"] != plan_b[1].params["path"]
    # provenance: which memory influenced this plan?
    assert ctx.provenance["episode_ids"], "provenance must record influencing episodes"
    assert ctx.guidance, "guidance must be derived from memory"
    cats = {g.category for g in ctx.guidance}
    assert "avoid" in cats and "prefer" in cats
    # the change is attributable to guidance (not ordering noise): the avoid
    # guidance names the denied resource; the prefer guidance names the safe one
    avoid = [g for g in ctx.guidance if g.category == "avoid"][0]
    prefer = [g for g in ctx.guidance if g.category == "prefer"][0]
    assert avoid.resource == "README.md"
    assert prefer.resource == "notes.txt"
    engine.storage.close()


def test_failure_feedback_loop_end_to_end(tmp_path, sandbox):
    """Full loop through the real engine: failure -> episode -> reflection ->
    retrieval -> different plan -> success -> new episode -> consolidation."""
    db = tmp_path / "loop.db"
    engine, _ = _engine(db, sandbox, boundary=DenyReadmeBoundary())

    # Cycle 1: goal fails because README.md is denied -> episode + reflection
    t1 = engine.execute_goal("inspect this repository")
    assert t1.status == TaskStatus.FAILED
    episodes = engine.memory.list_recent(limit=10)
    denied = [e for e in episodes if e.outcome == "denied"]
    assert denied and denied[0].task_id == t1.id
    assert denied[0].reflection_id  # reflection generated

    # seed a successful alternative (notes.txt) so a safe substitution exists
    assert _read_notes_task(engine).status == TaskStatus.COMPLETED

    # Cycle 2: SAME goal now completes via a different strategy
    t2 = engine.execute_goal("inspect this repository")
    assert t2.status == TaskStatus.COMPLETED, f"expected completion after learning: {t2.error}"
    # the planner chose the safe resource (not the known-failing one)
    read_step = [s for s in t2.steps if s.action == "read"][0]
    assert read_step.params["path"] == "notes.txt"
    # observability: influence + retrieval + episode recorded
    kinds = [e.kind for e in engine.storage.list_events(t2.id)]
    assert "planning.memory.influence" in kinds
    influence = [e for e in engine.storage.list_events(t2.id) if e.kind == "planning.memory.influence"][0]
    assert influence.detail["episode_ids"], "influence event must carry episode ids"
    assert "avoid" in influence.detail["guidance_categories"]
    assert "memory.episode.recorded" in kinds

    # Cycle 3: repeats succeed; lessons do NOT pile up infinitely
    t3 = engine.execute_goal("inspect this repository")
    assert t3.status == TaskStatus.COMPLETED

    guidance = engine.memory and None
    # guidance dedupe: exactly one 'prefer' entry for (read, notes.txt)
    retriever = MemoryRetriever(engine.memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    prefers = [g for g in ctx.guidance if g.category == "prefer" and g.resource == "notes.txt"]
    assert len(prefers) == 1, "repeated successes must consolidate into one guidance entry"
    # consolidation records exist (repeated identical episodes merged)
    consolidations = engine.memory.list_consolidations(limit=100)
    assert consolidations, "consolidation must produce explicit records"
    engine.storage.close()


def test_second_cycle_does_not_duplicate_lessons_forever(tmp_path, sandbox):
    """Run the same goal many times; guidance stays bounded (no lesson pileup)."""
    db = tmp_path / "dup.db"
    engine, _ = _engine(db, sandbox, boundary=DenyReadmeBoundary())
    # seed the lesson: README.md denied, notes.txt works
    assert engine.execute_goal("inspect this repository").status == TaskStatus.FAILED
    assert _read_notes_task(engine).status == TaskStatus.COMPLETED
    for _ in range(3):
        assert engine.execute_goal("inspect this repository").status == TaskStatus.COMPLETED

    retriever = MemoryRetriever(engine.memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    # bounded: never more guidance than episodes, and deduped to one per key
    assert len(ctx.guidance) <= len(ctx.episodes)
    keys = {(g.category, g.capability, g.action, g.resource) for g in ctx.guidance}
    assert len(keys) == len(ctx.guidance), "guidance must be deduplicated"
    # the digest stays within budget even after many episodes
    assert len(ctx.digest()["episodes"]) <= ctx.budget.max_episodes
    engine.storage.close()


def test_provenance_traceable_to_source(tmp_path, sandbox):
    db = tmp_path / "prov.db"
    engine, _ = _engine(db, sandbox, boundary=DenyReadmeBoundary())
    t1 = engine.execute_goal("inspect this repository")  # denied
    assert _read_notes_task(engine).status == TaskStatus.COMPLETED

    retriever = MemoryRetriever(engine.memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    assert ctx.provenance["episode_ids"]
    assert ctx.provenance["reflection_ids"]
    assert ctx.provenance["guidance_ids"]
    # every guidance entry is traceable to an episode + reflection
    for g in ctx.guidance:
        assert g.episode_id in ctx.provenance["episode_ids"]
        if g.reflection_id:
            assert g.reflection_id in ctx.provenance["reflection_ids"]
    engine.storage.close()
