"""Durable one-consolidation-per-source-set invariant (ADR-013 addendum).

A previous race allowed concurrent learning workers to create more than one
consolidation for the same canonical source-episode set:

    Worker A: list_consolidations() -> source set absent
    Worker B: list_consolidations() -> source set absent     (both pass the guard)
    Worker A: INSERT consolidation A  (id A)
    Worker B: INSERT consolidation B  (id B)                  (no set-keyed uniqueness)

The `consolidations` table only keyed uniqueness on `consolidation_id`, so two
concurrent workers could both persist a permanent duplicate consolidation for
the same experience (consolidations are never pruned, so this piles up). The
duplicate was also observed at the engine level as duplicate
`memory.consolidated` events.

This file proves the equivalent DURABLE guarantee for consolidations:

- one canonical source-episode set (ORDER-INDEPENDENT: [A,B,C] == [C,A,B]) ->
  exactly one durable consolidation (storage-level, cross-process);
- canonical source-set identity via a deterministic, order-independent key;
- deterministic concurrent consolidation race (barrier-synchronized);
- first-writer-wins claim; losers adopt the canonical consolidation;
- same-id re-record still refreshes content WITHOUT mutating the immutable
  source-set identity;
- legacy databases with duplicate consolidations are merged at init;
- concurrent engine learning emits exactly one creation event;
- episodes with task_id IS NULL survive SQLite store initialization (the
  init-time task-dedup must not silently delete task-less episodes).
"""

from __future__ import annotations

import json
import sqlite3
import threading

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.consolidation import MemoryConsolidator
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy, utcnow
from arion.state.store import SQLiteStorage

from conftest import MemorySink

FS = "filesystem:path"


def _episode(episode_id, task_id, created_at=None):
    from arion.memory.models import Episode
    return Episode(
        episode_id=episode_id, task_id=task_id, goal_id="goal-1",
        goal="read the missing file", outcome="failed",
        plan_summary=[{"index": 0, "intent": "read", "capability": "filesystem.read",
                       "action": "read", "status": "failed", "params_keys": ["path"]}],
        failures=[{"step": 0, "error": "not a file: 'missing.txt'", "category": "execution"}],
        tags=["filesystem.read", "outcome:failed", "category:execution"],
        importance=0.6, created_at=created_at or utcnow(), updated_at=created_at or utcnow(),
    )


def _consolidation(consolidation_id, source_ids, lesson, created_at=None):
    from arion.memory.store import ConsolidationRecord
    return ConsolidationRecord(
        consolidation_id=consolidation_id,
        source_episode_ids=list(source_ids),
        category="execution",
        merged_lesson=lesson,
        count=len(source_ids),
        importance=0.6,
        created_at=created_at or utcnow(),
    )


def _engine(db_path, sandbox, reflector=None, memory=None, sink=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage] + ([sink] if sink else []))
    planner = DeterministicPlanner()
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=reflector or DeterministicReflector(),
    )
    return engine


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


class _BarrierConsolidationStore:
    """Wraps a SQLiteMemoryStore; barriers all parties at record_consolidation.

    Every worker calls consolidate() which does its read-only PRE-CHECK
    (list_consolidations) BEFORE reaching the write. The barrier sits on the
    write seam, so it guarantees ALL workers observed the source set absent
    before ANY of them records - the exact check-then-act interleaving of the
    original race, made deterministic. All other store methods pass through.
    """

    def __init__(self, store, parties: int):
        self._store = store
        self._barrier = threading.Barrier(parties)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def record_consolidation(self, record):
        self._barrier.wait(timeout=60)
        return self._store.record_consolidation(record)


class _BarrierReflector:
    """Synchronizes N concurrent learners at the reflection seam.

    Mirrors the reflection-invariant test: both workers block in reflect()
    until all parties arrive, so neither can persist a reflection (and thus
    neither can be short-circuited by the early-return guard) before the
    other has reached the learning pipeline - both then proceed together to
    consolidation, making the engine-level race deterministic.
    """

    def __init__(self, parties: int):
        self._barrier = threading.Barrier(parties)
        self._inner = DeterministicReflector()

    def reflect(self, episode):
        self._barrier.wait(timeout=60)
        return self._inner.reflect(episode)


# ------------------------------------------------------------------ #
# order-independent canonical identity + sequential idempotency
# ------------------------------------------------------------------ #


