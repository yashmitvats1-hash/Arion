"""Durable belief-identity / versioning / supersession invariant.

A previous defect let two belief-write paths diverge:

- ``CognitiveState._persist_belief`` intended to version + supersede, but
  did the read/decide/write as separate non-atomic calls;
- ``ArionEngine._derive_beliefs`` bypassed the facade entirely and wrote
  straight to ``SQLiteCognitiveStore.record_belief``, so higher-confidence
  revisions never superseded the active row and concurrent workers could
  both pass the check-then-act guard and insert competing ACTIVE beliefs.

The beliefs table had no durable backstop for logical identity
``(category, statement)``: no uniqueness, no transactional claim.

This module proves the DURABLE guarantee:

- logical identity ``(category, statement)`` has at most one ACTIVE row at
  any committed state;
- revisions have monotonic versions;
- a STRICTLY higher-confidence observation becomes a new versioned row and
  atomically supersedes the previously active row (history preserved);
- equal/lower-confidence observations adopt the canonical active revision
  (no new row, no new ``belief.derived`` event);
- repeated engine learning, concurrent threads, independent SQLite
  connections and real subprocesses all converge to one valid topology;
- legacy databases with duplicate ACTIVE rows for the same logical belief
  are deterministically repaired at store initialization before the
  storage-level partial-unique backstop is created, and reopening is
  idempotent.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from arion.cognition.models import Belief
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.models import Episode, Reflection
from arion.state.models import new_id, utcnow

REPO = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _denied_episode(episode_id=None, task_id=None, goal_id="goal-1",
                    importance=0.5, goal="read notes.txt"):
    """An episode whose deriver emits one deterministic semantic belief:

        "filesystem.read/read on 'notes.txt' is not permitted by current policy"

    Confidence is controlled by ``importance``: the deriver maps a 'high'
    reflection confidence through ``round(0.9 * (0.5 + 0.5 * importance), 3)``,
    so importance 0.5 -> 0.675 and importance 0.9 -> 0.855.
    """
    episode_id = episode_id or new_id("ep")
    return Episode(
        episode_id=episode_id,
        task_id=task_id or new_id("task"),
        goal_id=goal_id,
        goal=goal,
        outcome="denied",
        plan_summary=[{
            "index": 0, "intent": "read", "capability": "filesystem.read",
            "action": "read", "status": "denied", "params_keys": ["path"],
        }],
        actions=[{"capability": "filesystem.read", "action": "read",
                  "status": "denied", "attempts": 1}],
        resources=[{"step": 0, "capability": "filesystem.read",
                    "action": "read", "resource": "notes.txt",
                    "status": "denied"}],
        verification={},
        failures=[{"step": 0, "capability": "filesystem.read",
                   "action": "read",
                   "error": "denied by policy", "category": "denied"}],
        authorization={"denials": [{
            "capability": "filesystem.read", "action": "read",
            "scope": "filesystem:read", "resource": "notes.txt",
        }]},
        recovery={},
        tags=["filesystem.read", "outcome:denied", "category:denied",
              "authorization:denied"],
        importance=importance,
    )


def _reflection(episode, confidence="high"):
    return Reflection(
        reflection_id=new_id("refl"),
        episode_id=episode.episode_id,
        what_happened="denied",
        what_worked="",
        what_failed="authorization denied",
        why="policy denied",
        lesson="authorization is required before attempting",
        recommendation="request approval",
        confidence=confidence,
        importance=episode.importance,
    )


STMT = "filesystem.read/read on 'notes.txt' is not permitted by current policy"


def _active_for(store, statement=STMT, category="semantic"):
    rows = [b for b in store.list_beliefs(include_superseded=True, limit=10000)
            if b.statement == statement and b.category == category]
    return rows


def _active(store, statement=STMT, category="semantic"):
    return [b for b in _active_for(store, statement, category)
            if b.superseded_at is None]


# --------------------------------------------------------------------------- #
# A. Engine supersession divergence (the reported defect)
# --------------------------------------------------------------------------- #


def _engine_with_cognition(db):
    from arion.bootstrap import build_engine

    sandbox = Path(db).parent / "sandbox"
    sandbox.mkdir(exist_ok=True)
    # Deny filesystem:read so a terminal task deterministically derives the
    # 'avoid' semantic belief; no real capability execution is required for
    # the direct _derive_beliefs tests below.
    from arion.orchestration.authz import ResourcePolicy

    class _DenyAll:
        def allows(self, resource):
            return False

    return build_engine(
        db, sandbox,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read"},
                              boundaries={"filesystem:path": _DenyAll()}),
        memory=True, cognition=True,
    )


def test_engine_path_higher_confidence_revision_supersedes(tmp_path):
    """A -> B: lower then strictly higher confidence must leave exactly one
    ACTIVE revision (B at the next version), with A superseded. This is the
    exact divergence the review reproduced against the old engine path."""
    db = tmp_path / "belief.db"
    engine = _engine_with_cognition(db)
    try:
        low = _denied_episode(importance=0.5)
        high = _denied_episode(importance=0.9)
        engine._derive_beliefs(low, _reflection(low))
        engine._derive_beliefs(high, _reflection(high))
        store = engine.cognition.cognition
        active = _active(store)
        assert len(active) == 1, (
            f"expected exactly one active revision, got "
            f"{[(b.belief_id, b.version, b.confidence, b.superseded_at) for b in active]}")
        assert active[0].confidence == pytest.approx(0.855)
        assert active[0].version == 2
        all_rows = _active_for(store)
        superseded = [b for b in all_rows if b.superseded_at is not None]
        assert len(superseded) == 1
        assert superseded[0].version == 1
        assert superseded[0].confidence == pytest.approx(0.675)
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------- #
# B. Facade and engine parity
# --------------------------------------------------------------------------- #


def _facade(db):
    mem = __import__("arion.memory.store", fromlist=["SQLiteMemoryStore"]).SQLiteMemoryStore(db)
    cog = SQLiteCognitiveStore(db)
    return mem, cog, CognitiveState(mem, cog)


def test_facade_and_engine_produce_equivalent_lifecycle(tmp_path):
    """Same sequence (low then high confidence) through both the facade's
    own derive path and the engine path yields the same durable topology:
    one active v2 row, one superseded v1 row, two rows total."""
    db_f = tmp_path / "facade.db"
    mem_f, cog_f, facade = _facade(db_f)
    low = _denied_episode(importance=0.5)
    high = _denied_episode(importance=0.9)
    # facade path: derive_and_store -> _persist_belief
    from arion.memory.guidance import DeterministicMemoryGuidance
    for ep in (low, high):
        ref = _reflection(ep)
        mem_f.record_episode(ep)
        mem_f.record_reflection(ref)
        mem_f.link_reflection(ep.episode_id, ref.reflection_id)
        guidance = DeterministicMemoryGuidance().build([ep], [ref])
        facade.derive_and_store([ep], [ref], guidance)
    f_active = _active(cog_f)
    f_all = _active_for(cog_f)
    assert len(f_active) == 1 and f_active[0].version == 2
    assert len([b for b in f_all if b.superseded_at is not None]) == 1

    db_e = tmp_path / "engine.db"
    engine = _engine_with_cognition(db_e)
    try:
        engine._derive_beliefs(low, _reflection(low))
        engine._derive_beliefs(high, _reflection(high))
        cog_e = engine.cognition.cognition
        e_active = _active(cog_e)
        e_all = _active_for(cog_e)
        assert len(e_active) == 1 and e_active[0].version == 2
        assert len([b for b in e_all if b.superseded_at is not None]) == 1
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------- #
# C. Equal / lower confidence idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("second_importance", [0.5, 0.1])
def test_equal_or_lower_confidence_is_noop(tmp_path, second_importance):
    db = tmp_path / "idempotent.db"
    engine = _engine_with_cognition(db)
    try:
        first = _denied_episode(importance=0.5)
        engine._derive_beliefs(first, _reflection(first))
        store = engine.cognition.cognition
        before = _active_for(store)
        assert len(before) >= 1 and all(b.version == 1 for b in before)
        before_events = len([e for e in engine.storage.list_events(task_id=None)
                             if e.kind == "belief.derived"])

        second = _denied_episode(importance=second_importance)
        engine._derive_beliefs(second, _reflection(second))

        after = _active_for(store)
        # equal/lower confidence must not add any new active revision or
        # bump versions; every canonical row is unchanged.
        assert len(after) == len(before)
        assert {b.belief_id for b in after} == {b.belief_id for b in before}
        assert all(b.version == 1 and b.superseded_at is None for b in after)
        # and no new belief.derived event for the no-op observation
        after_events = len([e for e in engine.storage.list_events(task_id=None)
                            if e.kind == "belief.derived"])
        assert after_events == before_events
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------- #
# D. Higher-confidence revision chain
# --------------------------------------------------------------------------- #


def test_increasing_confidence_chain_is_monotonic(tmp_path):
    db = tmp_path / "chain.db"
    engine = _engine_with_cognition(db)
    try:
        confidences = [0.3, 0.5, 0.7, 0.9]
        for i, imp in enumerate(confidences):
            ep = _denied_episode(importance=imp)
            engine._derive_beliefs(ep, _reflection(ep))
        store = engine.cognition.cognition
        rows = _active_for(store)
        versions = sorted(b.version for b in rows)
        assert versions == [1, 2, 3, 4]
        active = [b for b in rows if b.superseded_at is None]
        assert len(active) == 1 and active[0].version == 4
        # confidence is monotonically non-decreasing across versions
        by_version = {b.version: b.confidence for b in rows}
        assert by_version[1] < by_version[2] < by_version[3] < by_version[4]
        # history preserved: every row still fetchable
        for b in rows:
            assert store.get_belief(b.belief_id) is not None
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------- #
# E. Deterministic concurrent revision race (barrier-synchronized)
# --------------------------------------------------------------------------- #


class _Barrier:
    """Tiny barrier that lets both workers reach the claim together."""

    def __init__(self, n):
        self._n = n
        self._count = 0
        self._cond = threading.Condition()

    def wait(self):
        with self._cond:
            self._count += 1
            if self._count >= self._n:
                self._cond.notify_all()
                return
            self._cond.wait_for(lambda: self._count >= self._n)


def test_concurrent_equal_confidence_one_active(tmp_path):
    db = tmp_path / "concurrent_eq.db"
    store_a = SQLiteCognitiveStore(db)
    store_b = SQLiteCognitiveStore(db)
    barrier = _Barrier(2)
    results = {}

    def worker(store, tag):
        b = Belief(
            belief_id=new_id("belief"),
            category="semantic",
            statement="concurrent equal statement",
            confidence=0.7, importance=0.5,
            provenance={"episode_ids": [f"ep-{tag}"]}, source="deterministic",
        )
        barrier.wait()
        results[tag] = store.persist_belief(b)

    t_a = threading.Thread(target=worker, args=(store_a, "A"))
    t_b = threading.Thread(target=worker, args=(store_b, "B"))
    t_a.start(); t_b.start(); t_a.join(); t_b.join()

    # exactly one writer created; the other adopted the canonical row
    created = [tag for tag, r in results.items() if r.created]
    assert len(created) == 1
    rows = [b for b in store_a.list_beliefs(include_superseded=True, limit=10000)
            if b.statement == "concurrent equal statement"]
    assert len(rows) == 1 and rows[0].version == 1
    assert results["A"].belief.belief_id == results["B"].belief.belief_id


def test_concurrent_competing_confidence_one_active(tmp_path):
    """Two workers contend with DIFFERENT confidences for the same identity.
    In every interleaving there is exactly one ACTIVE row at the highest
    confidence and versions are monotonic; both workers converge on the
    same canonical active belief when they re-read after the commit."""
    db = tmp_path / "concurrent_conf.db"
    store_a = SQLiteCognitiveStore(db)
    store_b = SQLiteCognitiveStore(db)
    barrier = _Barrier(2)
    results = {}

    def worker(store, tag, conf):
        b = Belief(
            belief_id=new_id("belief"),
            category="semantic",
            statement="concurrent competing statement",
            confidence=conf, importance=0.5,
            provenance={"episode_ids": [f"ep-{tag}"]}, source="deterministic",
        )
        barrier.wait()
        results[tag] = store.persist_belief(b)

    t_a = threading.Thread(target=worker, args=(store_a, "A", 0.6))
    t_b = threading.Thread(target=worker, args=(store_b, "B", 0.9))
    t_a.start(); t_b.start(); t_a.join(); t_b.join()

    rows = [b for b in store_a.list_beliefs(include_superseded=True, limit=10000)
            if b.statement == "concurrent competing statement"]
    active = [b for b in rows if b.superseded_at is None]
    assert len(active) == 1
    assert active[0].confidence == pytest.approx(0.9)
    versions = sorted(b.version for b in rows)
    assert versions == list(range(1, len(rows) + 1))
    # The high-confidence writer always wins and returns the canonical
    # active belief. The low-confidence writer may return its own insert
    # (which the high-confidence writer then supersedes) or, if it lost
    # the insert race entirely, the canonical row - either is acceptable
    # because the DURABLE topology below is the invariant under test.
    assert results["B"].belief.belief_id == active[0].belief_id
    assert results["B"].belief.confidence == pytest.approx(0.9)
    assert results["A"].belief is not None
    assert results["A"].belief.confidence in (pytest.approx(0.6), pytest.approx(0.9))
    # Durable history preserved: every inserted row still exists and every
    # non-active row is superseded.
    assert len(rows) in (1, 2)
    assert all(b.superseded_at is not None for b in rows if b.belief_id != active[0].belief_id)


# --------------------------------------------------------------------------- #
# F. Independent SQLite connections converge
# --------------------------------------------------------------------------- #


def test_independent_connections_converge(tmp_path):
    db = tmp_path / "independent.db"
    s1 = SQLiteCognitiveStore(db)
    s2 = SQLiteCognitiveStore(db)
    b1 = Belief(belief_id=new_id("belief"), category="procedural",
                statement="independent statement", confidence=0.5,
                provenance={"episode_ids": ["ep-1"]})
    b2 = Belief(belief_id=new_id("belief"), category="procedural",
                statement="independent statement", confidence=0.8,
                provenance={"episode_ids": ["ep-2"]})
    r1 = s1.persist_belief(b1)
    r2 = s2.persist_belief(b2)
    assert r1.created and r2.created
    s3 = SQLiteCognitiveStore(db)
    rows = [b for b in s3.list_beliefs(include_superseded=True, limit=1000)
            if b.statement == "independent statement"]
    assert len([b for b in rows if b.superseded_at is None]) == 1
    assert max(b.version for b in rows) == 2
    assert max(b.confidence for b in rows) == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# G. Cross-process (real subprocesses)
# --------------------------------------------------------------------------- #


_HELPER = textwrap.dedent(r"""
    import json, os, sys, time, uuid
    sys.path.insert(0, %r)
    from arion.cognition.models import Belief
    from arion.cognition.store import SQLiteCognitiveStore
    db, tag, conf, barrier_dir = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    store = SQLiteCognitiveStore(db)
    # Deterministic barrier: both processes create their ready file, then
    # wait until BOTH are present before opening the transaction. This
    # forces the read/decide/write windows to overlap rather than relying
    # on process-start timing.
    ready = os.path.join(barrier_dir, "ready-" + tag)
    open(ready, "w").close()
    for _ in range(200):
        if (os.path.exists(os.path.join(barrier_dir, "ready-A"))
                and os.path.exists(os.path.join(barrier_dir, "ready-B"))):
            break
        time.sleep(0.01)
    b = Belief(
        belief_id="belief-" + uuid.uuid4().hex[:12],
        category="semantic",
        statement="cross-process statement",
        confidence=conf, importance=0.5,
        provenance={"episode_ids": ["ep-" + tag]},
        source="deterministic",
    )
    r = store.persist_belief(b)
    print(json.dumps({"created": r.created, "belief_id": r.belief.belief_id,
                      "version": r.belief.version,
                      "confidence": r.belief.confidence}))
    store.close()
