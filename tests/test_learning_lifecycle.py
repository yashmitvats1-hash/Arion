"""Learning lifecycle invariants (ADR-013 addendum, Phase 2) - tests first.

- repeated lifecycle invocation for the SAME task -> exactly one episode
  and one reflection (idempotent; no duplicate memories);
- retry after completion -> still one episode per task;
- two different tasks -> two episodes (dedup is per task, not per goal);
- restart between execution and learning -> catch-up records the
  missing episode exactly once;
- learn_from_terminal_tasks is idempotent across repeated runs;
- empty/malformed learning input fails closed (model validation);
- concurrent completion (two stores, one DB) -> exactly one episode
  (transactional task-keyed dedup);
- lifecycle state advances recorded -> reflected -> consolidated.
"""

from __future__ import annotations

import threading

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, memory=True):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    mem = SQLiteMemoryStore(db_path) if memory else None
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=mem, reflector=DeterministicReflector(),
    ), mem


def _episodes_for(memory, task_id: str) -> list[Episode]:
    return [e for e in memory.list_recent(limit=1000) if e.task_id == task_id]


def test_repeated_lifecycle_invocation_is_idempotent(tmp_path, sandbox):
    """Recording the same terminal task twice yields ONE episode + ONE
    reflection (the engine calls _record_memory from many terminal paths;
    restart/resume/retry must never duplicate memories)."""
    engine, memory = _engine(tmp_path / "a.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    assert len(_episodes_for(memory, task.id)) == 1

    # simulate a second terminal-path invocation for the SAME task
    engine._record_memory(task)
    engine._record_memory(task)
    episodes = _episodes_for(memory, task.id)
    assert len(episodes) == 1, "duplicate episodes for one task"
    ep = episodes[0]
    assert ep.lifecycle == "consolidated"
    # exactly one reflection, linked
    reflections = memory.list_recent_reflections(limit=100)
    mine = [r for r in reflections if r.episode_id == ep.episode_id]
    assert len(mine) == 1
    assert memory.get_episode(ep.episode_id).reflection_id == mine[0].reflection_id
    engine.storage.close()
    memory.close()


def test_retry_after_completion_does_not_duplicate(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "b.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    # a retry of the same task id (as the engine does on resume paths)
    engine._record_memory(task)
    engine._record_memory(task)
    assert len(_episodes_for(memory, task.id)) == 1
    engine.storage.close()
    memory.close()


def test_distinct_tasks_get_distinct_episodes(tmp_path, sandbox):
    """Dedup is per task - two different tasks produce two episodes."""
    engine, memory = _engine(tmp_path / "c.db", sandbox)
    t1 = engine.execute_goal("summarize this repository")
    t2 = engine.execute_goal("list files in this repository")
    assert t1.id != t2.id
    assert len(_episodes_for(memory, t1.id)) == 1
    assert len(_episodes_for(memory, t2.id)) == 1
    assert len(memory.list_recent(limit=100)) >= 2
    engine.storage.close()
    memory.close()


def test_restart_between_execution_and_learning_catch_up(tmp_path, sandbox):
    """A task completed WITHOUT memory (crash before learning) is
    recovered by learn_from_terminal_tasks after restart - exactly once."""
    db = tmp_path / "d.db"
    engine_a, _ = _engine(db, sandbox, memory=False)  # no memory: like a
    task = engine_a.execute_goal("summarize this repository")  # crash gap
    task_id = task.id
    assert task.status == TaskStatus.COMPLETED
    engine_a.storage.close()

    # restart WITH memory: the episode does not exist yet
    engine_b, memory_b = _engine(db, sandbox, memory=True)
    assert _episodes_for(memory_b, task_id) == []
    # catch-up learning records it exactly once
    n = engine_b.learn_from_terminal_tasks()
    assert n == 1
    episodes = _episodes_for(memory_b, task_id)
    assert len(episodes) == 1 and episodes[0].lifecycle == "consolidated"
    # a second catch-up pass records nothing new
    assert engine_b.learn_from_terminal_tasks() == 0
    assert len(_episodes_for(memory_b, task_id)) == 1
    engine_b.storage.close()
    memory_b.close()


def test_catch_up_is_idempotent_and_skips_existing(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "e.db", sandbox)
    t1 = engine.execute_goal("summarize this repository")
    # t1 already has an episode; a fresh failed task exists without one
    goal = engine.submit_goal("read a missing file")
    t2 = engine.create_task(goal)
    from arion.state.models import PlanStep, VerificationPolicy
    t2.steps = [PlanStep(index=0, intent="read", capability="filesystem.read",
                         action="read", scope="filesystem:read",
                         params={"path": "nope.txt"},
                         verification=VerificationPolicy("non_empty"))]
    engine.storage.save_task(t2)
    t2 = engine.run_task(t2.id)
    assert t2.status == TaskStatus.FAILED
    engine._record_memory(t2)  # already recorded

    # catch-up: t1 exists, t2 exists -> nothing new
    assert engine.learn_from_terminal_tasks() == 0
    assert len(_episodes_for(memory, t1.id)) == 1
    assert len(_episodes_for(memory, t2.id)) == 1
    engine.storage.close()
    memory.close()


def test_empty_episode_rejected_and_malformed_input_fails_closed(tmp_path, sandbox):
    """The store fails closed on malformed learning input."""
    memory = SQLiteMemoryStore(tmp_path / "f.db")
    with pytest.raises(ValueError):
        Episode(episode_id="", goal="x", outcome="completed")  # empty id
    with pytest.raises(ValueError):
        Episode(episode_id="ep-1", goal="x", outcome="bogus-outcome")
    with pytest.raises(ValueError):
        Episode(episode_id="ep-1", goal="x", outcome="completed",
                importance=99.0)  # out of [0,1]
    assert memory.get_episode("ep-1") is None  # nothing was stored
    memory.close()


def test_concurrent_completion_exactly_one_episode(tmp_path, sandbox):
    """Two stores (two 'learning workers') recording the same task
    concurrently: the DB-level task-keyed uniqueness guarantees exactly
    one episode survives."""
    db = tmp_path / "g.db"
    engine, _ = _engine(db, sandbox)
    task = engine.execute_goal("summarize this repository")
    engine.storage.close()

    from arion.memory.lifecycle import build_episode_from_task
    from arion.state.store import SQLiteStorage as SS
    storage = SS(db)
    saved = storage.load_task(task.id)

    m1 = SQLiteMemoryStore(db)
    m2 = SQLiteMemoryStore(db)
    errors: list[Exception] = []

    def record(mem):
        try:
            ep = build_episode_from_task(saved)
            mem.record_episode(ep)
        except Exception as exc:  # unique-index race is expected for one
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(m,)) for m in (m1, m2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    m1.close()
    m2.close()

    m3 = SQLiteMemoryStore(db)
    episodes = _episodes_for(m3, task.id)
    assert len(episodes) == 1, f"expected exactly one episode, got {len(episodes)}"
    m3.close()


def test_lifecycle_state_advances_in_order(tmp_path, sandbox):
    """recorded -> reflected -> consolidated, observable durably."""
    engine, memory = _engine(tmp_path / "h.db", sandbox)
    task = engine.execute_goal("summarize this repository")
    ep = _episodes_for(memory, task.id)[0]
    assert ep.lifecycle == "consolidated"  # the full pass ran
    assert ep.reflection_id is not None
    engine.storage.close()
    memory.close()


def test_learning_failure_never_breaks_execution(tmp_path, sandbox):
    """A throwing memory store must not change the task outcome; the
    lifecycle stays recoverable (catch-up can retry later)."""
    engine, _ = _engine(tmp_path / "i.db", sandbox)

    class BrokenMemory:
        def get_episode_by_task(self, task_id):
            return None

        def record_episode(self, ep):
            raise RuntimeError("disk full")

        def record_reflection(self, r):
            raise RuntimeError("disk full")

        def link_reflection(self, e, r):
            raise RuntimeError("disk full")

    engine.memory = BrokenMemory()
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED  # memory failure invisible
    engine.storage.close()


def test_catchup_observability_event_bounded(tmp_path, sandbox):
    """learn_from_terminal_tasks emits one bounded memory.learning.catchup
    event with counts - never raw content."""
    db = tmp_path / "obs.db"
    engine_a, _ = _engine(db, sandbox, memory=False)
    task = engine_a.execute_goal("summarize this repository")
    engine_a.storage.close()
    engine_b, memory_b = _engine(db, sandbox, memory=True)
    assert engine_b.learn_from_terminal_tasks() == 1
    events = [e for e in engine_b.storage.list_events()  # global event
              if e.kind == "memory.learning.catchup"]
    assert len(events) == 1
    detail = events[0].detail
    assert detail["recorded"] == 1 and detail["skipped"] == 0
    assert detail["processed"] >= 1
    dumped = str(detail)
    assert "summarize" not in dumped  # no goal text in the event
    assert len(dumped) < 500
    engine_b.storage.close()
    memory_b.close()
