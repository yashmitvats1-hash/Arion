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
from arion.state.models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS beliefs (
    belief_id   TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    statement   TEXT NOT NULL,
    confidence  REAL NOT NULL,
    importance  REAL NOT NULL,
    provenance  TEXT NOT NULL,
    source      TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
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
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_preferences_key_user ON preferences(key, user);
CREATE TABLE IF NOT EXISTS environment_facts (
    fact_id    TEXT PRIMARY KEY,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    observed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON environment_facts(key);
CREATE TABLE IF NOT EXISTS goal_plans (
    goal_id      TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    strategy     TEXT NOT NULL,
    plan_summary TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (goal_id, plan_version)
);
CREATE INDEX IF NOT EXISTS idx_goal_plans_goal ON goal_plans(goal_id);
"""

_BELIEF_COLS = ["belief_id", "category", "statement", "confidence", "importance",
                "provenance", "source", "version", "superseded_at", "created_at", "updated_at"]
_PREF_COLS = ["preference_id", "key", "value", "user", "source", "provenance", "created_at", "updated_at"]
_FACT_COLS = ["fact_id", "key", "value", "source", "version", "observed_at", "created_at", "updated_at"]
_GOAL_PLAN_COLS = ["goal_id", "plan_version", "strategy", "plan_summary", "reason", "created_at"]


class SQLiteCognitiveStore:
    """Durable SQLite cognitive state (beliefs, preferences, environment facts)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Lightweight additive migration for pre-versioning schemas."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(beliefs)").fetchall()}
        if "version" not in cols:
            self._conn.execute("ALTER TABLE beliefs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "superseded_at" not in cols:
            self._conn.execute("ALTER TABLE beliefs ADD COLUMN superseded_at TEXT")
        pcols = {r[1] for r in self._conn.execute("PRAGMA table_info(preferences)").fetchall()}
        if "updated_at" not in pcols:
            self._conn.execute("ALTER TABLE preferences ADD COLUMN updated_at TEXT")
        fcols = {r[1] for r in self._conn.execute("PRAGMA table_info(environment_facts)").fetchall()}
        if "version" not in fcols:
            self._conn.execute("ALTER TABLE environment_facts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "observed_at" not in fcols:
            self._conn.execute("ALTER TABLE environment_facts ADD COLUMN observed_at TEXT")
        gcols = {r[1] for r in self._conn.execute("PRAGMA table_info(goal_plans)").fetchall()}
        if "reason" not in gcols:
            self._conn.execute("ALTER TABLE goal_plans ADD COLUMN reason TEXT NOT NULL DEFAULT ''")

    # ---- beliefs (append-only + versioned) ----

    def record_belief(self, belief: Belief) -> None:
        # Append-only: INSERT OR REPLACE is keyed by belief_id, and derivation
        # always creates NEW ids for revisions; superseded rows are retained.
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
                belief.version,
                belief.superseded_at,
                belief.created_at,
                belief.updated_at,
            ),
        )
        self._conn.commit()

    def supersede_belief(self, belief_id: str, superseded_at: str | None = None) -> None:
        """Mark a belief as superseded (history preserved, excluded from
        active listing)."""
        from arion.state.models import utcnow

        self._conn.execute(
            "UPDATE beliefs SET superseded_at=?, updated_at=? WHERE belief_id=?",
            (superseded_at or utcnow(), superseded_at or utcnow(), belief_id),
        )
        self._conn.commit()

    def get_belief(self, belief_id: str) -> Belief | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_BELIEF_COLS) + " FROM beliefs WHERE belief_id=?",
            (belief_id,),
        ).fetchone()
        return _belief_from_row(row) if row else None

    def list_beliefs(self, category: str | None = None, limit: int = 100, include_superseded: bool = False) -> list[Belief]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if not include_superseded:
            clauses.append("superseded_at IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT {', '.join(_BELIEF_COLS)} FROM beliefs{where} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, limit)),
        ).fetchall()
        return [_belief_from_row(r) for r in rows]

    def count_beliefs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM beliefs WHERE superseded_at IS NULL").fetchone()[0]

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
                preference.updated_at or preference.created_at,
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

    # ---- environment facts (versioned per key) ----

    def record_environment_fact(self, fact: EnvironmentFact) -> None:
        existing = self.get_environment_fact(fact.key)
        if existing is not None:
            changed = existing.value != fact.value
            version = existing.version + 1 if changed else existing.version
            fact_id = existing.fact_id
            created_at = existing.created_at
            observed_at = fact.observed_at or fact.updated_at
            if not changed:
                # unchanged observation: keep version, just refresh observed_at
                fact_id = existing.fact_id
                version = existing.version
                created_at = existing.created_at
                observed_at = existing.observed_at or fact.updated_at
            self._conn.execute(
                "INSERT OR REPLACE INTO environment_facts "
                f"({', '.join(_FACT_COLS)}) VALUES ({', '.join('?' * len(_FACT_COLS))})",
                (
                    fact_id,
                    fact.key,
                    json.dumps(fact.value, default=str),
                    fact.source,
                    version,
                    observed_at,
                    created_at,
                    fact.updated_at,
                ),
            )
            self._conn.commit()
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO environment_facts "
            f"({', '.join(_FACT_COLS)}) VALUES ({', '.join('?' * len(_FACT_COLS))})",
            (
                fact.fact_id,
                fact.key,
                json.dumps(fact.value, default=str),
                fact.source,
                fact.version,
                fact.observed_at or fact.updated_at,
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

    # ---- long-horizon goal plans ----

    def record_goal_plan(self, goal_id: str, plan_version: int, strategy: str,
                         plan_summary: list[dict], reason: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goal_plans "
            f"({', '.join(_GOAL_PLAN_COLS)}) VALUES ({', '.join('?' * len(_GOAL_PLAN_COLS))})",
            (goal_id, plan_version, strategy, json.dumps(plan_summary), reason, utcnow()),
        )
        self._conn.commit()

    def list_goal_plans(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_PLAN_COLS) + " FROM goal_plans WHERE goal_id=? ORDER BY plan_version",
            (goal_id,),
        ).fetchall()
        return [_goal_plan_from_row(r) for r in rows]

    def latest_goal_plan(self, goal_id: str) -> dict[str, Any] | None:
        rows = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_PLAN_COLS) + " FROM goal_plans WHERE goal_id=? "
            "ORDER BY plan_version DESC LIMIT 1",
            (goal_id,),
        ).fetchall()
        return _goal_plan_from_row(rows[0]) if rows else None

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


def _goal_plan_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    d = {c: v for c, v in zip(_GOAL_PLAN_COLS, row)}
    try:
        d["plan_summary"] = json.loads(d["plan_summary"])
    except (TypeError, json.JSONDecodeError):
        d["plan_summary"] = []
    return d


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
