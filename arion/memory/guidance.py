"""Memory-driven planning guidance (learning milestone).

Converts relevant prior experience into STRUCTURED planning guidance that a
planner can consume to choose a different (safer) strategy. The mechanism is
reusable across capabilities:

    previous outcome + capability/action + failure category
    + recommendation + confidence + importance + relevance
    -> MemoryGuidance(category, capability, action, resource, ...)

Categories:
  avoid   - do not target (capability, action, resource) - prior denial/failure
  prefer  - (capability, action, resource) previously succeeded
  informational - a lesson with no direct action mapping

Guidance is deterministic, bounded, and traceable (episode_id, reflection_id).
It is INFORMATIONAL: it can only change what the planner PROPOSES. The
authorization layer decides what may actually run.

apply_guidance_to_steps implements the deterministic plan transform: a step
targeting an "avoid" resource is re-targeted to a "prefer" resource for the
same capability/action, or dropped if no safe alternative exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from arion.memory.models import Episode, Reflection
from arion.state.models import PlanStep, new_id

GUIDANCE_CATEGORIES = ("avoid", "prefer", "informational")


@dataclass
class MemoryGuidance:
    """One structured piece of planning guidance derived from memory."""

    guidance_id: str
    category: str                      # avoid | prefer | informational
    capability: str | None = None
    action: str | None = None
    resource: str | None = None
    reason: str = ""
    recommendation: str = ""
    episode_id: str = ""
    reflection_id: str | None = None
    confidence: str = "medium"
    importance: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "guidance_id": self.guidance_id,
            "category": self.category,
            "capability": self.capability,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "recommendation": self.recommendation,
            "episode_id": self.episode_id,
            "reflection_id": self.reflection_id,
            "confidence": self.confidence,
            "importance": round(self.importance, 2),
        }


def _entry_for_resource(episode: Episode, resource: str) -> dict | None:
    for r in episode.resources:
        if r.get("resource") == resource:
            return r
    return None


def _entry_for_step(episode: Episode, step_index: int) -> dict | None:
    for r in episode.resources:
        if r.get("step") == step_index:
            return r
    return None


def _first_successful_resource(episode: Episode) -> dict | None:
    for r in episode.resources:
        if r.get("status") == "succeeded" and r.get("resource"):
            return r
    return None


def _first_resource(episode: Episode) -> str | None:
    """First declared resource from the episode's resources (if any)."""
    for r in episode.resources:
        if r.get("resource"):
            return str(r["resource"])
    return None


def _denied_resource(episode: Episode) -> str | None:
    for d in episode.authorization.get("denials", []):
        if d.get("resource"):
            return str(d["resource"])
    return None


def _capability_action(episode: Episode) -> tuple[str | None, str | None]:
    for r in episode.resources:
        return r.get("capability"), r.get("action")
    for s in episode.plan_summary:
        if s.get("capability"):
            return s.get("capability"), s.get("action")
    return None, None


def build_guidance_for_episode(episode: Episode, reflection: Reflection | None = None) -> MemoryGuidance | None:
    """Deterministic mapping: episode (+reflection) -> one guidance entry."""
    cap, act = _capability_action(episode)
    rec = reflection.recommendation if reflection else ""
    base = dict(
        episode_id=episode.episode_id,
        reflection_id=reflection.reflection_id if reflection else None,
        confidence=reflection.confidence if reflection else "medium",
        importance=episode.importance,
        recommendation=rec,
    )

    if episode.outcome == "denied":
        resource = _denied_resource(episode) or _first_resource(episode)
        entry = _entry_for_resource(episode, resource) if resource else None
        cap = entry.get("capability") if entry else cap
        act = entry.get("action") if entry else act
        reasons = [d.get("reason", "") for d in episode.authorization.get("denials", [])]
        return MemoryGuidance(
            guidance_id=new_id("guide"),
            category="avoid",
            capability=cap,
            action=act,
            resource=resource,
            reason="; ".join(str(r) for r in reasons if r)[:300] or "authorization denied",
            **base,
        )

    if episode.outcome == "failed":
        # target the resource of the FAILED step specifically
        failed_step = next((f.get("step") for f in episode.failures if f.get("step") is not None), None)
        entry = _entry_for_step(episode, failed_step) if failed_step is not None else None
        resource = entry.get("resource") if entry else _first_resource(episode)
        cap = entry.get("capability") if entry else cap
        act = entry.get("action") if entry else act
        category = next((f.get("category") for f in episode.failures if f.get("category")), "execution")
        reason = f"prior failure ({category}): " + (
            next((f.get("error", "") for f in episode.failures), "")[:200]
        )
        return MemoryGuidance(
            guidance_id=new_id("guide"),
            category="avoid",
            capability=cap,
            action=act,
            resource=resource,
            reason=reason,
            **base,
        )

    if episode.outcome == "completed":
        # prefer the first SUCCEEDED step's resource (skip 'list .' noise)
        entry = _first_successful_resource(episode)
        resource = entry.get("resource") if entry else _first_resource(episode)
        cap = entry.get("capability") if entry else cap
        act = entry.get("action") if entry else act
        return MemoryGuidance(
            guidance_id=new_id("guide"),
            category="prefer",
            capability=cap,
            action=act,
            resource=resource,
            reason="prior success",
            **base,
        )

    return None  # recovered or no useful signal