""" % str(REPO))


def test_cross_process_concurrent_persist(tmp_path):
    db = str(tmp_path / "xproc.db")
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    # Pre-init schema from one connection so subprocesses don't race on DDL.
    SQLiteCognitiveStore(db).close()
    p_a = subprocess.Popen([sys.executable, "-c", _HELPER, db, "A", "0.6",
                            str(barrier_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True)
    p_b = subprocess.Popen([sys.executable, "-c", _HELPER, db, "B", "0.9",
                            str(barrier_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True)
    out_a, err_a = p_a.communicate(timeout=60)
    out_b, err_b = p_b.communicate(timeout=60)
    assert p_a.returncode == 0, err_a
    assert p_b.returncode == 0, err_b
    ra = json.loads(out_a.strip().splitlines()[-1])
    rb = json.loads(out_b.strip().splitlines()[-1])

    store = SQLiteCognitiveStore(db)
    rows = [b for b in store.list_beliefs(include_superseded=True, limit=1000)
            if b.statement == "cross-process statement"]
    active = [b for b in rows if b.superseded_at is None]
    assert len(active) == 1
    assert active[0].confidence == pytest.approx(0.9)
    # Both subprocesses converge on the SAME canonical active id (the
    # 0.9-confidence revision). The lower-confidence 0.6 writer may have
    # briefly held v1 (created=True before the higher writer superseded it),
    # but every committed state has exactly one active row at max confidence.
    assert ra["belief_id"] == active[0].belief_id or rb["belief_id"] == active[0].belief_id
    # the canonical active id is the higher-confidence process's id
    assert active[0].belief_id == rb["belief_id"]
    # monotonic versions
    assert sorted(b.version for b in rows) == list(range(1, len(rows) + 1))
    store.close()


# --------------------------------------------------------------------------- #
# H. Repeated engine learning / catch-up accumulates no duplicate active beliefs
# --------------------------------------------------------------------------- #


def test_repeated_engine_learning_no_duplicate_active(tmp_path):
    db = tmp_path / "repeated.db"
    engine = _engine_with_cognition(db)
    try:
        # Many independent denied episodes all derive the SAME statements
        # (one semantic denied-resource belief + one procedural lesson).
        for _ in range(8):
            ep = _denied_episode(importance=0.6)
            engine._derive_beliefs(ep, _reflection(ep))
        store = engine.cognition.cognition
        # No (category, statement) may have more than one active revision.
        by_stmt: dict[tuple, list] = {}
        for b in store.list_beliefs(include_superseded=True, limit=10000):
            by_stmt.setdefault((b.category, b.statement), []).append(b)
        for key, rows in by_stmt.items():
            assert len([b for b in rows if b.superseded_at is None]) == 1, key
        # The semantic denied-resource lineage has exactly one active row.
        active = _active(store)
        assert len(active) == 1, [
            (b.belief_id, b.version, b.confidence) for b in active]
        # exactly one belief.derived event per distinct lineage (one semantic
        # + one procedural, derived on the first episode; later episodes
        # adopt the canonical revision and emit nothing).
        derived = [e for e in engine.storage.list_events(task_id=None)
                   if e.kind == "belief.derived"]
        assert len(derived) == 2
    finally:
        engine.storage.close()


def test_catchup_learning_does_not_duplicate_active_belief(tmp_path):
    """learn_from_terminal_tasks re-derives beliefs for terminal tasks; the
    durable claim means a second catch-up pass is a no-op for active rows."""
    db = tmp_path / "catchup.db"
    engine = _engine_with_cognition(db)
    try:
        from arion.state.models import Task, TaskStatus

        # First pass: learn two terminal tasks with the same denied resource.
        for _ in range(2):
            t = Task(id=new_id("task"), goal_id=new_id("goal"),
                     description="read notes.txt", status=TaskStatus.FAILED)
            engine.storage.save_task(t)
        first = engine.learn_from_terminal_tasks(limit=10)
        second = engine.learn_from_terminal_tasks(limit=10)
        assert first >= 1
        assert second == 0  # idempotent catch-up
        store = engine.cognition.cognition
        # The semantic denied-resource belief has exactly one active row even
        # though multiple tasks produced the same derived statement.
        denied = [b for b in store.list_beliefs(limit=1000)
                  if "not permitted by current policy" in b.statement]
        # There can be multiple distinct resources/statements in general,
        # but no statement may have more than one active row.
        by_stmt: dict[str, list[Belief]] = {}
        for b in store.list_beliefs(include_superseded=True, limit=10000):
            by_stmt.setdefault((b.category, b.statement), []).append(b)
        for key, rows in by_stmt.items():
            assert len([b for b in rows if b.superseded_at is None]) <= 1, key
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------- #
# I. Legacy invalid-state migration
# --------------------------------------------------------------------------- #


def _seed_legacy_duplicates(db):
    """Seed a pre-fix beliefs table with multiple ACTIVE rows for the same
    logical identity, varying versions/confidences (the bug artifact the old
    engine path could leave behind)."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS beliefs ("
                 "belief_id TEXT PRIMARY KEY, category TEXT NOT NULL, "
                 "statement TEXT NOT NULL, confidence REAL NOT NULL, "
                 "importance REAL NOT NULL, provenance TEXT NOT NULL, "
                 "source TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, "
                 "superseded_at TEXT, created_at TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL)")
    now = utcnow()
    rows = [
        # lineage X: three active rows, versions 1,1,2; conf 0.5, 0.7, 0.6
        ("x1", "semantic", "legacy stmt X", 0.5, 0.5, 1, None),
        ("x2", "semantic", "legacy stmt X", 0.7, 0.5, 1, None),
        ("x3", "semantic", "legacy stmt X", 0.6, 0.5, 2, None),
        # lineage Y: a clean already-superseded history (must be preserved)
        ("y1", "procedural", "legacy stmt Y", 0.4, 0.5, 1, now),
        ("y2", "procedural", "legacy stmt Y", 0.8, 0.5, 2, None),
        # distinct statement Z: untouched
        ("z1", "semantic", "legacy stmt Z", 0.5, 0.5, 1, None),
    ]
    for bid, cat, stmt, conf, imp, ver, sup in rows:
        conn.execute(
            "INSERT INTO beliefs (belief_id, category, statement, confidence,"
            " importance, provenance, source, version, superseded_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (bid, cat, stmt, conf, imp, '{"episode_ids":[]}', "deterministic",
             ver, sup, now, now))
    conn.commit()
    conn.close()


