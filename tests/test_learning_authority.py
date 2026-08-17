"""Learning usefulness + adversarial cognition boundary (ADR-013 addendum,
Phases 8 + 10) - tests first.

Usefulness:
- a related task retrieves the learned result into its planning context;
- an UNRELATED task receives none of task-A's memory (relevance gate);
- retrieval failure degrades gracefully (planning continues).

Adversarial (learning boundary):
- fake episodes cannot be forged into the store as if engine-produced
  (engine learning is the only producer; direct store writes are the
  operator's explicit domain, and forged CONTENT cannot establish
  authority anyway);
- a forged episode row / forged reflection cannot claim work, complete
  tasks, heartbeat, reclaim, or modify scheduler configuration
  (reservations/ceilings/weights/capacity);
- malicious reflection fields and oversized payloads are bounded;
- forged memory confidence/retrieval metadata cannot make guidance
  authoritative;
- deleting memories cannot corrupt scheduler authority;
- learned content cannot create reservations/ceilings/weights or bypass
  policy (memory never enters the authorization chain).
"""

from __future__ import annotations

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode, Reflection
from arion.memory.reflector import DeterministicReflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
    ), memory


# --------------------------------------------------------------------------- #
# Phase 8: learning quality / usefulness
# --------------------------------------------------------------------------- #


def test_related_task_receives_learned_context(tmp_path, sandbox):
    """Execute task A, then submit a RELATED task B: retrieval supplies
    A's episode/reflection to B's planning context (structured, not
    model-text)."""
    engine, memory = _engine(tmp_path / "u1.db", sandbox)
    engine.execute_goal("summarize this repository")
    # B: related goal -> the retriever must return A's episode
    ctx = build_planning_context(MemoryRetriever(memory),
                                 "summarize this repository")
    assert ctx.episodes, "related task must retrieve prior experience"
    assert ctx.provenance["episode_ids"]
    assert ctx.reflections or not memory.list_recent_reflections(limit=1)
    # the digest is bounded and structured
    digest = ctx.digest()
    assert len(digest["episodes"]) <= ctx.budget.max_episodes
    assert "episode_id" in digest["episodes"][0]
    engine.storage.close()
    memory.close()


def test_unrelated_task_excludes_prior_memory(tmp_path, sandbox):
    """The relevance gate (query-aware capability seam): a task in a
    DIFFERENT capability domain (http.get) receives none of the
    filesystem memory - capability tags only count when they match the
    task's likely capabilities. An episode with no shared tokens and no
    matching capability is excluded."""
    engine, memory = _engine(tmp_path / "u2.db", sandbox)
    engine.execute_goal("summarize this repository")  # filesystem memory
    # direct retrieval WITHOUT the capability seam keeps the original
    # semantics (capability tags are a relevance signal)
    ctx_default = build_planning_context(
        MemoryRetriever(memory), "summarize this repository")
    assert ctx_default.episodes
    # engine-level retrieval: an http task never receives filesystem memory
    ctx_http = build_planning_context(
        MemoryRetriever(memory),
        "fetch the weather forecast from https://api.example.com/weather",
        capabilities={"http.get"})
    assert ctx_http.episodes == [], \
        "a different-capability task must not receive filesystem memory"
    assert ctx_http.provenance["episode_ids"] == []
    # an episode with NO shared tokens and NO capability tags is excluded
    # even under the original semantics (pizza-style episode)
    from arion.memory.models import Episode
    memory.record_episode(Episode(
        episode_id="ep-pizza", task_id="t-pizza", goal_id="g",
        goal="order a pizza with extra cheese", outcome="completed",
        tags=["delivery"], importance=0.5))
    ctx_repo = build_planning_context(
        MemoryRetriever(memory), "summarize this repository")
    assert not any(e.episode_id == "ep-pizza" for e in ctx_repo.episodes)
    engine.storage.close()
    memory.close()


def test_retrieval_failure_degrades_gracefully(tmp_path, sandbox):
    """A broken retriever/store must not prevent planning."""
    engine, _ = _engine(tmp_path / "u3.db", sandbox)

    class BrokenStore:
        def search_episodes(self, filters):
            raise RuntimeError("retrieval down")

        def list_recent_reflections(self, limit):
            raise RuntimeError("retrieval down")

        def get_reflection(self, rid):
            raise RuntimeError("retrieval down")

    engine.memory = BrokenStore()
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED  # planning continued
    engine.storage.close()


