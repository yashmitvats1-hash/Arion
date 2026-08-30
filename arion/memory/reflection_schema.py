"""Strict reflection schema + validation (ADR-012, learning milestone).

A reflection may contain ONLY informational fields:
  what_happened, what_worked, what_failed, why, lesson, recommendation,
  confidence, importance (plus reflection_id / episode_id / created_at).

It must NEVER contain authority-bearing fields:
  scope, permissions, actor, grant, approve, authorization,
  capability_registration, resource_boundary, allowed, boundary, register, ...

Malformed model-generated reflections are rejected here before storage; they
can never influence execution. Validation is strict and offline-testable.
"""

from __future__ import annotations

import json
from typing import Any

from arion.memory.models import Reflection
from arion.state.models import utcnow

REFLECTION_SCHEMA_VERSION = "1.0"

REFLECTION_FIELDS = frozenset(
    {
        "reflection_id",
        "episode_id",
        "what_happened",
        "what_worked",
        "what_failed",
        "why",
        "lesson",
        "recommendation",
        "confidence",
        "importance",
        "created_at",
    }
)
REQUIRED_FIELDS = frozenset({"what_happened", "what_worked", "what_failed",
                             "why", "lesson", "recommendation", "confidence"})
CONFIDENCES = ("low", "medium", "high")

# Fields a reflection must NEVER carry (would attempt to influence authority).
FORBIDDEN_FIELDS = frozenset(
    {
        "scope", "permissions", "permission", "actor", "grant", "approve",
        "authorization", "authorize", "capability_registration", "register",
        "resource_boundary", "boundary", "allowed", "allow", "deny",
        "risk_level", "side_effects", "idempotent", "retry_safe", "policy",
    }
)


class ReflectionValidationError(ValueError):
    """Raised when a reflection is malformed or contains forbidden fields."""


def validate_reflection_dict(d: Any, episode_id: str | None = None) -> Reflection:
    """Strictly validate a (model-produced) reflection dict into a Reflection.

    - must be a JSON object with only known fields;
    - required fields present with correct types;
    - confidence in (low, medium, high); importance in [0,1];
    - no forbidden authority-bearing fields anywhere (top-level or nested).
    Raises ReflectionValidationError on any violation.
    """
    if not isinstance(d, dict):
        raise ReflectionValidationError("reflection must be a JSON object")

    unknown = set(d) - REFLECTION_FIELDS
    if unknown:
        forbidden = sorted(unknown & FORBIDDEN_FIELDS)
        if forbidden:
            raise ReflectionValidationError(
                f"reflection contains forbidden field(s) {forbidden} - "
                "reflections are informational and cannot carry authority fields"
            )
        raise ReflectionValidationError(f"reflection contains unknown field(s) {sorted(unknown)}")

    missing = REQUIRED_FIELDS - set(d)
    if missing:
        raise ReflectionValidationError(f"reflection missing required field(s) {sorted(missing)}")

    for field in ("what_happened", "what_worked", "what_failed", "why", "lesson", "recommendation"):
        if not isinstance(d.get(field), str) or not d[field].strip():
            raise ReflectionValidationError(f"reflection field {field!r} must be a non-empty string")

    confidence = d.get("confidence")
    if confidence not in CONFIDENCES:
        raise ReflectionValidationError(f"reflection confidence must be one of {CONFIDENCES} (got {confidence!r})")

    importance = d.get("importance", 0.5)
    if not isinstance(importance, (int, float)) or isinstance(importance, bool):
        raise ReflectionValidationError("reflection importance must be a number")
    if not (0.0 <= float(importance) <= 1.0):
        raise ReflectionValidationError("reflection importance must be within [0, 1]")

    target_episode = d.get("episode_id") or episode_id
    if not isinstance(target_episode, str) or not target_episode:
        raise ReflectionValidationError("reflection episode_id must be a non-empty string")

    return Reflection(
        reflection_id=d.get("reflection_id") or f"refl_{abs(hash(target_episode + d.get('lesson', ''))):x}",
        episode_id=target_episode,
        what_happened=d["what_happened"][:1000],
        what_worked=d["what_worked"][:1000],
        what_failed=d["what_failed"][:1000],
        why=d["why"][:1000],
        lesson=d["lesson"][:1000],
        recommendation=d["recommendation"][:1000],
        confidence=confidence,
        importance=float(importance),
        # Model output omits created_at by contract (the reflection schema
        # fields are the informational ones); default to now so the durable
        # reflections.created_at NOT NULL insert never fails on a
        # well-formed model reflection (ADR-057 M4).
        created_at=d.get("created_at") or utcnow(),
    )


def reflection_from_json(text: str, episode_id: str | None = None) -> Reflection:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReflectionValidationError(f"reflection: malformed JSON: {exc}") from exc
    return validate_reflection_dict(obj, episode_id=episode_id)
