"""SQLite-backed memory store (ADR-012).

Episodes and reflections live in their own tables (same DB file as core state,
so restart persistence is automatic). Structured JSON columns keep the schema
stable while the payload grows. Text retrieval is deterministic (substring/
structured filters) - no embeddings or vector DB (deferred by decision).

NEVER stored: raw prompts, raw model responses, credentials, auth tokens,
param VALUES (only param key names in plan_summary).
"""

from __future__ import annotations

import json
import functools
import sqlite3
import threading
from pathlib import Path
from typing import Any

from arion.memory.models import Episode, EpisodeFilter, Reflection
from arion.state.models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_memories (
    episode_id    TEXT PRIMARY KEY,
    task_id       TEXT,
    goal_id       TEXT,
    goal          TEXT NOT NULL,
    plan_summary  TEXT NOT NULL,
    actions       TEXT NOT NULL,
    resources     TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    verification  TEXT NOT NULL,
    failures      TEXT NOT NULL,
    authorization TEXT NOT NULL,
    recovery      TEXT NOT NULL,
    tags          TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,
    reflection_id TEXT,
    lifecycle     TEXT NOT NULL DEFAULT 'recorded',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_task    ON episodic_memories(task_id);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodic_memories(created_at);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodic_memories(outcome);
CREATE INDEX IF NOT EXISTS idx_episodes_tags    ON episodic_memories(tags);
CREATE TABLE IF NOT EXISTS reflections (
    reflection_id TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL,
    what_happened TEXT NOT NULL,
    what_worked   TEXT NOT NULL,
    what_failed   TEXT NOT NULL,
    why           TEXT NOT NULL,
    lesson        TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence    TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflections_episode ON reflections(episode_id);
CREATE INDEX IF NOT EXISTS idx_reflections_created ON reflections(created_at);
CREATE TABLE IF NOT EXISTS consolidations (
    consolidation_id TEXT PRIMARY KEY,
    source_key       TEXT,
    source_episode_ids TEXT NOT NULL,
    category TEXT NOT NULL,
    merged_lesson TEXT NOT NULL,
    count INTEGER NOT NULL,
    importance REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


def canonical_source_key(source_episode_ids) -> str:
    """Deterministic, ORDER-INDEPENDENT identity for a source episode set.

    The same set of episode ids always produces the same key regardless of
    input ordering - e.g. [A,B,C], [C,A,B] and [B,C,A] all resolve to
    '["A","B","C"]'. This is the durable identity on which the
    one-consolidation-per-source-set invariant is keyed (ADR-013 addendum).
    """
    ids = sorted(str(i) for i in (source_episode_ids or []))
    return json.dumps(ids)


class ConsolidationRecord:
    """An explicit, explainable consolidation of similar episodes."""

    def __init__(self, consolidation_id, source_episode_ids, category, merged_lesson, count, importance, created_at, source_key=None):
        self.consolidation_id = consolidation_id
        self.source_episode_ids = source_episode_ids
        self.source_key = source_key
        self.category = category
        self.merged_lesson = merged_lesson
        self.count = count
        self.importance = importance
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "consolidation_id": self.consolidation_id,
            "source_episode_ids": self.source_episode_ids,
            "source_key": self.source_key,
            "category": self.category,
            "merged_lesson": self.merged_lesson,
            "count": self.count,
            "importance": self.importance,
            "created_at": self.created_at,
        }

_EPISODE_COLS = [
    "episode_id", "task_id", "goal_id", "goal", "plan_summary", "actions", "resources",
    "outcome", "verification", "failures", "authorization", "recovery",
    "tags", "importance", "reflection_id", "lifecycle", "created_at", "updated_at",
]
_REFLECTION_COLS = [
    "reflection_id", "episode_id", "what_happened", "what_worked", "what_failed",
    "why", "lesson", "recommendation", "confidence", "importance", "created_at",
]


def _threadsafe(method):
    """Guard public methods with the connection's RLock (ADR-026:
    cross-process engines may drive a store from different threads)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._sql_lock:
            return method(self, *args, **kwargs)

    return wrapper


class SQLiteMemoryStore:
    """Durable SQLite episodic memory + reflections."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._sql_lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        # ADR-013 addendum: exactly one episode per task. Older databases may
        # contain duplicate rows for the same task (pre-idempotency bug
        # artifact); merge them here, keeping the newest row per task. This
        # is a duplicate merge, NOT archival pruning (memory is never
        # deleted otherwise). Then enforce the invariant at the DB level as
        # the cross-process backstop.
        try:
            # Guard the task-dedup with `task_id IS NOT NULL`: a NULL
            # comparison in SQL is never true, so without this guard the
            # subquery `m2.task_id = episodic_memories.task_id` matches no
            # row for a task-less episode (task_id IS NULL), leaving the
            # `NOT IN (...)` predicate true and silently DELETING valid
            # task-less episodes at initialization. The guard keeps ordinary
            # per-task dedup for non-NULL task_id values unchanged while
            # preserving NULL-task_id episodes (which the task-keyed unique
            # index already allows multiple of).
            self._conn.execute(
                "DELETE FROM episodic_memories WHERE task_id IS NOT NULL "
                "AND episode_id NOT IN ("
                "SELECT episode_id FROM episodic_memories m2 WHERE "
                "m2.task_id = episodic_memories.task_id ORDER BY updated_at "
                "DESC, rowid DESC LIMIT 1)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_task_unique "
                "ON episodic_memories(task_id)")
            # ADR-013 addendum (reflection invariant): at most ONE
            # reflection per episode, enforced at the DB level. Older
            # databases may hold duplicates from the pre-invariant learning
            # race; merge them (a bug-artifact merge, never archival
            # pruning): keep the episode's LINKED reflection, else the
            # NEWEST row, and repair a link that pointed at a losing
            # duplicate. The unique index then guarantees the invariant
            # cross-process from here on.
            dup = self._conn.execute(
                "SELECT 1 FROM (SELECT episode_id FROM reflections"
                " GROUP BY episode_id HAVING COUNT(*) > 1 LIMIT 1)").fetchone()
            if dup is not None:
                episode_ids = [r[0] for r in self._conn.execute(
                    "SELECT DISTINCT episode_id FROM reflections").fetchall()]
                for ep_id in episode_ids:
                    rows = self._conn.execute(
                        "SELECT reflection_id FROM reflections WHERE episode_id=?"
                        " ORDER BY created_at DESC, rowid DESC", (ep_id,)).fetchall()
                    ids = {r[0] for r in rows}
                    linked = self._conn.execute(
                        "SELECT reflection_id FROM episodic_memories"
                        " WHERE episode_id=?", (ep_id,)).fetchone()
                    keep = linked[0] if linked and linked[0] in ids else rows[0][0]
                    if linked and linked[0] != keep:
                        self._conn.execute(
                            "UPDATE episodic_memories SET reflection_id=?"
                            " WHERE episode_id=?", (keep, ep_id))
                    if len(rows) > 1:
                        self._conn.execute(
                            "DELETE FROM reflections WHERE episode_id=?"
                            " AND reflection_id<>?", (ep_id, keep))
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reflections_episode_unique "
                "ON reflections(episode_id)")
            # ADR-013 addendum (consolidation invariant): at most ONE
            # consolidation per canonical source-episode set, enforced at
            # the DB level. Add the canonical source_key column if the
            # existing database predates it, backfill canonical source keys
            # from the stored source episode ids, then MERGE legacy
            # duplicates that share a source key (keep the newest row by
            # created_at, rowid - a bug-artifact merge, never archival
            # pruning). Only after duplicates are repaired is the unique
            # index created, so it is the structural cross-process backstop
            # from here on. Malformed legacy rows that cannot produce a
            # valid canonical key are left NULL (SQLite allows multiple NULLs
            # in a unique index) rather than blocking it.
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(consolidations)").fetchall()]
            if "source_key" not in cols:
                self._conn.execute(
                    "ALTER TABLE consolidations ADD COLUMN source_key TEXT")
            for (cid,) in self._conn.execute(
                "SELECT consolidation_id FROM consolidations "
                "WHERE source_key IS NULL").fetchall():
                row = self._conn.execute(
                    "SELECT source_episode_ids FROM consolidations "
                    "WHERE consolidation_id=?", (cid,)).fetchone()
                key = None
                try:
                    ids = json.loads(row[0])
                    if isinstance(ids, list):
                        key = canonical_source_key(ids)
                except (ValueError, TypeError):
                    key = None
                self._conn.execute(
                    "UPDATE consolidations SET source_key=? "
                    "WHERE consolidation_id=?", (key, cid))
            dup = self._conn.execute(
                "SELECT 1 FROM (SELECT source_key FROM consolidations "
                "WHERE source_key IS NOT NULL GROUP BY source_key "
                "HAVING COUNT(*)>1 LIMIT 1)").fetchone()
            if dup is not None:
                keys = [r[0] for r in self._conn.execute(
                    "SELECT DISTINCT source_key FROM consolidations "
                    "WHERE source_key IS NOT NULL").fetchall()]
                for key in keys:
                    rows = self._conn.execute(
                        "SELECT consolidation_id FROM consolidations "
                        "WHERE source_key=? ORDER BY created_at DESC, "
                        "rowid DESC", (key,)).fetchall()
                    keep = rows[0][0]
                    if len(rows) > 1:
                        self._conn.execute(
                            "DELETE FROM consolidations WHERE source_key=? "
                            "AND consolidation_id<>?", (key, keep))
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_consolidations_source_key "
                "ON consolidations(source_key)")
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    # ---- episodes ----

    @_threadsafe
    def record_episode(self, episode: Episode) -> Episode | None:
        """Record/refresh an episode; returns the CANONICAL durable episode.

        DURABLE IDENTITY CLAIM (one episode per task, and a reflection link
        that a re-record can never clobber - ADR-013 addendum). The whole
        claim runs inside one BEGIN IMMEDIATE transaction, so concurrent
        threads AND separate processes serialize correctly:

        - a fresh task claims the episode slot: the FIRST writer wins the
          identity; a racing worker that minted its own episode_id for the
          same task LOSES and is returned the canonical row (its content is
          dropped - never a second row, never a replaced identity);
        - re-recording the SAME episode id refreshes content in place while
          PRESERVING the durable reflection link (a fresh build with
          reflection_id=None can no longer orphan a linked reflection) and
          never regressing the learning lifecycle;
        - the canonical row is re-read and returned so every subsequent
          operation (reflection claim, link, lifecycle) targets the
          durable identity.
        """
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            canonical = self._canonical_episode_row(episode)
            if canonical is None:
                self._conn.execute(
                    "INSERT INTO episodic_memories "
                    f"({', '.join(_EPISODE_COLS)}) VALUES ({', '.join('?' * len(_EPISODE_COLS))})",
                    (
                        episode.episode_id,
                        episode.task_id,
                        episode.goal_id,
                        episode.goal,
                        json.dumps(episode.plan_summary),
                        json.dumps(episode.actions),
                        json.dumps(episode.resources),
                        episode.outcome,
                        json.dumps(episode.verification),
                        json.dumps(episode.failures),
                        json.dumps(episode.authorization),
                        json.dumps(episode.recovery),
                        json.dumps(episode.tags),
                        float(episode.importance),
                        episode.reflection_id,
                        episode.lifecycle,
                        episode.created_at,
                        episode.updated_at,
                    ),
                )
            elif canonical.episode_id == episode.episode_id:
                # same identity: refresh content, PRESERVE the durable
                # reflection link + created_at, never regress lifecycle.
                self._conn.execute(
                    "UPDATE episodic_memories SET "
                    "task_id=COALESCE(?, task_id), goal_id=?, goal=?,"
                    " plan_summary=?, actions=?, resources=?, outcome=?,"
                    " verification=?, failures=?, authorization=?, recovery=?,"
                    " tags=?, importance=?, updated_at=?,"
                    " reflection_id=COALESCE(?, reflection_id),"
                    " lifecycle=(CASE WHEN (CASE ? WHEN 'consolidated' THEN 2"
                    " WHEN 'reflected' THEN 1 ELSE 0 END) >"
                    " (CASE lifecycle WHEN 'consolidated' THEN 2"
                    " WHEN 'reflected' THEN 1 ELSE 0 END)"
                    " THEN ? ELSE lifecycle END)"
                    " WHERE episode_id=?",
                    (
                        episode.task_id,
                        episode.goal_id,
                        episode.goal,
                        json.dumps(episode.plan_summary),
                        json.dumps(episode.actions),
                        json.dumps(episode.resources),
                        episode.outcome,
                        json.dumps(episode.verification),
                        json.dumps(episode.failures),
                        json.dumps(episode.authorization),
                        json.dumps(episode.recovery),
                        json.dumps(episode.tags),
                        float(episode.importance),
                        episode.updated_at,
                        episode.reflection_id,
                        episode.lifecycle,
                        episode.lifecycle,
                        episode.episode_id,
                    ),
                )
            # else: lost the identity race (canonical belongs to the same
            # task under a different id) - adopt the canonical row as-is.
            row = self._conn.execute(
                "SELECT " + ", ".join(_EPISODE_COLS)
                + " FROM episodic_memories WHERE episode_id=?",
                (canonical.episode_id if canonical is not None else episode.episode_id,),
            ).fetchone()
            self._conn.commit()
            return _episode_from_row(row) if row else None
        except sqlite3.Error:
            self._conn.rollback()
            raise

    def _canonical_episode_row(self, episode: Episode) -> Episode | None:
        """The durable canonical episode for this episode's task (preferred)
        or id, or None when the slot is unclaimed."""
        if episode.task_id:
            row = self._conn.execute(
                "SELECT " + ", ".join(_EPISODE_COLS)
                + " FROM episodic_memories WHERE task_id=?",
                (episode.task_id,),
            ).fetchone()
            if row is not None:
                return _episode_from_row(row)
        row = self._conn.execute(
            "SELECT " + ", ".join(_EPISODE_COLS)
            + " FROM episodic_memories WHERE episode_id=?",
            (episode.episode_id,),
        ).fetchone()
        return _episode_from_row(row) if row else None

    @_threadsafe
    def get_episode(self, episode_id: str) -> Episode | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_EPISODE_COLS) + " FROM episodic_memories WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        return _episode_from_row(row) if row else None

    @_threadsafe
    def get_episode_by_task(self, task_id: str) -> Episode | None:
        """Exactly one episode per task (ADR-013 addendum): the task-keyed
        unique index guarantees at most one row."""
        row = self._conn.execute(
            "SELECT " + ", ".join(_EPISODE_COLS)
            + " FROM episodic_memories WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return _episode_from_row(row) if row else None

    @_threadsafe
    def set_episode_lifecycle(self, episode_id: str, state: str) -> None:
        """Advance the durable learning lifecycle state (recorded ->
        reflected -> consolidated). Observational bookkeeping only."""
        if state not in ("recorded", "reflected", "consolidated"):
            raise ValueError(f"unknown lifecycle state {state!r}")
        self._conn.execute(
            "UPDATE episodic_memories SET lifecycle=?, updated_at=? "
            "WHERE episode_id=?",
            (state, utcnow(), episode_id),
        )
        self._conn.commit()

    @_threadsafe
    def search_episodes(self, filters: EpisodeFilter) -> list[Episode]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.outcome:
            clauses.append("outcome = ?")
            params.append(filters.outcome)
        if filters.capability:
            clauses.append("tags LIKE ?")
            params.append(f"%{filters.capability}%")
        if filters.failure_category:
            clauses.append("failures LIKE ?")
            params.append(f"%{filters.failure_category}%")
        if filters.tag:
            clauses.append("tags LIKE ?")
            params.append(f"%{filters.tag}%")
        if filters.text:
            clauses.append("goal LIKE ?")
            params.append(f"%{filters.text}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT {', '.join(_EPISODE_COLS)} FROM episodic_memories{where} "
            "ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, filters.limit)),
        ).fetchall()
        return [_episode_from_row(r) for r in rows]

    @_threadsafe
    def list_recent(self, limit: int = 10) -> list[Episode]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_EPISODE_COLS)} FROM episodic_memories ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_episode_from_row(r) for r in rows]

    # ---- reflections ----

    @_threadsafe
    def record_reflection(self, reflection: Reflection) -> Reflection | None:
        """Persist a reflection; returns the CANONICAL durable reflection.

        ONE REFLECTION PER EPISODE (ADR-013 addendum), durable across
        threads and processes (whole claim inside BEGIN IMMEDIATE, backed
        by the unique index on reflections.episode_id):

        - re-recording the SAME reflection_id refreshes its content in
          place (the historical INSERT OR REPLACE semantics);
        - a NEW reflection_id for an episode that already has one LOSES
          the claim: nothing is stored and the durable first-writer row is
          returned, so concurrent learners adopt the canonical reflection
          instead of duplicating it.
        """
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE reflections SET what_happened=?, what_worked=?,"
                " what_failed=?, why=?, lesson=?, recommendation=?,"
                " confidence=?, importance=?, created_at=?"
                " WHERE reflection_id=?",
                (
                    reflection.what_happened,
                    reflection.what_worked,
                    reflection.what_failed,
                    reflection.why,
                    reflection.lesson,
                    reflection.recommendation,
                    reflection.confidence,
                    float(reflection.importance),
                    reflection.created_at,
                    reflection.reflection_id,
                ),
            )
            if cur.rowcount == 0:
                try:
                    self._conn.execute(
                        "INSERT INTO reflections "
                        f"({', '.join(_REFLECTION_COLS)}) VALUES ({', '.join('?' * len(_REFLECTION_COLS))})",
                        (
                            reflection.reflection_id,
                            reflection.episode_id,
                            reflection.what_happened,
                            reflection.what_worked,
                            reflection.what_failed,
                            reflection.why,
                            reflection.lesson,
                            reflection.recommendation,
                            reflection.confidence,
                            float(reflection.importance),
                            reflection.created_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # the episode's single reflection slot is already taken:
                    # first writer wins - the canonical row is returned below
                    pass
            row = self._conn.execute(
                "SELECT " + ", ".join(_REFLECTION_COLS)
                + " FROM reflections WHERE episode_id=?",
                (reflection.episode_id,),
            ).fetchone()
            self._conn.commit()
            return _reflection_from_row(row) if row else None
        except sqlite3.Error:
            self._conn.rollback()
            raise

    @_threadsafe
    def get_reflection(self, reflection_id: str) -> Reflection | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_REFLECTION_COLS) + " FROM reflections WHERE reflection_id=?",
            (reflection_id,),
        ).fetchone()
        return _reflection_from_row(row) if row else None

    @_threadsafe
    def list_recent_reflections(self, limit: int = 10) -> list[Reflection]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_REFLECTION_COLS)} FROM reflections ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_reflection_from_row(r) for r in rows]

    @_threadsafe
    def count_reflections(self) -> int:
        """Bounded read-only count of reflection rows (ADR-014 prune event)."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM reflections"
        ).fetchone()[0]

    @_threadsafe
    def link_reflection(self, episode_id: str, reflection_id: str) -> None:
        self._conn.execute(
            "UPDATE episodic_memories SET reflection_id=?, updated_at=datetime('now') WHERE episode_id=?",
            (reflection_id, episode_id),
        )
        self._conn.commit()

    # ---- consolidations ----

    @_threadsafe
    def record_consolidation(self, record: ConsolidationRecord) -> ConsolidationRecord | None:
        """Durable claim for a consolidation's canonical source-episode set.

        ONE CONSOLIDATION PER SOURCE SET (ADR-013 addendum), durable across
        threads and processes (whole claim inside BEGIN IMMEDIATE, backed by
        the unique index on consolidations.source_key):

        - re-recording the SAME consolidation_id refreshes its content in
          place (the historical refresh semantics) but NEVER mutates the
          immutable source-set identity (source_key / source_episode_ids);
        - a NEW consolidation_id for a source set that already has one LOSES
          the claim: nothing is stored and the durable first-writer row is
          returned, so concurrent learners ADOPT the canonical consolidation
          instead of duplicating it.

        The expensive consolidation COMPUTATION stays outside this short
        transaction - only the claim runs here. Returns the CANONICAL durable
        consolidation (the caller's own when it won, the first-writer's when
        it lost), or None when the row cannot be determined.
        """
        source_key = canonical_source_key(record.source_episode_ids)
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE consolidations SET category=?, merged_lesson=?, "
                "count=?, importance=?, created_at=? WHERE consolidation_id=?",
                (
                    record.category,
                    record.merged_lesson,
                    record.count,
                    float(record.importance),
                    record.created_at,
                    record.consolidation_id,
                ),
            )
            won = cur.rowcount > 0
            if not won:
                try:
                    self._conn.execute(
                        "INSERT INTO consolidations "
                        "(consolidation_id, source_key, source_episode_ids, "
                        "category, merged_lesson, count, importance, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            record.consolidation_id,
                            source_key,
                            json.dumps(record.source_episode_ids),
                            record.category,
                            record.merged_lesson,
                            record.count,
                            float(record.importance),
                            record.created_at,
                        ),
                    )
                    won = True
                except sqlite3.IntegrityError:
                    # the source set's single consolidation slot is already
                    # taken: first writer wins - the canonical row is read
                    # below and returned so the loser ADOPTS it
                    won = False
            # Canonical row: when THIS invocation owns the consolidation (a
            # same-id refresh or a fresh first insert) it is keyed by its own
            # consolidation_id (whose immutable source_key may differ from a
            # tampered re-record). When it LOST the claim, the canonical row
            # is the first-writer's, keyed by the source set.
            if won:
                row = self._conn.execute(
                    "SELECT consolidation_id, source_key, source_episode_ids, "
                    "category, merged_lesson, count, importance, created_at "
                    "FROM consolidations WHERE consolidation_id=?",
                    (record.consolidation_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT consolidation_id, source_key, source_episode_ids, "
                    "category, merged_lesson, count, importance, created_at "
                    "FROM consolidations WHERE source_key=?", (source_key,),
                ).fetchone()
            self._conn.commit()
            return _consolidation_from_row(row) if row else None
        except sqlite3.Error:
            self._conn.rollback()
            raise

    @_threadsafe
    def list_consolidations(self, limit: int = 50) -> list[ConsolidationRecord]:
        rows = self._conn.execute(
            "SELECT consolidation_id, source_key, source_episode_ids, category, merged_lesson, count, importance, created_at "
            "FROM consolidations ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_consolidation_from_row(r) for r in rows]

    @_threadsafe
    def prune(self, older_than: str | None = None, max_episodes: int | None = None,
              batch_size: int = 500, keep_importance: float = 0.0,
              dry_run: bool = False) -> int:
        """Explicit archival/pruning (ADR-014 addendum).

        Deterministic, operator-invoked, bounded-batched (ADR-028 pattern):

        - older_than: remove episodes with created_at < the ISO cutoff
          (never silent, never recent) together with their reflections;
        - max_episodes: keep the NEWEST N episodes (by created_at), remove
          the rest with their reflections;
        - keep_importance: age-pruning protects episodes with
          importance >= floor (salient failures stay);
        - batch_size in [1, 5000] (fail closed outside); the loop drains in
          bounded SELECT-batch-then-DELETE batches;
        - dry_run: return the would-be count WITHOUT deleting anything;
        - CONSOLIDATIONS are NEVER pruned here (they are the permanent
          merged summary; source provenance is preserved);
        - idempotent: a repeated identical prune removes 0;
        - authority isolation: touches ONLY episodic_memories + reflections
          (never tasks/goals/scheduler/audit/cognition tables).

        Returns the number of episodes removed (their reflections count
        toward the same removal; the return value is the episode count).
        """
        if older_than is not None:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(str(older_than).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(
                    f"older_than must be an ISO-8601 timestamp, got {older_than!r} "
                    f"(fail closed)")
        if max_episodes is not None:
            if not isinstance(max_episodes, int) or isinstance(max_episodes, bool) \
                    or max_episodes < 1:
                raise ValueError(
                    f"max_episodes must be a positive integer, got "
                    f"{max_episodes!r} (fail closed)")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) \
                or batch_size < 1 or batch_size > 5000:
            raise ValueError(
                f"batch_size must be in [1, 5000], got {batch_size!r} "
                f"(fail closed)")
        if not isinstance(keep_importance, (int, float)) \
                or isinstance(keep_importance, bool) \
                or not (0.0 <= float(keep_importance) <= 1.0):
            raise ValueError(
                f"keep_importance must be within [0, 1], got "
                f"{keep_importance!r} (fail closed)")
        if older_than is None and max_episodes is None:
            raise ValueError(
                "prune requires older_than and/or max_episodes "
                "(never silently delete)")

        # Deterministic candidate selection FIRST (read-before-delete):
        # episodes to remove = old ones (respecting the importance floor)
        # OR beyond the newest-N cap; always keep the newest-N (by
        # created_at) when max_episodes is set.
        rows = self._conn.execute(
            "SELECT episode_id, created_at, importance FROM episodic_memories"
        ).fetchall()
        rows.sort(key=lambda r: r[1])  # oldest first (ISO strings compare)
        doomed: list[str] = []
        if max_episodes is not None:
            doomed_ids = {r[0] for r in rows[:-int(max_episodes)]}
        else:
            doomed_ids = set()
        for ep_id, created_at, importance in rows:
            remove = False
            if older_than is not None and created_at < older_than:
                remove = True
                if float(keep_importance) > 0.0 and \
                        float(importance) >= float(keep_importance):
                    remove = False  # salient memories are protected
            if ep_id in doomed_ids:
                remove = True  # count cap overrides the importance floor
            if remove:
                doomed.append(ep_id)
        if not doomed:
            return 0
        if dry_run:
            return len(doomed)

        # bounded SELECT-batch-then-DELETE loop (ADR-028 pattern)
        removed = 0
        for i in range(0, len(doomed), int(batch_size)):
            batch = doomed[i:i + int(batch_size)]
            marks = ",".join("?" * len(batch))
            self._conn.execute(
                "DELETE FROM reflections WHERE episode_id IN (" + marks + ")",
                batch)
            self._conn.execute(
                "DELETE FROM episodic_memories WHERE episode_id IN ("
                + marks + ")", batch)
            removed += len(batch)
        self._conn.commit()
        return removed

    @_threadsafe
    def close(self) -> None:
        self._conn.close()


def _episode_from_row(row: tuple[Any, ...]) -> Episode:
    d = {c: v for c, v in zip(_EPISODE_COLS, row)}
    d["plan_summary"] = json.loads(d["plan_summary"])
    d["actions"] = json.loads(d["actions"])
    d["resources"] = json.loads(d["resources"])
    d["verification"] = json.loads(d["verification"])
    d["failures"] = json.loads(d["failures"])
    d["authorization"] = json.loads(d["authorization"])
    d["recovery"] = json.loads(d["recovery"])
    d["tags"] = json.loads(d["tags"])
    return Episode.from_dict(d)


def _reflection_from_row(row: tuple[Any, ...]) -> Reflection:
    d = {c: v for c, v in zip(_REFLECTION_COLS, row)}
    return Reflection.from_dict(d)


def _consolidation_from_row(row: tuple[Any, ...]) -> ConsolidationRecord:
    return ConsolidationRecord(
        consolidation_id=row[0],
        source_key=row[1],
        source_episode_ids=json.loads(row[2]),
        category=row[3],
        merged_lesson=row[4],
        count=row[5],
        importance=row[6],
        created_at=row[7],
    )