class DeterministicMemoryGuidance:
    """Converts retrieved episodes + reflections into bounded, deduplicated guidance."""

    def build(self, episodes: list[Episode], reflections: list[Reflection]) -> list[MemoryGuidance]:
        ref_by_ep = {r.episode_id: r for r in reflections}
        guidance: list[MemoryGuidance] = []
        for ep in episodes:
            g = build_guidance_for_episode(ep, ref_by_ep.get(ep.episode_id))
            if g is not None:
                guidance.append(g)
        # Deduplicate deterministically: keep the highest-importance entry per
        # (category, capability, action, resource); stable by guidance order.
        best: dict[tuple, MemoryGuidance] = {}
        order: list[tuple] = []
        for g in guidance:
            key = (g.category, g.capability, g.action, g.resource)
            if key not in best or g.importance > best[key].importance:
                best[key] = g
                order.append(key)
        seen: set = set()
        out: list[MemoryGuidance] = []
        for key in order:
            if key in seen:
                continue
            seen.add(key)
            out.append(best[key])
        # deterministic order: importance desc, then reason asc
        out.sort(key=lambda g: (-g.importance, g.reason))
        return out


# ---------------------------------------------------------------------------
# Deterministic plan transform
# ---------------------------------------------------------------------------


def apply_guidance_to_steps(steps: list[PlanStep], guidance: list[MemoryGuidance]) -> tuple[list[PlanStep], list[dict[str, Any]]]:
    """Re-target steps away from known-failing resources.

    For each step targeting (capability, action, resource) with an 'avoid'
    guidance entry, substitute the corresponding 'prefer' resource (same
    capability+action) if one exists; otherwise drop the step. Returns the
    transformed steps plus a record of every applied decision (for audit).

    Deterministic and explainable - no model involved.
    """
    avoid = [g for g in guidance if g.category == "avoid" and g.capability and g.resource]
    prefer = {
        (g.capability, g.action): g
        for g in guidance
        if g.category == "prefer" and g.capability and g.resource
    }
    applied: list[dict[str, Any]] = []
    out: list[PlanStep] = []
    for step in steps:
        target = step.params.get("path")  # resource param used by filesystem actions
        if not isinstance(target, str):
            out.append(step)
            continue
        hit = next(
            (g for g in avoid
             if g.capability == step.capability and g.action == step.action and g.resource == target),
            None,
        )
        if hit is None:
            out.append(step)
            continue
        alt = prefer.get((step.capability, step.action))
        if alt is not None and alt.resource:
            step.params["path"] = alt.resource
            applied.append({
                "step_index": step.index,
                "category": "resource_substitution",
                "avoid": {"capability": hit.capability, "action": hit.action, "resource": hit.resource},
                "prefer": {"capability": alt.capability, "action": alt.action, "resource": alt.resource},
                "guidance_id": alt.guidance_id,
                "episode_id": alt.episode_id,
                "reflection_id": alt.reflection_id,
            })
            out.append(step)
        else:
            applied.append({
                "step_index": step.index,
                "category": "step_skipped",
                "avoid": {"capability": hit.capability, "action": hit.action, "resource": hit.resource},
                "guidance_id": hit.guidance_id,
                "episode_id": hit.episode_id,
                "reflection_id": hit.reflection_id,
            })
            # no safe alternative: drop the doomed step
    return out, applied