def test_source_set_identity_is_order_independent(tmp_path):
    """[A,B,C], [C,A,B], [B,C,A] all denote the SAME source set and must
    converge on ONE canonical consolidation (order-independent identity)."""
    store = SQLiteMemoryStore(tmp_path / "order.db")
    first = store.record_consolidation(_consolidation("c-1", ["A", "B", "C"], "lesson one"))

    # the same source set in a different permutation with a NEW id loses
    # the claim and returns the canonical first-writer row
    got_perm1 = store.record_consolidation(_consolidation("c-2", ["C", "A", "B"], "lesson two"))
    got_perm2 = store.record_consolidation(_consolidation("c-3", ["B", "C", "A"], "lesson three"))

    assert first is not None and first.consolidation_id == "c-1"
    assert got_perm1 is not None and got_perm1.consolidation_id == "c-1"
    assert got_perm2 is not None and got_perm2.consolidation_id == "c-1"
    rows = store.list_consolidations(limit=100)
    assert len(rows) == 1 and rows[0].consolidation_id == "c-1"
    store.close()


def test_one_source_set_at_most_one_consolidation_idempotent(tmp_path):
    """Sequential repeated equivalent consolidation attempts never create a
    duplicate row."""
    store = SQLiteMemoryStore(tmp_path / "idem.db")
    store.record_episode(_episode("e1", "t-1"))
    store.record_episode(_episode("e2", "t-2"))
    consolidator = MemoryConsolidator(store)
    assert len(consolidator.consolidate(limit=100)) == 1
    assert len(consolidator.consolidate(limit=100)) == 0  # idempotent second pass
    assert len(store.list_consolidations(limit=100)) == 1
    store.close()


# ------------------------------------------------------------------ #
# storage-level claim semantics
# ------------------------------------------------------------------ #


def test_store_first_writer_wins_and_loser_adopts_canonical(tmp_path):
    """Two consolidations (different ids) for one source set: the durable row
    inserted first is canonical; the loser's row is not stored and the
    canonical consolidation is returned to BOTH callers."""
    store = SQLiteMemoryStore(tmp_path / "claim.db")
    got1 = store.record_consolidation(_consolidation("c-1", ["ep-a", "ep-b"], "first lesson"))
    got2 = store.record_consolidation(_consolidation("c-2", ["ep-b", "ep-a"], "second lesson"))

    assert got1 is not None and got1.consolidation_id == "c-1"
    assert got2 is not None and got2.consolidation_id == "c-1"  # canonical, not c-2
    rows = store.list_consolidations(limit=100)
    assert len(rows) == 1 and rows[0].consolidation_id == "c-1"
    store.close()


def test_same_id_rerecord_refreshes_without_mutating_source_identity(tmp_path):
    """Re-recording the SAME consolidation id updates its content in place
    (the historical refresh semantics) but must NEVER mutate the immutable
    source-set identity."""
    store = SQLiteMemoryStore(tmp_path / "refresh.db")
    got1 = store.record_consolidation(_consolidation("c-1", ["ep-a", "ep-b"], "old lesson"))
    # a tampered re-record claims the same id with a DIFFERENT source set -
    # the immutable source identity must be preserved
    got2 = store.record_consolidation(_consolidation("c-1", ["ep-x", "ep-y"], "a different lesson"))

    assert got1 is not None and got1.consolidation_id == "c-1"
    assert got2 is not None and got2.consolidation_id == "c-1"
    assert sorted(got2.source_episode_ids) == ["ep-a", "ep-b"]  # source identity immutable
    assert got2.merged_lesson == "a different lesson"            # content refreshed
    assert len(store.list_consolidations(limit=100)) == 1
    store.close()


# ------------------------------------------------------------------ #
# deterministic concurrent race (storage + consolidator)
# ------------------------------------------------------------------ #


def test_deterministic_concurrent_consolidation_race_single_row(tmp_path):
    """Two consolidator workers race the pre-check -> record boundary on the
    SAME candidate source set (barrier-synchronized so both observe it absent
    before either records). Exactly ONE durable consolidation may exist."""
    db = tmp_path / "race.db"
    store = SQLiteMemoryStore(db)
    for i in range(2):
        store.record_episode(_episode(f"e{i}", f"t-{i}"))
        ref = DeterministicReflector().reflect(store.get_episode(f"e{i}"))
        store.record_reflection(ref)
        store.link_reflection(f"e{i}", ref.reflection_id)
    barriered = _BarrierConsolidationStore(store, parties=2)
    consolidator = MemoryConsolidator(barriered)

    errors: list[Exception] = []

    def worker():
        try:
            consolidator.consolidate(limit=100)
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors

    rows = store.list_consolidations(limit=100)
    assert len(rows) == 1, f"concurrent workers created {len(rows)} consolidations"
    assert rows[0].count == 2
    store.close()


