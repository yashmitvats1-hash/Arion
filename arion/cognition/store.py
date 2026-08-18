"""Cognitive State storage (ADR-014).

SQLite-backed stores for beliefs, preferences, and environment facts - the
semantic/procedural/preference/environment layers of the cognitive state,
distinct from episodic memory (which lives in arion/memory).

Same DB file as core state, so the cognitive state survives restarts.
INFORMATIONAL ONLY: no authorization influence.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from dataclasses import dataclass

from arion.cognition.models import Belief, EnvironmentFact, Preference
from arion.cognition.strategy import STRATEGY_NAMES, STRATEGY_OUTCOME_STATES
from arion.state.models import new_id, utcnow


@dataclass
class BeliefPersistResult:
    """Outcome of an authoritative belief-persistence claim.

    ``belief`` is the CANONICAL active revision for the logical identity
    ``(category, statement)`` after the claim commits. ``created`` is True
    only when this invocation inserted a NEW revision (so callers emit
    creation/revision observability exactly once per durable change);
    concurrent losers and equal/lower-confidence observations adopt the
    canonical row and report ``created=False``. ``superseded_ids`` lists
    the beliefs that this commit transitioned from active to superseded
    (empty when the observation was adopted without a revision).
    """

    belief: Belief
    created: bool = False
    superseded_ids: tuple[str, ...] = ()

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
-- NOTE: the partial UNIQUE index idx_beliefs_active_identity (the cross-process
-- backstop for one active revision per (category, statement)) is created in
-- __init__ AFTER the legacy-duplicate repair, so pre-fix databases with
-- multiple active revisions for the same logical belief do not fail to open.
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
CREATE TABLE IF NOT EXISTS strategy_outcomes (
    outcome_id       TEXT PRIMARY KEY,
    goal_id          TEXT NOT NULL,
    goal_description TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    plan_version     INTEGER NOT NULL,
    outcome          TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    episode_id       TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(goal_id, plan_version)
);
CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_goal
    ON strategy_outcomes(goal_id);
"""

_BELIEF_COLS = ["belief_id", "category", "statement", "confidence", "importance",
                "provenance", "source", "version", "superseded_at", "created_at", "updated_at"]
_PREF_COLS = ["preference_id", "key", "value", "user", "source", "provenance", "created_at", "updated_at"]
_FACT_COLS = ["fact_id", "key", "value", "source", "version", "observed_at", "created_at", "updated_at"]
_GOAL_PLAN_COLS = ["goal_id", "plan_version", "strategy", "plan_summary", "reason", "created_at"]
_OUTCOME_COLS = ["outcome_id", "goal_id", "goal_description", "strategy",
                 "plan_version", "outcome", "reason", "episode_id", "created_at"]