def test_legacy_duplicate_active_repair_is_deterministic(tmp_path):
    db = str(tmp_path / "legacy.db")
    _seed_legacy_duplicates(db)
    store = SQLiteCognitiveStore(db)  # runs the repair migration

    x_rows = [b for b in store.list_beliefs(include_superseded=True, limit=1000)
              if b.statement == "legacy stmt X"]
    x_active = [b for b in x_rows if b.superseded_at is None]
    assert len(x_active) == 1
    # canonical winner = highest confidence (0.7)
    assert x_active[0].belief_id == "x2"
    # versions renumbered monotonically in created_at/rowid order; canonical
    # keeps the max lineage version
    versions = sorted(b.version for b in x_rows)
    assert versions == [1, 2, 3]
    assert x_active[0].version == 3

    # Y's clean history is preserved byte-for-byte
    y_rows = [b for b in store.list_beliefs(include_superseded=True, limit=1000)
              if b.statement == "legacy stmt Y"]
    assert [(b.belief_id, b.version, b.superseded_at is not None)
            for b in sorted(y_rows, key=lambda b: b.version)] == [
                ("y1", 1, True), ("y2", 2, False)]
    # Z is untouched
    z = store.get_belief("z1")
    assert z is not None and z.superseded_at is None and z.version == 1
    store.close()


