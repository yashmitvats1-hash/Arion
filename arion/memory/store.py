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
    source_episode_ids TEXT NOT NULL,
    category TEXT NOT NULL,
    merged_lesson TEXT NOT NULL,
    count INTEGER NOT NULL,
    importance REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ConsolidationRecord:
    """An explicit, explainable consolidation of similar episodes."""

    def __init__(self, consolidation_id, source_episode_ids, category, merged_lesson, count, importance, created_at):
        self.consolidation_id = consolidation_id
        self.source_episode_ids = source_episode_ids
        self.category = category
        self.merged_lesson = merged_lesson
        self.count = count
        self.importance = importance
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "consolidation_id": self.consolidation_id,
            "source_episode_ids": self.source_episode_ids,
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
            self._conn.execute(
                "DELETE FROM episodic_memories WHERE episode_id NOT IN ("
                "SELECT episode_id FROM episodic_memories m2 WHERE "
                "m2.task_id = episodic_memories.task_id ORDER BY updated_at "
                "DESC, rowid DESC LIMIT 1)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_task_unique "
                "ON episodic_memories(task_id)")
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    # ---- episodes ----

    @_threadsafe
    def record_episode(self, episode: Episode) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO episodic_memories "
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
        self._conn.commit()

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
    def record_reflection(self, reflection: Reflection) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO reflections "
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
        self._conn.commit()

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
    def link_reflection(self, episode_id: str, reflection_id: str) -> None:
        self._conn.execute(
            "UPDATE episodic_memories SET reflection_id=?, updated_at=datetime('now') WHERE episode_id=?",
            (reflection_id, episode_id),
        )
        self._conn.commit()

    # ---- consolidations ----

    @_threadsafe
    def record_consolidation(self, record: ConsolidationRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO consolidations "
            "(consolidation_id, source_episode_ids, category, merged_lesson, count, importance, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                record.consolidation_id,
                json.dumps(record.source_episode_ids),
                record.category,
                record.merged_lesson,
                record.count,
                float(record.importance),
                record.created_at,
            ),
        )
        self._conn.commit()

    @_threadsafe
    def list_consolidations(self, limit: int = 50) -> list[ConsolidationRecord]:
        rows = self._conn.execute(
            "SELECT consolidation_id, source_episode_ids, category, merged_lesson, count, importance, created_at "
            "FROM consolidations ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        out = []
        for r in rows:
            out.append(ConsolidationRecord(
                consolidation_id=r[0], source_episode_ids=json.loads(r[1]), category=r[2],
                merged_lesson=r[3], count=r[4], importance=r[5], created_at=r[6],
            ))
        return out

    @_threadsafe
    def prune(self, older_than: str | None = None, max_episodes: int | None = None) -> int:
        """Archival/pruning seam (ADR-014) - intentionally NOT implemented.

        Consolidation preserves history and therefore does not bound physical
        storage; this seam is where a future archival policy (age-based,
        count-capped, or importance-weighted pruning/archival) will live.
        Memory is never deleted in this milestone.
        """
        raise NotImplementedError(
            "archival/pruning seam (ADR-014): not yet implemented - memory is never deleted; "
            "design the archival policy before enabling this"
        )

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
