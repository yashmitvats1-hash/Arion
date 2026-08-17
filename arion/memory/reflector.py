"""Reflection abstraction (ADR-012).

Reflections are structured, informational lessons about an episode. The first
implementation is deterministic (no LLM) so the whole loop stays testable
offline (ADR-008); a ModelReflector can implement the same protocol later.

REFLECTION SAFETY: a reflection may RECOMMEND future behavior but can never
trigger execution. Only a future plan that explicitly proposes an action, plus
the capability + authorization layers independently permitting it, may act.
"""

from __future__ import annotations

from typing import Protocol

from arion.memory.models import Episode, Reflection
from arion.state.models import new_id, utcnow

CONFIDENCES = ("low", "medium", "high")


class Reflector(Protocol):
    """Produces a structured reflection from an episode."""

    def reflect(self, episode: Episode) -> Reflection: ...


class DeterministicReflector:
    """Template-based reflector: deterministic, offline, testable.

    Distinguishes success vs failure vs denial and builds structured lessons
    from the episode's data - no model, no magic.
    """

    def reflect(self, episode: Episode) -> Reflection:
        goal = episode.goal[:200] if episode.goal else "(unknown goal)"
        task_ref = episode.task_id or "?"
        what_happened = f"Task {task_ref} for goal {goal!r} ended with outcome {episode.outcome}."

        succeeded = [s.get("intent", "") for s in episode.plan_summary if s.get("status") == "succeeded"]
        what_worked = "Completed steps: " + (", ".join(succeeded[:5]) if succeeded else "none") + "."

        failed = episode.failures
        if failed:
            detail = "; ".join(f.get("error", "")[:120] for f in failed[:3])
            what_failed = f"{len(failed)} failure(s): {detail}"
        elif episode.authorization.get("denials"):
            denials = episode.authorization["denials"]
            what_failed = "Authorization denied: " + "; ".join(
                f"{d.get('scope')} {d.get('resource') or ''}".strip()[:120] for d in denials[:3]
            )
        else:
            what_failed = "No step failures."

        if episode.outcome == "completed":
            why = "All planned steps succeeded and passed verification."
            lesson = f"Goal {goal!r} is achievable with the current capability set."
            recommendation = "Reuse a similar plan for comparable goals."
            confidence = "high" if not failed else "medium"
        elif episode.outcome == "denied":
            scopes = {d.get("scope") for d in episode.authorization.get("denials", [])}
            why = f"Task was blocked by authorization (scopes: {sorted(scopes) or 'unknown'})."
            lesson = "This goal requires authorization the current policy does not grant."
            recommendation = "Do not attempt this action; request policy change or human approval instead."
            confidence = "high"
        elif episode.outcome == "recovered":
            why = "The task recovered after interruption (resume/re-execution)."
            lesson = "Resume-based recovery preserved task state."
            recommendation = "Prefer resumable plans for long-running goals."
            confidence = "medium"
        else:  # failed
            categories = {f.get("category") for f in failed if f.get("category")}
            why = "Step(s) failed during execution." + (f" Categories: {sorted(categories)}." if categories else "")
            lesson = f"Goal {goal!r} failed and may need a different plan, capability, or authorization."
            recommendation = "Retry with adjusted parameters or a different approach; verify prerequisites first."
            confidence = "medium"

        return Reflection(
            reflection_id=new_id("refl"),
            episode_id=episode.episode_id,
            what_happened=what_happened,
            what_worked=what_worked,
            what_failed=what_failed,
            why=why,
            lesson=lesson,
            recommendation=recommendation,
            confidence=confidence,
            importance=round(min(1.0, episode.importance + (0.1 if episode.outcome == "failed" else 0.0)), 2),
            created_at=utcnow(),
        )
