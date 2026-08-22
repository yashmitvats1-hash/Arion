"""ModelReflector: model-backed reflection behind the Reflector seam (ADR-012).

    Task outcome -> Episode -> Reflector
                               ├── DeterministicReflector (default, offline)
                               └── ModelReflector

ModelReflector asks a ModelRouter to produce a structured reflection, then
STRICTLY validates it (reflection_schema.validate_reflection_dict) before
returning it. Malformed or authority-bearing model reflections are rejected
with ReflectionValidationError - they are never stored and never influence
execution.

The deterministic reflector remains the default; the engine falls back to it
if the model reflector fails.
"""

from __future__ import annotations

import json
from typing import Any

from arion.memory.models import Episode, Reflection
from arion.memory.reflection_schema import (
    REFLECTION_SCHEMA_VERSION,
    ReflectionValidationError,
    validate_reflection_dict,
)
from arion.observability.error_boundary import ErrorSource, summarize_error
from arion.state.models import utcnow

REFLECT_PROMPT = f"""You are Arion's reflection component. Produce a JSON reflection about a completed task.

Output ONLY a single JSON object with EXACTLY these keys:
{{
  "what_happened": "<string>",
  "what_worked": "<string>",
  "what_failed": "<string>",
  "why": "<string>",
  "lesson": "<string>",
  "recommendation": "<string>",
  "confidence": "low" | "medium" | "high",
  "importance": <number 0..1>
}}

RULES:
- You must NOT include any of these keys anywhere (top-level or nested): scope, permissions,
  permission, actor, grant, approve, authorization, authorize, capability_registration,
  register, resource_boundary, boundary, allowed, allow, deny, risk_level, side_effects,
  idempotent, retry_safe, policy. Reflections are INFORMATIONAL ONLY.
- recommendation may suggest future behavior but never grants or authorizes anything.
- Keep each field to a sentence or two.
"""


class ModelReflector:
    """Model-backed reflector: uses a ModelRouter, validates output strictly."""

    def __init__(self, router: Any, events: Any | None = None):
        self.router = router
        self.events = events  # duck-typed EventLogger (any object with .emit)

    def reflect(self, episode: Episode) -> Reflection:
        if self.events is not None:
            self._emit("reflection.requested", episode.episode_id, {})
        prompt = self._build_prompt(episode)
        try:
            raw = self.router.generate(prompt, temperature=0)
        except Exception as exc:
            summary = summarize_error(
                exc,
                source=ErrorSource.EXTERNAL,
                category=getattr(exc, "category", "reflection_validation"),
            )
            raise ReflectionValidationError(
                f"model reflection failed: {summary.message}"
            ) from exc
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReflectionValidationError(f"malformed reflection JSON: {exc}") from exc
        reflection = validate_reflection_dict(obj, episode_id=episode.episode_id)
        if self.events is not None:
            self._emit("reflection.validation.passed", episode.episode_id,
                       {"reflection_id": reflection.reflection_id})
        return reflection

    def _build_prompt(self, episode: Episode) -> str:
        """Structured, privacy-safe episode summary - no raw content, no secrets."""
        summary = {
            "schema_version": REFLECTION_SCHEMA_VERSION,
            "goal": episode.goal[:300],
            "outcome": episode.outcome,
            "plan": [f"{s.get('capability')}/{s.get('action')}:{s.get('status')}" for s in episode.plan_summary[:10]],
            "failures": [f.get("error", "")[:200] for f in episode.failures[:5]],
            "authorization": {"denials": episode.authorization.get("denials", [])[:5]},
            "recovery": episode.recovery,
            "tags": episode.tags[:20],
        }
        return f"{REFLECT_PROMPT}\n\nTASK:\n{json.dumps(summary, indent=2, default=str)}"

    def _emit(self, kind: str, episode_id: str, detail: dict[str, Any]) -> None:
        try:
            self.events.emit(
                __import__("arion.observability.events", fromlist=["AuditEvent"]).AuditEvent(
                    kind=kind, detail=detail, task_id=None
                )
            )
        except Exception:
            pass