# ------------------------------------------------------------------ #
# cross-process / independent connection claim
# ------------------------------------------------------------------ #


def test_cross_process_claim_single_row(tmp_path):
    """A separate connection (fresh store = separate process view) racing the
    same source set must converge on the single durable first-writer row."""
    db = tmp_path / "cross.db"
    s1 = SQLiteMemoryStore(db)
    got1 = s1.record_consolidation(_consolidation("c-1", ["ep-a", "ep-b"], "first"))
    s1.close()

    s2 = SQLiteMemoryStore(db)  # independent connection / separate process
    got2 = s2.record_consolidation(_consolidation("c-2", ["ep-b", "ep-a"], "second"))

    assert got1 is not None and got1.consolidation_id == "c-1"
    assert got2 is not None and got2.consolidation_id == "c-1"
    assert len(s2.list_consolidations(limit=100)) == 1
    s2.close()


# ------------------------------------------------------------------ #
# concurrent engine learning
# ------------------------------------------------------------------ #


def test_concurrent_engine_learning_single_consolidation_single_event(tmp_path, sandbox):
    """Two engine learners race _record_memory for the same terminal task.
    Exactly one durable consolidation and exactly one memory.consolidated
    CREATION event may result (a worker that merely ADOPTS the canonical
    consolidation must not emit a duplicate event)."""
    db = tmp_path / "engrace.db"
    store = SQLiteMemoryStore(db)
    barriered_memory = _BarrierConsolidationStore(store, parties=2)
    sink = MemorySink()
    engine = _engine(db, sandbox, reflector=_BarrierReflector(2),
                     memory=barriered_memory, sink=sink)
    # Pre-existing similar experience (two failed episodes) -> exactly ONE
    # consolidation candidate. Both racing learners will attempt to record it,
    # so their `_consolidate` calls contend on the same source set.
    for i in range(2):
        store.record_episode(_episode(f"e{i}", f"t-{i}"))
        ref = DeterministicReflector().reflect(store.get_episode(f"e{i}"))
        store.record_reflection(ref)
        store.link_reflection(f"e{i}", ref.reflection_id)
    task = _terminal_task(engine)
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

    cons = store.list_consolidations(limit=100)
    assert len(cons) == 1, f"concurrent learning created {len(cons)} consolidations"
    assert sink.count("memory.consolidated") == 1, sink.by_kind("memory.consolidated")
    engine.storage.close()
    store.close()


# ------------------------------------------------------------------ #
# legacy-database migration
# ------------------------------------------------------------------ #


def _insert_legacy_consolidation(conn, cid, source_ids, lesson, created_at):
    conn.execute(
        "INSERT INTO consolidations (consolidation_id, source_episode_ids, category,"
        " merged_lesson, count, importance, created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, json.dumps(list(source_ids)), "execution", lesson,
         len(source_ids), 0.6, created_at),
    )


