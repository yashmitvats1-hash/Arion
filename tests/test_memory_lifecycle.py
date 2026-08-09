"""Memory lifecycle integration tests (ADR-012).

- successful task creates an episode
- failed task creates an episode
- authorization denial creates an episode
- restart preserves episodes
- planning context is built from memory before planning
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, PathPrefixBoundary, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, actor=None):
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
        actor=actor or Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
    ), memory


def _step(action="read", params=None, capability="filesystem.read", scope="filesystem:read", verification=None):
    return PlanStep(index=0, intent="step", capability=capability, action=action, scope=scope,
                    params=params or {"path": "README.md"},
                    verification=verification or VerificationPolicy("non_empty"))


def test_successful_task_creates_episode_and_reflection(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "a.db", sandbox)
    task = engine.execute_goal("summarize this repository")

    assert task.status == TaskStatus.COMPLETED
    episodes = memory.list_recent(limit=10)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.task_id == task.id
    assert ep.outcome == "completed"
    assert ep.plan_summary  # structured, not raw
    assert ep.goal == "summarize this repository"
    # reflection was generated and linked
    assert ep.reflection_id is not None
    ref = memory.get_reflection(ep.reflection_id)
    assert ref is not None
    assert ref.lesson
    # observability events emitted
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    for k in ("memory.episode.recorded", "reflection.created", "planning.context.created", "memory.retrieval.completed"):
        assert k in kinds, f"missing {k}"


def test_failed_task_creates_episode_with_failure(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "b.db", sandbox)
    goal = engine.submit_goal("read a missing file")
    task = engine.create_task(goal)
    task.steps = [_step(params={"path": "nope.txt"})]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    episodes = memory.list_recent(limit=10)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.outcome == "failed"
    assert ep.failures and "nope.txt" in ep.failures[0]["error"]
    assert ep.importance >= 0.6  # failures are salient
    # param VALUES never stored - only keys
    assert all("nope.txt" not in str(pk) for s in ep.plan_summary for pk in s["params_keys"])


def test_authorization_denial_creates_denied_episode(tmp_path, sandbox):
    # a policy constraining filesystem to public/ -> reading README.md is denied
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    storage = SQLiteStorage(tmp_path / "c.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(tmp_path / "c.db")
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events, policy=policy,
        memory=memory, reflector=DeterministicReflector(),
    )
    goal = engine.submit_goal("read the repo")
    task = engine.create_task(goal)
    task.steps = [_step(params={"path": "README.md"})]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    episodes = memory.list_recent(limit=10)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.outcome == "denied"
    assert ep.authorization["denials"], "denial must be recorded"
    assert "authorization:denied" in ep.tags


def test_completed_task_is_not_labeled_recovered(tmp_path, sandbox):
    """A task that runs to completion in one process (resuming from its
    plan-only checkpoint is the normal start-of-run boundary, NOT an
    interruption) yields a 'completed' episode - so successful outcomes
    produce prefer guidance instead of being discarded as 'recovered'."""
    engine, memory = _engine(tmp_path / "e.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    episodes = memory.list_recent(limit=5)
    completed = [e for e in episodes if e.task_id == task.id]
    assert completed and completed[0].outcome == "completed"
    assert "recovery:resumed" not in completed[0].tags
    # prefer guidance is derivable from the completed episode (learned success)
    from arion.memory.guidance import DeterministicMemoryGuidance
    from arion.memory.reflector import DeterministicReflector
    guidance = DeterministicMemoryGuidance().build(
        completed,
        [DeterministicReflector().reflect(completed[0])],
    )
    assert any(g.category == "prefer" for g in guidance)
    engine.storage.close()
    memory.close()


def test_mid_execution_resume_is_labeled_recovered(tmp_path, sandbox):
    """A task interrupted mid-execution and resumed across a restart is
    genuinely 'recovered' (interruption happened during step execution)."""
    db = tmp_path / "rec.db"
    engine_a, memory_a = _engine(db, sandbox)
    task_a = engine_a.create_task(engine_a.submit_goal("summarize this repository"))
    # plan + run in process A; then simulate a crash mid-execution by
    # checkpointing after the FIRST step completed, and resume in process B.
    task_a = engine_a._plan(task_a)
    task_a_id = task_a.id
    # mimic a mid-execution checkpoint (status RUNNING, as the engine saves it
    # after a step completes) - the interruption happened DURING execution
    task_a.status = TaskStatus.RUNNING
    task_a.current_step = 1
    engine_a._checkpoint(task_a, reason="step completed")
    engine_a.storage.save_task(task_a)
    engine_a.storage.close()
    memory_a.close()

    engine_b, memory_b = _engine(db, sandbox)
    resumed = engine_b.run_task(task_a_id)
    assert resumed.status == TaskStatus.COMPLETED
    episodes = memory_b.list_recent(limit=5)
    ep = next(e for e in episodes if e.task_id == task_a_id)
    assert ep.outcome == "recovered"
    assert "recovery:resumed" in ep.tags
    engine_b.storage.close()
    memory_b.close()


def test_restart_preserves_episodes_and_reflections(tmp_path, sandbox):
    """Task A executes in process 1; process 2 sees the episode + reflection."""
    db = tmp_path / "r.db"
    engine_a, memory_a = _engine(db, sandbox)
    task_a = engine_a.execute_goal("summarize this repository")
    task_a_id = task_a.id
    memory_a.close()

    # ---- process restart ----
    engine_b, memory_b = _engine(db, sandbox)
    episodes = memory_b.list_recent(limit=10)
    assert len(episodes) == 1
    assert episodes[0].task_id == task_a_id
    assert memory_b.get_reflection(episodes[0].reflection_id) is not None
    # a subsequent planning request receives relevant historical context
    ctx = engine_b._build_planning_context(engine_b.storage.load_task(task_a_id))
    assert ctx is not None and len(ctx.episodes) >= 1
    engine_b.storage.close()
    memory_b.close()


def test_memory_recording_never_breaks_lifecycle(tmp_path, sandbox):
    """Even a broken memory store must not fail the task."""
    engine, _ = _engine(tmp_path / "d.db", sandbox)

    class BrokenMemory:
        def record_episode(self, ep):
            raise RuntimeError("disk full")

    engine.memory = BrokenMemory()
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED  # memory failure is invisible to the loop
