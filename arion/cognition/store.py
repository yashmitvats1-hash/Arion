"""Cognitive State storage (ADR-014).

SQLite-backed stores for beliefs, preferences, and environment facts - the
semantic/procedural/preference/environment layers of the cognitive state,
distinct from episodic memory (which lives in arion/memory).

Same DB file as core state, so the cognitive state survives restarts.
INFORMATIONAL ONLY: no authorization influence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from arion.cognition.models import Belief, EnvironmentFact, Preference

SCHEMA = """
CREATE TABLE IF NOT EXISTS beliefs (
    belief_id   TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    statement   TEXT NOT NULL,
    confidence  REAL NOT NULL,
    importance  REAL NOT NULL,
    provenance  TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beliefs_category ON beliefs(category);
CREATE INDEX IF NOT EXISTS idx_beliefs_created ON beliefs(created_at);
CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT PRIMARY KEY,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    user          TEXT NOT NULL,
    source        TEXT NOT NULL,
    provenance    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_preferences_key_user ON preferences(key, user);
CREATE TABLE IF NOT EXISTS environment_facts (
    fact_id    TEXT PRIMARY KEY,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON environment_facts(key);
"""

_BELIEF_COLS = ["belief_id", "category", "statement", "confidence", "importance",
                "provenance", "source", "created_at", "updated_at"]
_PREF_COLS = ["preference_id", "key", "value", "user", "source", "provenance", "created_at"]
_FACT_COLS = ["fact_id", "key", "value", "source", "created_at", "updated_at"]


class SQLiteCognitiveStore:
    """Durable SQLite cognitive state (beliefs, preferences, environment facts)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- beliefs ----

    def record_belief(self, belief: Belief) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO beliefs "
            f"({', '.join(_BELIEF_COLS)}) VALUES ({', '.join('?' * len(_BELIEF_COLS))})",
            (
                belief.belief_id,
                belief.category,
                belief.statement,
                float(belief.confidence),
                float(belief.importance),
                json.dumps(belief.provenance),
                belief.source,
                belief.created_at,
                belief.updated_at,
            ),
        )
        self._conn.commit()

    def get_belief(self, belief_id: str) -> Belief | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_BELIEF_COLS) + " FROM beliefs WHERE belief_id=?",
            (belief_id,),
        ).fetchone()
        return _belief_from_row(row) if row else None

    def list_beliefs(self, category: str | None = None, limit: int = 100) -> list[Belief]:
        if category:
            rows = self._conn.execute(
                f"SELECT {', '.join(_BELIEF_COLS)} FROM beliefs WHERE category=? ORDER BY created_at DESC LIMIT ?",
                (category, max(1, limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {', '.join(_BELIEF_COLS)} FROM beliefs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [_belief_from_row(r) for r in rows]

    def count_beliefs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]

    # ---- preferences ----

    def record_preference(self, preference: Preference) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO preferences "
            f"({', '.join(_PREF_COLS)}) VALUES ({', '.join('?' * len(_PREF_COLS))})",
            (
                preference.preference_id,
                preference.key,
                preference.value,
                preference.user,
                preference.source,
                json.dumps(preference.provenance),
                preference.created_at,
            ),
        )
        self._conn.commit()

    def get_preference(self, key: str, user: str = "default") -> Preference | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_PREF_COLS) + " FROM preferences WHERE key=? AND user=?",
            (key, user),
        ).fetchone()
        return _pref_from_row(row) if row else None

    def list_preferences(self, limit: int = 100) -> list[Preference]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_PREF_COLS)} FROM preferences ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_pref_from_row(r) for r in rows]

    # ---- environment facts ----

    def record_environment_fact(self, fact: EnvironmentFact) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO environment_facts "
            f"({', '.join(_FACT_COLS)}) VALUES ({', '.join('?' * len(_FACT_COLS))})",
            (
                fact.fact_id,
                fact.key,
                json.dumps(fact.value, default=str),
                fact.source,
                fact.created_at,
                fact.updated_at,
            ),
        )
        self._conn.commit()

    def get_environment_fact(self, key: str) -> EnvironmentFact | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_FACT_COLS) + " FROM environment_facts WHERE key=?",
            (key,),
        ).fetchone()
        return _fact_from_row(row) if row else None

    def list_environment_facts(self, limit: int = 100) -> list[EnvironmentFact]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_FACT_COLS)} FROM environment_facts ORDER BY updated_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_fact_from_row(r) for r in rows]

    # ---- aggregate ----

    def snapshot(self, limit_beliefs: int = 50) -> dict[str, Any]:
        from arion.cognition.models import CognitiveSnapshot

        snap = CognitiveSnapshot(
            beliefs=self.list_beliefs(limit=limit_beliefs),
            preferences=self.list_preferences(limit=limit_beliefs),
            environment=self.list_environment_facts(limit=limit_beliefs),
            counts={
                "beliefs": self.count_beliefs(),
                "preferences": len(self.list_preferences(limit=1000)),
                "environment": len(self.list_environment_facts(limit=1000)),
            },
        )
        return snap.to_dict(limit_beliefs=limit_beliefs)

    def close(self) -> None:
        self._conn.close()


def _belief_from_row(row: tuple[Any, ...]) -> Belief:
    d = {c: v for c, v in zip(_BELIEF_COLS, row)}
    d["provenance"] = json.loads(d["provenance"])
    return Belief.from_dict(d)


def _pref_from_row(row: tuple[Any, ...]) -> Preference:
    d = {c: v for c, v in zip(_PREF_COLS, row)}
    d["provenance"] = json.loads(d["provenance"])
    return Preference.from_dict(d)


def _fact_from_row(row: tuple[Any, ...]) -> EnvironmentFact:
    d = {c: v for c, v in zip(_FACT_COLS, row)}
    d["value"] = json.loads(d["value"])
    return EnvironmentFact.from_dict(d)
