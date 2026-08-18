"""Durable one-reflection-per-episode invariant (ADR-013 addendum).

A previous race allowed concurrent learning workers to create more than one
reflection for the same episode:

    Worker A: observe episode.reflection_id is None
    Worker B: observe episode.reflection_id is None
    Worker A: create reflection
    Worker B: create reflection

The one-episode-per-task invariant is protected by a task-keyed unique
index; this file proves the equivalent DURABLE guarantee for reflections:

- one episode -> exactly one reflection (storage-level, cross-process);
- deterministic concurrent _record_memory race (barrier-synchronized);
- first-writer-wins claim; losers adopt the canonical reflection;
- same-id re-record still refreshes content (consolidation semantics);
- re-recording an episode never clobbers its durable reflection link;
- legacy databases with duplicate reflections are merged at init;
- repeated _record_memory / learn_from_terminal_tasks stay idempotent.
"""

from __future__ import annotations

import sqlite3
import threading

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.lifecycle import build_episode_from_task
from arion.memory.models import Reflection
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, reflector=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=reflector or DeterministicReflector(),
    )
    return engine, memory


def _terminal_task(engine):
    """A durably COMPLETED task with no learning applied yet."""
    goal = engine.submit_goal("summarize this repository")
    task = engine.create_task(goal)
    task.steps = [PlanStep(
        index=0, intent="read key files", capability="filesystem.read",
        action="read", scope="filesystem:read", params={"path": "README.md"},
        verification=VerificationPolicy("non_empty"),
        status=StepStatus.SUCCEEDED, result={"path": "README.md", "content": "# repo"},
    )]
    task.status = TaskStatus.COMPLETED
    engine.storage.save_task(task)
    return task


def _reflections_for(memory, episode_id):
    return [r for r in memory.list_recent_reflections(limit=200)
            if r.episode_id == episode_id]


class _BarrierReflector:
    """Synchronizes N concurrent learners at the reflection seam.

    Every worker blocks in reflect() until all parties arrive, which
    guarantees they ALL passed the `existing.reflection_id` guard (and
    re-recorded the episode) BEFORE any of them inserts a reflection -
    the exact interleaving of the original race, made deterministic.
    """

    def __init__(self, parties: int):
        self._barrier = threading.Barrier(parties)
        self._inner = DeterministicReflector()

    def reflect(self, episode):
        self._barrier.wait(timeout=60)
        return self._inner.reflect(episode)


# ------------------------------------------------------------------ #
# basic invariant + idempotency
# ------------------------------------------------------------------ #


def test_one_episode_exactly_one_reflection_and_idempotent_replays(tmp_path, sandbox):
    engine, memory = _engine(tmp_path / "inv.db", sandbox)
    task = engine.execute_goal("summarize this repository")

    eps = [e for e in memory.list_recent(limit=100) if e.task_id == task.id]
    assert len(eps) == 1
    refs = _reflections_for(memory, eps[0].episode_id)
    assert len(refs) == 1
    assert eps[0].reflection_id == refs[0].reflection_id

    # repeated _record_memory passes never add a second reflection
    saved = engine.storage.load_task(task.id)
    for _ in range(3):
        engine._record_memory(saved)
    assert len(_reflections_for(memory, eps[0].episode_id)) == 1

    # repeated catch-up passes record nothing new
    assert engine.learn_from_terminal_tasks() == 0
    assert engine.learn_from_terminal_tasks() == 0
    assert len(_reflections_for(memory, eps[0].episode_id)) == 1
    engine.storage.close()
    memory.close()


# ------------------------------------------------------------------ #
# deterministic concurrent _record_memory race (engine level)
# ------------------------------------------------------------------ #


def test_concurrent_record_memory_creates_exactly_one_reflection(tmp_path, sandbox):
    """Two workers race _record_memory on a recorded-but-unreflected
    episode (e.g. a peer crashed after record, or two catch-up workers
    overlap). Both observe reflection_id is None and both reflect - the
    barrier makes the interleaving deterministic. Exactly one reflection
    may exist afterwards, linked from the episode."""
    engine, memory = _engine(tmp_path / "race.db", sandbox, reflector=_BarrierReflector(2))
    task = _terminal_task(engine)
    # seed the recorded-but-unreflected episode (crash after record)
    memory.record_episode(build_episode_from_task(engine.storage.load_task(task.id)))
    saved = engine.storage.load_task(task.id)

    errors: list[Exception] = []

    def worker():
        try:
            engine._record_memory(saved)
        except Exception as exc:  # pragma: no cover - memory is best-effort
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors

    eps = [e for e in memory.list_recent(limit=100) if e.task_id == task.id]
    assert len(eps) == 1, f"concurrent workers created {len(eps)} episodes"
    refs = _reflections_for(memory, eps[0].episode_id)
    assert len(refs) == 1, f"concurrent workers created {len(refs)} reflections"
    assert eps[0].reflection_id == refs[0].reflection_id
    assert eps[0].lifecycle == "consolidated"
    engine.storage.close()
    memory.close()