def test_init_merges_legacy_duplicate_consolidations(tmp_path):
    """Seed a legacy (pre-invariant) DB with two duplicate consolidations for
    the SAME source set (different orderings) - the race artifact. Reopening
    the store must merge to ONE survivor (newest by created_at, rowid) BEFORE
    enforcing the unique index."""
    db = tmp_path / "legacy.db"
    store = SQLiteMemoryStore(db)
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_consolidations_source_key")
    _insert_legacy_consolidation(conn, "c-old", ["ep-a", "ep-b"], "old", "2026-01-01T00:00:00+00:00")
    _insert_legacy_consolidation(conn, "c-new", ["ep-b", "ep-a"], "new", "2026-02-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    reopened = SQLiteMemoryStore(db)  # init-time backfill + merge + unique index
    rows = reopened.list_consolidations(limit=100)
    assert len(rows) == 1, f"expected one survivor, got {len(rows)}"
    assert rows[0].consolidation_id == "c-new"  # newest kept
    _assert_consolidation_unique_index(reopened)
    reopened.close()


def test_init_keeps_distinct_source_sets_distinct(tmp_path):
    """Distinct source sets are never merged; unique enforcement works after
    migration; a re-claim of an existing set still returns the canonical row."""
    db = tmp_path / "distinct.db"
    store = SQLiteMemoryStore(db)
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_consolidations_source_key")
    _insert_legacy_consolidation(conn, "c-a", ["ep-a", "ep-b"], "a", "2026-01-01T00:00:00+00:00")
    _insert_legacy_consolidation(conn, "c-b", ["ep-c", "ep-d"], "b", "2026-01-02T00:00:00+00:00")
    conn.commit()
    conn.close()

    reopened = SQLiteMemoryStore(db)
    rows = reopened.list_consolidations(limit=100)
    assert len(rows) == 2
    assert {r.consolidation_id for r in rows} == {"c-a", "c-b"}
    # unique enforcement works afterward
    got = reopened.record_consolidation(_consolidation("c-a2", ["ep-a", "ep-b"], "dup"))
    assert got is not None and got.consolidation_id == "c-a"
    assert len(reopened.list_consolidations(limit=100)) == 2
    reopened.close()


def test_reopen_is_idempotent(tmp_path):
    """Reopening the store over and over (after the unique index exists) never
    re-merges, never duplicates, and stays idempotent."""
    db = tmp_path / "reopen.db"
    store = SQLiteMemoryStore(db)
    store.record_consolidation(_consolidation("c-1", ["ep-a", "ep-b"], "lesson"))
    store.close()

    SQLiteMemoryStore(db).close()
    SQLiteMemoryStore(db).close()
    final = SQLiteMemoryStore(db)
    rows = final.list_consolidations(limit=100)
    assert len(rows) == 1 and rows[0].consolidation_id == "c-1"
    final.close()


# ------------------------------------------------------------------ #
# NULL task_id preservation during SQLite store initialization
# ------------------------------------------------------------------ #


def test_null_task_id_episode_preserved_on_reopen(tmp_path):
    """An episode with task_id IS NULL must NOT be silently deleted by the
    init-time task-deduplication (a NULL comparison bug in the legacy cleanup
    would otherwise empty the subquery and delete valid task-less episodes)."""
    db = tmp_path / "nulltask.db"
    store = SQLiteMemoryStore(db)
    store.record_episode(_episode("ep-taskless", None))  # task-less episode
    store.close()

    reopened = SQLiteMemoryStore(db)  # init-time dedup must preserve it
    ep = reopened.get_episode("ep-taskless")
    assert ep is not None and ep.episode_id == "ep-taskless"
    reopened.close()


def test_nonnull_task_dedup_still_works_on_reopen(tmp_path):
    """Ordinary (non-NULL) task_id deduplication still merges duplicate rows
    for the same task on reopen - the NULL fix must not disable it."""
    db = tmp_path / "dedup.db"
    store = SQLiteMemoryStore(db)
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_episodes_task_unique")
    cols = ("episode_id,task_id,goal,plan_summary,actions,resources,outcome,"
            "verification,failures,authorization,recovery,tags,importance,"
            "lifecycle,created_at,updated_at")
    for eid, created in (("ep-old", "2026-01-01T00:00:00+00:00"),
                         ("ep-new", "2026-02-01T00:00:00+00:00")):
        conn.execute(
            "INSERT INTO episodic_memories (" + cols + ") VALUES ("
            "'" + eid + "','task-1','goal-1','[]','[]','[]','failed','{}','[]',"
            "'{}','[]','[]',0.5,'recorded','" + created + "','" + created + "')")
    conn.commit()
    conn.close()

    reopened = SQLiteMemoryStore(db)  # init-time dedup keeps the newest per task
    eps = [e for e in reopened.list_recent(limit=100) if e.task_id == "task-1"]
    assert len(eps) == 1
    assert eps[0].episode_id == "ep-new"  # newest kept
    reopened.close()


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


def _assert_consolidation_unique_index(store: SQLiteMemoryStore) -> None:
    idx = store._conn.execute(
        "SELECT name FROM pragma_index_list('consolidations') "
        "WHERE \"unique\"=1").fetchall()
    names = {row[0] for row in idx}
    assert any("source_key" in n for n in names), \
        f"no unique index on consolidations.source_key: {names}"