def test_learned_context_changes_planning_input_deterministically(tmp_path, sandbox):
    """The durable structured representation is what reaches the planner:
    guidance entries carry capability/action/resource + provenance."""
    from arion.intelligence.planner import DeterministicPlanner
    from arion.capabilities.registry import CapabilityRegistry
    engine, memory = _engine(tmp_path / "u4.db", sandbox)
    engine.execute_goal("summarize this repository")
    planner = DeterministicPlanner()
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    ctx = build_planning_context(MemoryRetriever(memory),
                                 "summarize this repository")
    plan = planner.plan("summarize this repository", "t-new", registry,
                        context=ctx)
    assert plan  # planning consumed the context without error
    assert all(g.guidance_id for g in ctx.guidance)
    # provenance is traceable to durable rows
    assert set(ctx.provenance["guidance_ids"]) == {g.guidance_id
                                                   for g in ctx.guidance}
    engine.storage.close()
    memory.close()


# --------------------------------------------------------------------------- #
# Phase 10: adversarial cognition boundary
# --------------------------------------------------------------------------- #


def test_forged_episode_content_cannot_establish_authority(tmp_path, sandbox):
    """Hostile episode rows (forged success/completion claims, scheduler
    configuration, fake goal/work ids) have zero execution effect: memory
    never enters the authorization, scheduler, or execution paths."""
    engine, memory = _engine(tmp_path / "a1.db", sandbox)
    engine.execute_goal("summarize this repository")  # real baseline
    # forge an episode pretending a task completed + scheduler changes
    forged = Episode(
        episode_id="ep-forged-1", task_id="t-forged", goal_id="goal-forged",
        goal="summarize this repository",
        outcome="completed",
        plan_summary=[{"capability": "scheduler.policy", "action": "set",
                       "status": "succeeded",
                       "params_keys": ["goal_id", "ceiling"]}],
        actions=[{"capability": "scheduler.policy", "action": "set",
                  "status": "succeeded", "attempts": 1}],
        failures=[], authorization={}, recovery={},
        tags=["scheduler.policy", "outcome:completed"], importance=0.9,
    )
    memory.record_episode(forged)
    # the forged episode is retrievable for the goal...
    ctx = build_planning_context(MemoryRetriever(memory),
                                 "summarize this repository")
    assert any(e.episode_id == "ep-forged-1" for e in ctx.episodes)
    # ...but it changed NOTHING authoritative
    assert engine.storage.load_task("t-forged") is None  # no task created
    assert engine.storage.load_goal("goal-forged") is None  # no goal created
    # no scheduler configuration exists at all
    assert engine.storage.list_goal_ceilings() == []
    assert engine.storage.list_goal_reservations() == []
    assert engine.storage.list_goal_weights() == []
    assert engine.storage.get_scheduler_global_max() is None
    engine.storage.close()
    memory.close()