def test_catchup_after_concurrent_race_still_single_reflection(tmp_path, sandbox):
    """After the race, a restart-style catch-up pass adds nothing."""
    engine, memory = _engine(tmp_path / "post.db", sandbox, reflector=_BarrierReflector(2))
    task = _terminal_task(engine)
    memory.record_episode(build_episode_from_task(engine.storage.load_task(task.id)))
    saved = engine.storage.load_task(task.id)
    threads = [threading.Thread(target=lambda: engine._record_memory(saved))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    recorded = engine.learn_from_terminal_tasks()
    assert recorded == 0  # fully learned already
    ep = memory.get_episode_by_task(task.id)
    assert ep is not None and ep.reflection_id is not None
    assert len(_reflections_for(memory, ep.episode_id)) == 1
    engine.storage.close()
    memory.close()


# ------------------------------------------------------------------ #
# storage-level claim semantics
# ------------------------------------------------------------------ #


def test_store_first_writer_wins_and_loser_adopts_canonical(tmp_path):
    """Two reflections (different ids) for one episode: the durable row
    inserted first is canonical; the loser's row is not stored and the
    canonical reflection is returned to BOTH callers."""
    db = tmp_path / "claim.db"
    m1 = SQLiteMemoryStore(db)
    m1.record_episode(_episode("ep-1", "t-1"))
    r1 = _reflection("refl-1", "ep-1", "first lesson")
    r2 = _reflection("refl-2", "ep-1", "second lesson")

    got1 = m1.record_reflection(r1)
    got2 = m1.record_reflection(r2)

    assert got1 is not None and got1.reflection_id == "refl-1"
    assert got2 is not None and got2.reflection_id == "refl-1"  # canonical, not refl-2
    rows = m1.list_recent_reflections(limit=100)
    assert len(rows) == 1 and rows[0].reflection_id == "refl-1"
    m1.close()

    # a separate process/store observes the same durable state
    m2 = SQLiteMemoryStore(db)
    got3 = m2.record_reflection(_reflection("refl-3", "ep-1", "third lesson"))
    assert got3 is not None and got3.reflection_id == "refl-1"
    assert len(m2.list_recent_reflections(limit=100)) == 1
    m2.close()


def test_store_same_id_rerecord_refreshes_content(tmp_path):
    """Re-recording the SAME reflection id updates its content in place
    (the historical INSERT OR REPLACE semantics consolidation relies on)
    and still leaves exactly one row."""
    store = SQLiteMemoryStore(tmp_path / "refresh.db")
    store.record_episode(_episode("ep-2", "t-2"))
    store.record_reflection(_reflection("refl-2", "ep-2", "old lesson"))
    got = store.record_reflection(_reflection("refl-2", "ep-2", "a different lesson"))
    assert got is not None and got.lesson == "a different lesson"
    rows = store.list_recent_reflections(limit=100)
    assert len(rows) == 1 and rows[0].lesson == "a different lesson"
    store.close()


def test_record_episode_never_clobbers_reflection_link(tmp_path):
    """Re-recording an episode (reflection_id=None on the fresh build)
    must preserve the durable reflection link - the old INSERT OR REPLACE
    wiped it, orphaning the reflection row."""
    store = SQLiteMemoryStore(tmp_path / "link.db")
    ep = _episode("ep-3", "t-3")
    store.record_episode(ep)
    store.record_reflection(_reflection("refl-3", "ep-3", "lesson"))
    store.link_reflection("ep-3", "refl-3")

    # a concurrent catch-up worker rebuilds the episode from scratch
    rebuilt = _episode("ep-3", "t-3")  # reflection_id defaults to None
    canonical = store.record_episode(rebuilt)

    assert canonical is not None
    assert canonical.reflection_id == "refl-3"  # link survived the re-record
    again = store.get_episode("ep-3")
    assert again is not None and again.reflection_id == "refl-3"
    store.close()


# ------------------------------------------------------------------ #
# legacy-database migration
# ------------------------------------------------------------------ #


def test_init_merges_duplicate_reflections_keeping_linked(tmp_path):
    db = tmp_path / "legacy.db"
    store = SQLiteMemoryStore(db)
    store.record_episode(_episode("ep-4", "t-4"))
    store.close()
    # simulate a legacy (pre-invariant) database: drop the unique index a
    # fresh store created, then hand-craft the race artifact - two
    # reflections for one episode; the episode links the NEWER one (the
    # last link_reflection won the original race)
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_reflections_episode_unique")
    for rid, lesson in (("refl-old", "old lesson"), ("refl-new", "new lesson")):
        conn.execute(
            "INSERT INTO reflections (reflection_id, episode_id, what_happened,"
            " what_worked, what_failed, why, lesson, recommendation, confidence,"
            " importance, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "ep-4", "h", "w", "f", "y", lesson, "r", "high", 0.5, "2026-01-01T00:00:00+00:00"))
    conn.execute("UPDATE episodic_memories SET reflection_id='refl-new' WHERE episode_id='ep-4'")
    conn.commit()
    conn.close()
    store.close()

    reopened = SQLiteMemoryStore(db)  # init-time merge + unique index
    rows = reopened.list_recent_reflections(limit=100)
    assert len(rows) == 1
    assert rows[0].reflection_id == "refl-new"  # the linked one survives
    ep = reopened.get_episode("ep-4")
    assert ep is not None and ep.reflection_id == "refl-new"  # link intact
    _assert_episode_unique_index(reopened)
    reopened.close()


def test_init_merges_duplicate_reflections_keeping_newest_without_link(tmp_path):
    db = tmp_path / "legacy2.db"
    store = SQLiteMemoryStore(db)
    store.record_episode(_episode("ep-5", "t-5"))
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_reflections_episode_unique")
    for rid, created in (("refl-a", "2026-01-01T00:00:00+00:00"),
                         ("refl-b", "2026-02-01T00:00:00+00:00")):
        conn.execute(
            "INSERT INTO reflections (reflection_id, episode_id, what_happened,"
            " what_worked, what_failed, why, lesson, recommendation, confidence,"
            " importance, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "ep-5", "h", "w", "f", "y", "lesson " + rid, "r", "high", 0.5, created))
    conn.commit()
    conn.close()
    store.close()

    reopened = SQLiteMemoryStore(db)
    rows = reopened.list_recent_reflections(limit=100)
    assert len(rows) == 1 and rows[0].reflection_id == "refl-b"  # newest kept
    _assert_episode_unique_index(reopened)
    reopened.close()


def test_init_repairs_link_pointing_at_a_losing_duplicate(tmp_path):
    db = tmp_path / "legacy3.db"
    store = SQLiteMemoryStore(db)
    store.record_episode(_episode("ep-6", "t-6"))
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_reflections_episode_unique")
    for rid, created in (("refl-keep", "2026-01-01T00:00:00+00:00"),
                         ("refl-drop", "2026-02-01T00:00:00+00:00")):
        conn.execute(
            "INSERT INTO reflections (reflection_id, episode_id, what_happened,"
            " what_worked, what_failed, why, lesson, recommendation, confidence,"
            " importance, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "ep-6", "h", "w", "f", "y", "lesson " + rid, "r", "high", 0.5, created))
    # the link points at the OLDER row; the merge keeps the linked one and
    # must never leave a dangling link
    conn.execute("UPDATE episodic_memories SET reflection_id='refl-keep' WHERE episode_id='ep-6'")
    conn.commit()
    conn.close()

    reopened = SQLiteMemoryStore(db)
    rows = reopened.list_recent_reflections(limit=100)
    assert len(rows) == 1 and rows[0].reflection_id == "refl-keep"
    ep = reopened.get_episode("ep-6")
    assert ep is not None and ep.reflection_id == "refl-keep"
    reopened.close()


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


def _episode(episode_id, task_id):
    from arion.memory.models import Episode
    return Episode(
        episode_id=episode_id, task_id=task_id, goal_id="goal-1",
        goal="summarize the repository", outcome="completed",
        tags=["filesystem.read", "outcome:completed"],
    )


def _reflection(reflection_id, episode_id, lesson):
    return Reflection(
        reflection_id=reflection_id, episode_id=episode_id,
        what_happened="a task ran", what_worked="steps passed",
        what_failed="nothing", why="ok", lesson=lesson,
        recommendation="repeat", confidence="high", importance=0.6,
    )


def _assert_episode_unique_index(store: SQLiteMemoryStore) -> None:
    idx = store._conn.execute(
        "SELECT name FROM pragma_index_list('reflections') "
        "WHERE \"unique\"=1").fetchall()
    names = {row[0] for row in idx}
    assert any("episode" in n for n in names), f"no unique index on reflections.episode_id: {names}"