def _threadsafe(method):
    """Guard public methods with the connection's RLock (ADR-026:
    cross-process engines may drive a store from different threads)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._sql_lock:
            return method(self, *args, **kwargs)

    return wrapper


class SQLiteCognitiveStore:
    """Durable SQLite cognitive state (beliefs, preferences, environment facts)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._sql_lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        # ADR-014 addendum: repair any pre-invariant legacy state (multiple
        # ACTIVE rows for the same logical belief identity) BEFORE creating
        # the partial-unique structural backstop. This is a bug-artifact
        # repair, never archival pruning - historical superseded rows are
        # preserved byte-for-byte.
        self._repair_belief_invariant()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_beliefs_active_identity "
            "ON beliefs(category, statement) WHERE superseded_at IS NULL")
        self._conn.commit()

    @_threadsafe
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

    @_threadsafe
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

    @_threadsafe
    def supersede_belief(self, belief_id: str, superseded_at: str | None = None) -> None:
        """Mark a belief as superseded (history preserved, excluded from
        active listing)."""
        from arion.state.models import utcnow

        self._conn.execute(
            "UPDATE beliefs SET superseded_at=?, updated_at=? WHERE belief_id=?",
            (superseded_at or utcnow(), superseded_at or utcnow(), belief_id),
        )
        self._conn.commit()

    @_threadsafe
    def persist_belief(self, belief: Belief, _attempt: int = 0) -> BeliefPersistResult:
        """Authoritative belief-persistence claim.

        ONE funnel for every production belief write (engine, facade,
        consolidation-fed beliefs). The whole identity/confidence/version/
        supersession decision runs inside ONE ``BEGIN IMMEDIATE``
        transaction, and a partial UNIQUE INDEX on
        ``(category, statement) WHERE superseded_at IS NULL`` is the
        cross-process backstop - so two threads or two engine processes can
        never commit two ACTIVE revisions for the same logical belief.

        Decision (the existing project rule, now made durable):

        - no active revision exists for ``(category, statement)`` -> the
          incoming belief becomes a NEW active revision at
          ``max(existing versions) + 1`` (or 1 on a fresh lineage);
        - the active revision has confidence STRICTLY LOWER than the
          incoming belief -> the incoming belief becomes a NEW active
          revision at ``max(version) + 1``, and every previously-active
          revision in the lineage is superseded atomically in the same
          transaction (history preserved);
        - the active revision has equal or HIGHER confidence -> the
          incoming observation is ADOPTED: no new row, no version bump,
          no event. The canonical active revision is returned.

        The incoming ``belief.belief_id`` is used only as the row id when
        this call actually inserts; on adoption the canonical row's id is
        returned regardless of what the caller minted. ``version`` and
        ``superseded_at`` on the incoming belief are overwritten by the
        authoritative decision.

        Contention: a concurrent committer may supersede the lineage
        between this call's read and write inside one ``BEGIN IMMEDIATE``
        (the partial unique index surfaces that as ``IntegrityError``).
        The decision is retried deterministically a bounded number of
        times so the LOSER of an equal/higher race adopts the canonical
        row and a higher-confidence observation still wins as a fresh
        revision.

        Returns a :class:`BeliefPersistResult` carrying the canonical
        active belief, ``created`` (True iff this call inserted a new row)
        and the tuple of belief ids it superseded. Expensive belief
        DERIVATION stays in the caller; only the claim runs here.
        """
        # Inputs are already validated by the Belief dataclass; keep the
        # transaction short - no derivation, no I/O beyond SQLite.
        max_attempts = 8
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT " + ", ".join(_BELIEF_COLS)
                + " FROM beliefs WHERE category=? AND statement=?",
                (belief.category, belief.statement),
            ).fetchall()
            lineage = [_belief_from_row(r) for r in rows]
            active = [b for b in lineage if b.superseded_at is None]
            max_version = max((b.version for b in lineage), default=0)

            now = utcnow()
            if active:
                canonical = max(active, key=lambda b: (b.confidence, b.created_at, b.belief_id))
                if canonical.confidence >= float(belief.confidence):
                    # Adopt: equal/higher active revision already canonical.
                    self._conn.commit()
                    return BeliefPersistResult(belief=canonical, created=False,
                                              superseded_ids=())
                # Strictly higher confidence: supersede ALL currently-active
                # rows in the lineage FIRST (so the partial UNIQUE INDEX on
                # active (category, statement) is satisfied when the new
                # revision is inserted), then insert the new active revision
                # at the next monotonic version. Normally exactly one prior
                # is active; the loop also heals any pre-backstop duplicate.
                new_version = max_version + 1
                belief.version = new_version
                belief.superseded_at = None
                belief.created_at = belief.created_at or now
                belief.updated_at = now
                superseded_ids: list[str] = []
                for prior in active:
                    self._conn.execute(
                        "UPDATE beliefs SET superseded_at=?, updated_at=? "
                        "WHERE belief_id=?",
                        (now, now, prior.belief_id),
                    )
                    superseded_ids.append(prior.belief_id)
                try:
                    self._conn.execute(
                        "INSERT INTO beliefs "
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
                except sqlite3.IntegrityError:
                    # A concurrent committer superseded the lineage and
                    # inserted a new active revision between our read and
                    # our write. Roll back this attempt and re-run the
                    # deterministic decision against the fresh state.
                    self._conn.rollback()
                    if _attempt + 1 >= max_attempts:
                        raise
                    return self.persist_belief(belief, _attempt=_attempt + 1)
                self._conn.commit()
                # Re-read the canonical active revision so a concurrent
                # higher-confidence writer that raced this commit is
                # reflected in the returned object (this caller may have
                # just become superseded history; result stays truthful).
                canonical_row = self._conn.execute(
                    "SELECT " + ", ".join(_BELIEF_COLS)
                    + " FROM beliefs WHERE category=? AND statement=?"
                    " AND superseded_at IS NULL",
                    (belief.category, belief.statement),
                ).fetchone()
                if canonical_row is not None:
                    canonical = _belief_from_row(canonical_row)
                else:
                    canonical = belief
                return BeliefPersistResult(
                    belief=canonical, created=True,
                    superseded_ids=tuple(superseded_ids))

            # No active revision. A superseded-only lineage (e.g. pruned
            # history where the active row was deleted) keeps appending at
            # the next version; a fresh lineage starts at version 1.
            new_version = max_version + 1
            belief.version = new_version
            belief.superseded_at = None
            belief.created_at = belief.created_at or now
            belief.updated_at = now
            try:
                self._conn.execute(
                    "INSERT INTO beliefs "
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
            except sqlite3.IntegrityError:
                # Same race: another process inserted the active row first.
                self._conn.rollback()
                if _attempt + 1 >= max_attempts:
                    raise
                return self.persist_belief(belief, _attempt=_attempt + 1)
            self._conn.commit()
            return BeliefPersistResult(belief=belief, created=True,
                                      superseded_ids=())
        except sqlite3.Error:
            self._conn.rollback()
            raise

    def _repair_belief_invariant(self) -> None:
        """Idempotent init-time repair of pre-invariant belief history.

        Older engines (the pre-fix ``_derive_beliefs`` path and the
        non-atomic facade check-then-act) could leave MULTIPLE active rows
        for the same logical identity ``(category, statement)`` and could
        reuse version numbers. This runs BEFORE the partial-unique
        backstop is created and makes every lineage valid without deleting
        historical evidence:

        - among the active rows of a lineage, the one with the highest
          confidence (ties: newest ``created_at``, then highest ``rowid``)
          is kept as canonical; every other active row is marked
          superseded at a deterministic timestamp (the canonical's
          ``updated_at`` so the ordering is stable across reopens);
        - if a lineage has NO active row (already valid), it is untouched;
        - versions across the lineage are renumbered monotonically in
          ``(coalesce(superseded_at, ''), created_at, rowid)`` order so
          the canonical active row receives the highest version and
          superseded rows retain their historical ordering (this only
          changes rows whose version was a pre-fix duplicate).

        Deterministic and idempotent: a second open changes nothing
        because the topology is already valid. Never deletes a row.
        """
        # Group every belief by logical identity.
        rows = self._conn.execute(
            "SELECT belief_id, category, statement, confidence, created_at, "
            "rowid, superseded_at FROM beliefs ORDER BY rowid"
        ).fetchall()
        lineages: dict[tuple[str, str], list[dict]] = {}
        for belief_id, category, statement, confidence, created_at, rowid, superseded_at in rows:
            lineages.setdefault((category, statement), []).append({
                "belief_id": belief_id,
                "confidence": float(confidence),
                "created_at": created_at,
                "rowid": rowid,
                "superseded_at": superseded_at,
            })
        stamp = utcnow()
        for lineage in lineages.values():
            active = [r for r in lineage if r["superseded_at"] is None]
            if len(active) <= 1:
                continue  # already valid topology
            canonical = max(
                active,
                key=lambda r: (r["confidence"], r["created_at"], r["rowid"]),
            )
            # Deterministic supersession timestamp for the losers.
            loser_stamp = stamp
            for r in active:
                if r["belief_id"] == canonical["belief_id"]:
                    continue
                self._conn.execute(
                    "UPDATE beliefs SET superseded_at=?, updated_at=? "
                    "WHERE belief_id=?",
                    (loser_stamp, loser_stamp, r["belief_id"]),
                )
        # Renumber versions per lineage in stable order so the canonical
        # active row holds the highest version. Superseded rows are ordered
        # by (superseded_at, created_at, rowid); the active row sorts LAST
        # (None never compares against strings portably, so use an explicit
        # 0/1 rank). Only touches rows whose current version differs from
        # the canonical order (idempotent).
        for (category, statement), lineage in lineages.items():
            active_now = [r for r in lineage if r["superseded_at"] is None]
            canonical_id = None
            if active_now:
                canonical_id = max(
                    active_now,
                    key=lambda r: (r["confidence"], r["created_at"], r["rowid"]),
                )["belief_id"]

            def _rank(r):
                # superseded rows (rank 0) first in supersession order; the
                # canonical active row (rank 1) always LAST, so it holds the
                # highest version regardless of its rowid/created_at.
                if r["belief_id"] == canonical_id:
                    return (2, "", "", 0)
                if r["superseded_at"] is None:
                    # A non-canonical active row: should have been superseded
                    # by the first loop, but order it just before the
                    # canonical so versioning stays stable if it survives.
                    return (1, "", r["created_at"], r["rowid"])
                return (0, r["superseded_at"], r["created_at"], r["rowid"])

            ordered = sorted(lineage, key=_rank)
            for new_version, r in enumerate(ordered, start=1):
                self._conn.execute(
                    "UPDATE beliefs SET version=? WHERE belief_id=? AND version<>?",
                    (new_version, r["belief_id"], new_version),
                )

    @_threadsafe
    def get_belief(self, belief_id: str) -> Belief | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_BELIEF_COLS) + " FROM beliefs WHERE belief_id=?",
            (belief_id,),
        ).fetchone()
        return _belief_from_row(row) if row else None

    @_threadsafe
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

    @_threadsafe
    def count_beliefs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM beliefs WHERE superseded_at IS NULL").fetchone()[0]

    # ---- preferences ----

    @_threadsafe
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

    @_threadsafe
    def get_preference(self, key: str, user: str = "default") -> Preference | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_PREF_COLS) + " FROM preferences WHERE key=? AND user=?",
            (key, user),
        ).fetchone()
        return _pref_from_row(row) if row else None

    @_threadsafe
    def list_preferences(self, limit: int = 100) -> list[Preference]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_PREF_COLS)} FROM preferences ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_pref_from_row(r) for r in rows]

    # ---- environment facts (versioned per key) ----

    @_threadsafe
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

    @_threadsafe
    def get_environment_fact(self, key: str) -> EnvironmentFact | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_FACT_COLS) + " FROM environment_facts WHERE key=?",
            (key,),
        ).fetchone()
        return _fact_from_row(row) if row else None

    @_threadsafe
    def list_environment_facts(self, limit: int = 100) -> list[EnvironmentFact]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_FACT_COLS)} FROM environment_facts ORDER BY updated_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [_fact_from_row(r) for r in rows]

    # ---- long-horizon goal plans ----

    @_threadsafe
    def record_goal_plan(self, goal_id: str, plan_version: int, strategy: str,
                         plan_summary: list[dict], reason: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goal_plans "
            f"({', '.join(_GOAL_PLAN_COLS)}) VALUES ({', '.join('?' * len(_GOAL_PLAN_COLS))})",
            (goal_id, plan_version, strategy, json.dumps(plan_summary), reason, utcnow()),
        )
        self._conn.commit()

    @_threadsafe
    def list_goal_plans(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_PLAN_COLS) + " FROM goal_plans WHERE goal_id=? ORDER BY plan_version",
            (goal_id,),
        ).fetchall()
        return [_goal_plan_from_row(r) for r in rows]

    @_threadsafe
    def latest_goal_plan(self, goal_id: str) -> dict[str, Any] | None:
        rows = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_PLAN_COLS) + " FROM goal_plans WHERE goal_id=? "
            "ORDER BY plan_version DESC LIMIT 1",
            (goal_id,),
        ).fetchall()
        return _goal_plan_from_row(rows[0]) if rows else None

    # ---- strategy outcomes (ADR-015 addendum, Phase A) ----

    @_threadsafe
    def record_strategy_outcome(self, goal_id: str, goal_description: str,
                                strategy: str, plan_version: int,
                                outcome: str, reason: str = "",
                                episode_id: str | None = None) -> bool:
        """Record one durable strategy outcome (INFORMATIONAL only).

        Exactly one row per (goal_id, plan_version) - UNIQUE invariant.
        Idempotent: re-recording the same (goal_id, plan_version, outcome,
        reason, episode_id) is a NO-OP and PRESERVES the original
        created_at. Returns True when a row was inserted or its values
        changed (a durable change - callers may emit observability), False
        for an idempotent replay. Fail closed on unknown strategy names,
        unknown outcome states, non-positive plan versions, empty ids, and
        non-string fields. goal_description is bounded to 300 chars, reason
        to 200 chars.
        """
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise ValueError(
                f"goal_id must be a non-empty string, got {goal_id!r} (fail closed)")
        if not isinstance(goal_description, str):
            raise ValueError(
                f"goal_description must be a string, got {goal_description!r} "
                f"(fail closed)")
        if strategy not in STRATEGY_NAMES:
            raise ValueError(
                f"strategy must be one of {STRATEGY_NAMES}, got {strategy!r} "
                f"(fail closed)")
        if (isinstance(plan_version, bool) or not isinstance(plan_version, int)
                or plan_version < 1):
            raise ValueError(
                f"plan_version must be a positive integer, got {plan_version!r} "
                f"(fail closed)")
        if outcome not in STRATEGY_OUTCOME_STATES:
            raise ValueError(
                f"outcome must be one of {STRATEGY_OUTCOME_STATES}, got "
                f"{outcome!r} (fail closed)")
        if not isinstance(reason, str):
            raise ValueError(
                f"reason must be a string, got {reason!r} (fail closed)")
        if episode_id is not None and (not isinstance(episode_id, str)
                                       or not episode_id):
            raise ValueError(
                f"episode_id must be a non-empty string or None, got "
                f"{episode_id!r} (fail closed)")
        existing = self.get_strategy_outcome(goal_id, plan_version)
        if existing is not None:
            if (existing["strategy"] == strategy
                    and existing["outcome"] == outcome
                    and existing["reason"] == reason[:200]
                    and existing["episode_id"] == episode_id):
                return False  # idempotent replay: no durable change
            # durable value change: update IN PLACE, preserving the original
            # outcome_id and created_at (history of the row's identity)
            self._conn.execute(
                "UPDATE strategy_outcomes SET goal_description=?, strategy=?, "
                "outcome=?, reason=?, episode_id=? "
                "WHERE goal_id=? AND plan_version=?",
                (goal_description[:300], strategy, outcome, reason[:200],
                 episode_id, goal_id, int(plan_version)),
            )
            self._conn.commit()
            return True
        # Missing row: CREATE. Cross-process safety (ADR-015 Phase D): if two
        # writers both read "missing", the FIRST writer wins - the second
        # INSERT OR IGNORE is a no-op (rowcount 0 -> False), so the winner's
        # outcome_id + created_at survive and no duplicate event is emitted.
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO strategy_outcomes "
            f"({', '.join(_OUTCOME_COLS)}) VALUES ({', '.join('?' * len(_OUTCOME_COLS))})",
            (
                new_id("sout"),
                goal_id,
                goal_description[:300],
                strategy,
                int(plan_version),
                outcome,
                reason[:200],
                episode_id,
                utcnow(),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    @_threadsafe
    def get_strategy_outcome(self, goal_id: str,
                             plan_version: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_OUTCOME_COLS) + " FROM strategy_outcomes "
            "WHERE goal_id=? AND plan_version=?",
            (goal_id, int(plan_version)),
        ).fetchone()
        return _strategy_outcome_from_row(row) if row else None

    @_threadsafe
    def list_strategy_outcomes(self, goal_id: str | None = None,
                               limit: int = 200) -> list[dict[str, Any]]:
        if goal_id is not None:
            rows = self._conn.execute(
                "SELECT " + ", ".join(_OUTCOME_COLS) + " FROM strategy_outcomes "
                "WHERE goal_id=? ORDER BY goal_id, plan_version LIMIT ?",
                (goal_id, max(1, limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT " + ", ".join(_OUTCOME_COLS) + " FROM strategy_outcomes "
                "ORDER BY goal_id, plan_version LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [_strategy_outcome_from_row(r) for r in rows]

    @_threadsafe
    def count_strategy_outcomes(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM strategy_outcomes"
        ).fetchone()[0]

    # ---- cognitive archival/pruning (ADR-014 addendum) ----

    @_threadsafe
    def prune_superseded_beliefs(self, older_than: str | None = None,
                                 keep_versions: int = 1,
                                 batch_size: int = 500,
                                 dry_run: bool = False) -> int:
        """Prune superseded belief history (ADR-014 addendum, Phase B).

        Deterministic, operator-invoked, bounded-batched (ADR-028 pattern):

        - only rows with superseded_at IS NOT NULL are ever candidates;
          ACTIVE beliefs are never pruned (a belief only leaves the active
          set by being superseded, never by pruning) - fail closed;
        - the newest `keep_versions` rows per belief lineage (category +
          statement, ordered by superseded_at DESC, created_at DESC) are
          always retained; `keep_versions` defaults to 1;
        - `older_than` additionally restricts candidates to rows whose
          superseded_at is strictly before the ISO cutoff;
        - batch_size in [1, 5000] (fail closed outside); deletion drains in
          bounded SELECT-then-DELETE chunks;
        - dry_run computes the count and mutates nothing.

        Returns the number of belief rows removed (or that WOULD be removed
        in a dry run). Idempotent: a second call with the same arguments
        returns 0.
        """
        if (isinstance(keep_versions, bool) or not isinstance(keep_versions, int)
                or keep_versions < 1):
            raise ValueError(
                f"keep_versions must be an int >= 1, got {keep_versions!r} "
                f"(fail closed)")
        if (isinstance(batch_size, bool) or not isinstance(batch_size, int)
                or not (1 <= batch_size <= 5000)):
            raise ValueError(
                f"batch_size must be within [1, 5000], got {batch_size!r} "
                f"(fail closed)")
        cutoff: str | None = None
        if older_than is not None:
            try:
                cutoff = str(older_than).replace("Z", "+00:00")
                datetime.fromisoformat(cutoff)
            except (ValueError, TypeError):
                raise ValueError(
                    f"older_than must be an ISO-8601 timestamp, got "
                    f"{older_than!r} (fail closed)") from None

        # Read-before-delete candidate selection: superseded rows only,
        # newest keep_versions per lineage protected.
        rows = self._conn.execute(
            "SELECT belief_id, category, statement, superseded_at, created_at "
            "FROM beliefs WHERE superseded_at IS NOT NULL"
        ).fetchall()
        lineages: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for r in rows:
            lineages.setdefault((r[1], r[2]), []).append(r)
        doomed: list[str] = []
        for lineage_rows in lineages.values():
            # newest first (superseded_at DESC, created_at DESC tiebreak)
            lineage_rows.sort(key=lambda r: (r[3], r[4]), reverse=True)
            for rank, r in enumerate(lineage_rows):
                if rank < keep_versions:
                    continue  # newest keep_versions per lineage protected
                if cutoff is None or r[3] < cutoff:
                    doomed.append(r[0])
        if dry_run:
            return len(doomed)

        removed = 0
        for i in range(0, len(doomed), batch_size):
            chunk = doomed[i:i + batch_size]
            placeholders = ", ".join("?" * len(chunk))
            cur = self._conn.execute(
                f"DELETE FROM beliefs WHERE belief_id IN ({placeholders})",
                chunk,
            )
            removed += cur.rowcount
        self._conn.commit()
        return removed

    @_threadsafe
    def prune_goal_plans(self, goal_id: str | None = None,
                         keep_latest: int = 10,
                         batch_size: int = 500,
                         dry_run: bool = False) -> int:
        """Bound replan history (ADR-014 addendum, Phase B).

        Deterministic, operator-invoked, bounded-batched (ADR-028 pattern):

        - keeps the newest `keep_latest` immutable plan versions per goal
          (ordered by plan_version); the LATEST version per goal is never
          pruned (keep_latest >= 1, replay/latest-version safety) - fail
          closed;
        - goal_id scopes the prune to one goal; None prunes all goals;
        - batch_size in [1, 5000] (fail closed outside); deletion drains in
          bounded SELECT-then-DELETE chunks;
        - dry_run computes the count and mutates nothing.

        Returns the number of plan rows removed (or that WOULD be removed in
        a dry run). Idempotent: a second call with the same arguments
        returns 0.
        """
        if (isinstance(keep_latest, bool) or not isinstance(keep_latest, int)
                or keep_latest < 1):
            raise ValueError(
                f"keep_latest must be an int >= 1, got {keep_latest!r} "
                f"(fail closed)")
        if (isinstance(batch_size, bool) or not isinstance(batch_size, int)
                or not (1 <= batch_size <= 5000)):
            raise ValueError(
                f"batch_size must be within [1, 5000], got {batch_size!r} "
                f"(fail closed)")

        # Read-before-delete candidate selection (newest per goal first).
        if goal_id is not None:
            rows = self._conn.execute(
                "SELECT goal_id, plan_version FROM goal_plans "
                "WHERE goal_id=? ORDER BY plan_version DESC",
                (goal_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT goal_id, plan_version FROM goal_plans "
                "ORDER BY goal_id, plan_version DESC"
            ).fetchall()
        per_goal: dict[str, list[int]] = {}
        for gid, version in rows:
            per_goal.setdefault(gid, []).append(int(version))
        doomed: dict[str, list[int]] = {}
        for gid, versions in per_goal.items():
            for rank, version in enumerate(versions):
                if rank >= keep_latest:
                    doomed.setdefault(gid, []).append(version)
        if dry_run:
            return sum(len(v) for v in doomed.values())

        removed = 0
        for gid, versions in doomed.items():
            for i in range(0, len(versions), batch_size):
                chunk = versions[i:i + batch_size]
                placeholders = ", ".join("?" * len(chunk))
                cur = self._conn.execute(
                    f"DELETE FROM goal_plans WHERE goal_id=? AND "
                    f"plan_version IN ({placeholders})",
                    (gid, *chunk),
                )
                removed += cur.rowcount
                # ADR-015 addendum Phase D: coupled strategy outcomes never
                # outlive their plan version - removed in the SAME bounded
                # batch (informational; the plan row is the authority).
                self._conn.execute(
                    f"DELETE FROM strategy_outcomes WHERE goal_id=? AND "
                    f"plan_version IN ({placeholders})",
                    (gid, *chunk),
                )
        self._conn.commit()
        return removed

    # ---- aggregate ----

    @_threadsafe
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

    @_threadsafe
    def close(self) -> None:
        self._conn.close()


def _strategy_outcome_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {c: v for c, v in zip(_OUTCOME_COLS, row)}


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