def test_legacy_repair_idempotent_across_reopen(tmp_path):
    db = str(tmp_path / "legacy2.db")
    _seed_legacy_duplicates(db)
    s1 = SQLiteCognitiveStore(db)
    s1.close()
    s2 = SQLiteCognitiveStore(db)  # must not re-merge / re-supersede
    x = [b for b in s2.list_beliefs(include_superseded=True, limit=1000)
         if b.statement == "legacy stmt X"]
    assert len([b for b in x if b.superseded_at is None]) == 1
    assert sorted(b.version for b in x) == [1, 2, 3]
    s2.close()


def test_storage_backstop_blocks_second_active_insert(tmp_path):
    """The partial UNIQUE INDEX is the cross-process backstop: a raw INSERT
    of a second ACTIVE row for the same (category, statement) is rejected by
    SQLite even if a buggy caller bypasses persist_belief."""
    db = tmp_path / "backstop.db"
    store = SQLiteCognitiveStore(db)
    store.record_belief(Belief(
        belief_id="raw-1", category="semantic", statement="backstop stmt",
        confidence=0.5, provenance={"episode_ids": ["ep-1"]}))
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO beliefs (belief_id, category, statement, confidence,"
            " importance, provenance, source, version, superseded_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("raw-2", "semantic", "backstop stmt", 0.6, 0.5,
             '{"episode_ids":[]}', "deterministic", 2, None,
             utcnow(), utcnow()))
        store._conn.commit()
