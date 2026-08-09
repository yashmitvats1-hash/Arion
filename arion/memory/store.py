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
import sqlite3
from pathlib import Path
from typing import Any

from arion.memory.models import Episode, EpisodeFilter, Reflection

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_memories (
    episode_id    TEXT PRIMARY KEY,
    task_id       TEXT,
    goal_id       TEXT,
    goal          TEXT NOT NULL,
    plan_summary  TEXT NOT NULL,
    actions       TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    verification  TEXT NOT NULL,
    failures      TEXT NOT NULL,
    authorization TEXT NOT NULL,
    recovery      TEXT NOT NULL,
    tags          TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,
    reflection_id TEXT,
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
"""

_EPISODE_COLS = [
    "episode_id", "task_id", "goal_id", "goal", "plan_summary", "actions",
    "outcome", "verification", "failures", "authorization", "recovery",
    "tags", "importance", "reflection_id", "created_at", "updated_at",
]
_REFLECTION_COLS = [
    "reflection_id", "episode_id", "what_happened", "what_worked", "what_failed",
    "why", "lesson", "recommendation", "confidence", "importance", "created_at",
]


class SQLiteMemoryStore:
    """Durable SQLite episodic memory + reflections."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- episodes ----

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
                episode.outcome,
                json.dumps(episode.verification),
                json.dumps(episode.failures),
                json.dumps(episode.authorization),
                json.dumps(episode.recovery),
                json.dumps(episode.tags),
                float(episode.importance),
                episode.reflection_id,
                episode.created_at,
                episode.updated_at,
            ),
        )
        self._conn.commit()

    def get_episode(self, episode_id: str) -> Episode | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_EPISODE_COLS) + " FROM episodic_memories WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        return _episode_from_row(row) if row else None

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

    def list_recent(self, limit: int = 10) -> list[Episode]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_EPISODE_COLS)} FROM episodic_memories ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_episode_from_row(r) for r in rows]

    # ---- reflections ----

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

    def get_reflection(self, reflection_id: str) -> Reflection | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_REFLECTION_COLS) + " FROM reflections WHERE reflection_id=?",
            (reflection_id,),
        ).fetchone()
        return _reflection_from_row(row) if row else None

    def list_recent_reflections(self, limit: int = 10) -> list[Reflection]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_REFLECTION_COLS)} FROM reflections ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_reflection_from_row(r) for r in rows]

    def link_reflection(self, episode_id: str, reflection_id: str) -> None:
        self._conn.execute(
            "UPDATE episodic_memories SET reflection_id=?, updated_at=datetime('now') WHERE episode_id=?",
            (reflection_id, episode_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _episode_from_row(row: tuple[Any, ...]) -> Episode:
    d = {c: v for c, v in zip(_EPISODE_COLS, row)}
    d["plan_summary"] = json.loads(d["plan_summary"])
    d["actions"] = json.loads(d["actions"])
    d["verification"] = json.loads(d["verification"])
    d["failures"] = json.loads(d["failures"])
    d["authorization"] = json.loads(d["authorization"])
    d["recovery"] = json.loads(d["recovery"])
    d["tags"] = json.loads(d["tags"])
    return Episode.from_dict(d)


def _reflection_from_row(row: tuple[Any, ...]) -> Reflection:
    d = {c: v for c, v in zip(_REFLECTION_COLS, row)}
    return Reflection.from_dict(d)
