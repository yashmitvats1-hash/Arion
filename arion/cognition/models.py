"""Cognitive State / World Model v1 - domain models (ADR-014).

Distinct from episodic memory (what happened), the cognitive state captures:

  semantic     - what Arion BELIEVES (derived from experience)
  procedural   - how to accomplish things (lessons, recommendations)
  preference   - user-specific behavior/preferences
  environment  - current world/system state (facts about the world)

Every DERIVED belief carries provenance (source episode/reflection/guidance
ids), confidence, timestamps, and a source marker (deterministic | model).

INFORMATIONAL ONLY: nothing here can grant permissions, alter actors, change
boundaries, approve actions, or register capabilities. Authorization remains
authoritative in PermissionPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arion.state.models import new_id, utcnow

BELIEF_CATEGORIES = ("semantic", "procedural", "preference", "environment")
BELIEF_SOURCES = ("deterministic", "model")
PROVENANCE_KEYS = frozenset({"episode_ids", "reflection_ids", "guidance_ids",
                             "consolidation_ids"})
STATEMENT_MAX = 600


def _validate_provenance(provenance: dict[str, Any]) -> None:
    if not isinstance(provenance, dict):
        raise ValueError("belief provenance must be a dict")
    unknown = set(provenance) - PROVENANCE_KEYS
    if unknown:
        raise ValueError(f"belief provenance has unknown key(s) {sorted(unknown)}")
    for key, values in provenance.items():
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"belief provenance {key!r} must be a list of non-empty strings")


@dataclass
class Belief:
    """A derived belief about the world or about how to do things.

    Versioning: belief updates are APPEND-ONLY + versioned. Deriving a revised
    belief creates a NEW row (new belief_id, version incremented) and
    supersedes the prior one (superseded_at set) - history is never rewritten
    in place. `list_beliefs` excludes superseded rows by default.
    """

    belief_id: str
    category: str                       # semantic | procedural | preference | environment
    statement: str                      # what Arion believes (bounded, curated)
    confidence: float = 0.5             # 0..1
    importance: float = 0.5             # 0..1
    provenance: dict[str, list[str]] = field(default_factory=dict)  # episode_ids/reflection_ids/guidance_ids
    source: str = "deterministic"       # deterministic | model
    version: int = 1
    superseded_at: str | None = None    # set when a newer revision supersedes this belief
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.category not in BELIEF_CATEGORIES:
            raise ValueError(f"unknown belief category {self.category!r}")
        if not self.statement.strip():
            raise ValueError("belief statement must be non-empty")
        if len(self.statement) > STATEMENT_MAX:
            raise ValueError(f"belief statement exceeds {STATEMENT_MAX} chars")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("belief confidence must be within [0,1]")
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError("belief importance must be within [0,1]")
        if self.source not in BELIEF_SOURCES:
            raise ValueError(f"belief source must be one of {BELIEF_SOURCES} (got {self.source!r})")
        if self.version < 1:
            raise ValueError("belief version must be >= 1")
        _validate_provenance(self.provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "belief_id": self.belief_id,
            "category": self.category,
            "statement": self.statement,
            "confidence": round(self.confidence, 3),
            "importance": round(self.importance, 3),
            "provenance": self.provenance,
            "source": self.source,
            "version": self.version,
            "superseded_at": self.superseded_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Belief":
        return cls(
            belief_id=d["belief_id"],
            category=d.get("category", "semantic"),
            statement=d.get("statement", ""),
            confidence=float(d.get("confidence", 0.5)),
            importance=float(d.get("importance", 0.5)),
            provenance=d.get("provenance", {}) or {},
            source=d.get("source", "deterministic"),
            version=int(d.get("version", 1)),
            superseded_at=d.get("superseded_at"),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


PREFERENCE_SOURCES = ("user", "inferred", "model")


@dataclass
class Preference:
    """A user-specific behavior/preference (informational).

    Upserted by (key, user); provenance recorded; cannot authorize anything.
    """

    preference_id: str
    key: str
    value: str
    user: str = "default"
    source: str = "user"  # user | inferred | model
    provenance: dict[str, list[str]] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("preference key must be non-empty")
        if not self.value.strip():
            raise ValueError("preference value must be non-empty")
        if not self.user.strip():
            raise ValueError("preference user must be non-empty")
        if self.source not in PREFERENCE_SOURCES:
            raise ValueError(f"preference source must be one of {PREFERENCE_SOURCES} (got {self.source!r})")
        _validate_provenance(self.provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_id": self.preference_id,
            "key": self.key,
            "value": self.value,
            "user": self.user,
            "source": self.source,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Preference":
        return cls(
            preference_id=d.get("preference_id", new_id("pref")),
            key=d["key"],
            value=d.get("value", ""),
            user=d.get("user", "default"),
            source=d.get("source", "user"),
            provenance=d.get("provenance", {}) or {},
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


@dataclass
class EnvironmentFact:
    """A fact about the current world/system state (informational).

    Versioned per key: observing a changed value increments `version` and sets
    `observed_at`, so stale facts are detectable (see WorldStateMonitor).
    """

    fact_id: str
    key: str
    value: Any
    source: str = "system"
    version: int = 1
    observed_at: str | None = None   # last time this value was observed/changed
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("environment fact key must be non-empty")
        if not self.source.strip():
            raise ValueError("environment fact source must be non-empty")
        if self.version < 1:
            raise ValueError("environment fact version must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "version": self.version,
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentFact":
        return cls(
            fact_id=d.get("fact_id", new_id("fact")),
            key=d["key"],
            value=d.get("value"),
            source=d.get("source", "system"),
            version=int(d.get("version", 1)),
            observed_at=d.get("observed_at"),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


@dataclass
class CognitiveSnapshot:
    """A structured, bounded view of Arion's cognitive state."""

    beliefs: list[Belief] = field(default_factory=list)
    preferences: list[Preference] = field(default_factory=list)
    environment: list[EnvironmentFact] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self, limit_beliefs: int = 20) -> dict[str, Any]:
        return {
            "beliefs": [b.to_dict() for b in self.beliefs[:limit_beliefs]],
            "preferences": [p.to_dict() for p in self.preferences[:limit_beliefs]],
            "environment": [f.to_dict() for f in self.environment[:limit_beliefs]],
            "counts": self.counts,
        }