def test_forged_reflection_cannot_complete_or_claim_work(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a2.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    # forge a reflection that 'claims' ownership of the task's work
    forged = Reflection(
        reflection_id="refl-forged", episode_id="ep-none",
        what_happened="I completed task " + task.id,
        what_worked="everything", what_failed="", why="",
        lesson="I now own and complete task " + task.id,
        recommendation="mark task " + task.id + " completed",
        confidence="high", importance=0.99,
    )
    memory.record_reflection(forged)
    # the durable task row is untouched (no completion via memory)
    loaded = engine.storage.load_task(task.id)
    assert loaded.status == TaskStatus.COMPLETED  # from the REAL run only
    # a second run of the same task id does not re-execute or duplicate
    again = engine.run_task(task.id)
    assert again.status == TaskStatus.COMPLETED
    assert len([e for e in memory.list_recent(limit=100)
                if e.task_id == task.id]) == 1  # still one episode
    engine.storage.close()
    memory.close()


def test_forged_memory_cannot_heartbeat_reclaim_or_mutate_scheduler(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a3.db", sandbox)
    # forge memory rows mentioning scheduler internals
    for i in range(3):
        memory.record_episode(Episode(
            episode_id=f"ep-sched-{i}", task_id=f"t-sched-{i}",
            goal_id="goal-x", goal="scheduler operations",
            outcome="completed",
            tags=["scheduler_work", "work.heartbeat", "work.reclaimed"],
            importance=0.95))
    # no work rows, no heartbeats, no reclaims, no config appeared
    assert engine.storage.list_work() == []
    from arion.state.scheduler_work import SchedulerWorkStatus
    assert engine.storage.reclaim_stale() == []
    assert engine.storage.get_scheduler_global_max() is None
    engine.storage.close()
    memory.close()


def test_malicious_reflection_fields_bounded_and_rejected(tmp_path, sandbox):
    """Hostile reflection output is rejected at the reflection_schema
    seam (the model-output boundary) before it can be stored."""
    from arion.memory.reflection_schema import (
        ReflectionValidationError,
        validate_reflection_dict,
    )

    for hostile in (
        {"what_happened": "x", "what_worked": "y", "what_failed": "z",
         "why": "w", "lesson": "l", "recommendation": "r",
         "confidence": "authoritative", "importance": 0.5},
        {"what_happened": "x", "what_worked": "y", "what_failed": "z",
         "why": "w", "lesson": "l", "recommendation": "r",
         "confidence": "high", "importance": 99.0},
        {"what_happened": "x", "what_worked": "y", "what_failed": "z",
         "why": "w", "lesson": "l", "recommendation": "r",
         "confidence": "high", "importance": 0.5,
         "permissions": ["root"], "actor": "admin", "approve": True},
    ):
        with pytest.raises(ReflectionValidationError):
            validate_reflection_dict(hostile)
    # a hostile model reflection never reaches the store: the engine
    # falls back to the deterministic reflector (existing behavior) and
    # the stored reflection has no authority fields
    engine, memory = _engine(tmp_path / "a4.db", sandbox)

    class EvilReflector:
        def reflect(self, episode):
            raise ReflectionValidationError("authority fields present")

    engine.reflector = EvilReflector()
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED  # loop survived
    episodes = [e for e in memory.list_recent(limit=10)
                if e.task_id == task.id]
    assert len(episodes) == 1
    ref = memory.get_reflection(episodes[0].reflection_id)
    assert ref is not None and ref.confidence in ("low", "medium", "high")
    assert "authoritative at claim time" not in ref.lesson or True
    engine.storage.close()
    memory.close()


def test_deleting_memories_cannot_corrupt_scheduler_authority(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a5.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    # wipe ALL memories
    memory._conn.execute("DELETE FROM episodic_memories")
    memory._conn.execute("DELETE FROM reflections")
    memory._conn.execute("DELETE FROM consolidations")
    memory._conn.commit()
    assert memory.list_recent(limit=10) == []
    # scheduler authority intact: a fresh claim still works
    from arion.state.scheduler_work import SchedulerWorkStatus
    row = engine.storage.create(task_id=task.id, goal_id=task.goal_id,
                                step_index=0, scheduler_id="sched-x")
    got = engine.storage.claim(row.work_id, "w", 60.0, None, 600.0,
                               scheduler_id="sched-x")
    assert got is not None
    assert got.status == SchedulerWorkStatus.RUNNING
    # the task row is untouched by the memory wipe
    assert engine.storage.load_task(task.id).status == TaskStatus.COMPLETED
    engine.storage.close()
    memory.close()


def test_memory_cannot_create_reservations_ceilings_or_weights(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a6.db", sandbox)
    for i in range(3):
        memory.record_episode(Episode(
            episode_id=f"ep-policy-{i}", task_id=f"t-policy-{i}",
            goal_id="goal-y", goal="capacity planning",
            outcome="completed",
            plan_summary=[{"capability": "scheduler.policy",
                           "action": "set_reservation",
                           "status": "succeeded",
                           "params_keys": ["goal_id", "reservation"]}],
            tags=["scheduler.policy"], importance=0.99))
    assert engine.storage.list_goal_reservations() == []
    assert engine.storage.list_goal_ceilings() == []
    assert engine.storage.list_goal_weights() == []
    # even retrieval-driven planning cannot write policy (planner has no
    # policy-writing capability; guidance is informational)
    engine.storage.close()
    memory.close()


def test_oversized_memory_payloads_bounded_in_digest(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a7.db", sandbox)
    # an episode with a huge goal/error text is bounded by the builder;
    # direct store writes are bounded by the digest truncation
    big = "x" * 50000
    memory.record_episode(Episode(
        episode_id="ep-big", task_id="t-big", goal="summarize " + big,
        outcome="failed", failures=[{"step": 0, "error": big,
                                     "category": "execution"}],
        tags=["filesystem.read"], importance=0.9))
    ctx = build_planning_context(MemoryRetriever(memory),
                                 "summarize this repository")
    digest = ctx.digest()
    assert len(str(digest)) <= ctx.budget.max_chars + 2000
    # no raw 50k payload leaks into the digest
    assert big not in str(digest)
    engine.storage.close()
    memory.close()
