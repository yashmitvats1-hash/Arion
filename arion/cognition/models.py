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


@dataclass
class Belief:
    """A derived belief about the world or about how to do things."""

    belief_id: str
    category: str                       # semantic | procedural | preference | environment
    statement: str                      # what Arion believes (bounded, curated)
    confidence: float = 0.5             # 0..1
    importance: float = 0.5             # 0..1
    provenance: dict[str, list[str]] = field(default_factory=dict)  # episode_ids/reflection_ids/guidance_ids
    source: str = "deterministic"       # deterministic | model
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.category not in BELIEF_CATEGORIES:
            raise ValueError(f"unknown belief category {self.category!r}")
        if not self.statement.strip():
            raise ValueError("belief statement must be non-empty")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("belief confidence must be within [0,1]")
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError("belief importance must be within [0,1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "belief_id": self.belief_id,
            "category": self.category,
            "statement": self.statement,
            "confidence": round(self.confidence, 3),
            "importance": round(self.importance, 3),
            "provenance": self.provenance,
            "source": self.source,
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
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


@dataclass
class Preference:
    """A user-specific behavior/preference (informational)."""

    preference_id: str
    key: str
    value: str
    user: str = "default"
    source: str = "user"  # user | inferred | model
    provenance: dict[str, list[str]] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_id": self.preference_id,
            "key": self.key,
            "value": self.value,
            "user": self.user,
            "source": self.source,
            "provenance": self.provenance,
            "created_at": self.created_at,
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
        )


@dataclass
class EnvironmentFact:
    """A fact about the current world/system state (informational)."""

    fact_id: str
    key: str
    value: Any
    source: str = "system"
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "key": self.key,
            "value": self.value,
            "source": self.source,
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
