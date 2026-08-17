"""Memory-driven planning guidance (learning milestone, hardened).

Converts relevant prior experience into STRUCTURED planning guidance that a
planner can consume to choose a different (safer) strategy. The mechanism is
reusable across capabilities:

    previous outcome + capability/action + failure category
    + recommendation + confidence + importance + relevance
    -> MemoryGuidance(category, capability, action, resource, strategy)

Categories:
  avoid   - do not target (capability, action, resource) - prior denial/failure
  prefer  - (capability, action, resource) previously succeeded
  informational - a lesson with no direct action mapping

Guidance is deterministic, bounded, and traceable (episode_id, reflection_id).
It is INFORMATIONAL: it can only change what the planner PROPOSES. The
authorization layer decides what may actually run.

apply_guidance_to_steps implements the deterministic, NON-MUTATING plan
transform:

- resources are resolved through a resolver callback (capability registry
  ActionSpec.resource_param), NEVER a hardcoded "path" assumption;
- the ORIGINAL plan is retained (deep copies) alongside the transformed plan;
- every transformation records provenance (guidance_id, episode_id,
  reflection_id) both in the decisions list and on the transformed PlanStep;
- strategy-level changes: an avoid'ed step is (1) resource-substituted when a
  'prefer' exists for the same action, (2) action-substituted when a 'prefer'
  exists for a DIFFERENT action of the same capability (materially different
  execution strategy), or (3) dropped.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from arion.memory.models import Episode, Reflection
from arion.state.models import PlanStep, new_id

GUIDANCE_CATEGORIES = ("avoid", "prefer", "informational")

# resource_param resolver: (capability, action) -> param key holding the resource
ResourceParamResolver = Callable[[str, str], str | None]
# action metadata resolver: (capability, action) -> ActionSpec | None (registry)
ActionMetaResolver = Callable[[str, str], Any | None]


@dataclass
class MemoryGuidance:
    """One structured piece of planning guidance derived from memory."""

    guidance_id: str
    category: str                      # avoid | prefer | informational
    capability: str | None = None
    action: str | None = None
    resource: str | None = None
    strategy: str | None = None        # e.g. "alternative_action", "defer", "verify_alt"
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
            "strategy": self.strategy,
            "reason": self.reason,
            "recommendation": self.recommendation,
            "episode_id": self.episode_id,
            "reflection_id": self.reflection_id,
            "confidence": self.confidence,
            "importance": round(self.importance, 2),
        }


@dataclass
class PlanTransformation:
    """Auditable, non-mutating result of applying guidance to a plan.

    original    - the plan BEFORE guidance (deep copies of the input)
    transformed - the plan AFTER guidance (new objects; originals untouched)
    decisions   - provenance for every transformation applied
    """

    original: list[PlanStep]
    transformed: list[PlanStep]
    decisions: list[dict[str, Any]]


def registry_resource_param(registry, capability: str, action: str) -> str | None:
    """Resolve an action's resource parameter from the capability registry."""
    if registry is None:
        return None
    try:
        spec = registry.action_spec(capability, action)
    except Exception:
        return None
    if spec is not None and spec.resource_kind and spec.resource_param:
        return spec.resource_param
    return None


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
            strategy="defer",  # authorization-driven avoidance: defer / do not attempt
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
            strategy="alternative_action",  # strategy-level: prefer a different approach
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
# Deterministic, non-mutating plan transform
# ---------------------------------------------------------------------------


def apply_guidance_to_steps(
    steps: list[PlanStep],
    guidance: list[MemoryGuidance],
    resource_param_resolver: ResourceParamResolver | None = None,
    action_meta_resolver: ActionMetaResolver | None = None,
) -> PlanTransformation:
    """Re-target steps away from known-failing resources WITHOUT mutating inputs.

    Resource params are resolved via the resolver (capability registry
    ActionSpec.resource_param) - never a hardcoded 'path' assumption.

    Strategy per avoid'ed step:
      1. resource_substitution - a 'prefer' exists for the same capability+action
      2. action_substitution   - a 'prefer' exists for a DIFFERENT action of the
                                 same capability (materially different strategy)
      3. step_skipped          - no safe alternative: the step is kept in the
                                 plan with status SKIPPED and its provenance
                                 (NEVER silently deleted - the engine treats
                                 SKIPPED as an explicit terminal state)

    FAIL-CLOSED RESOLUTION: resource resolution must come from the capability
    registry (ActionSpec.resource_param). If NO resolver is provided, the
    transform refuses to guess (raises ValueError) rather than assuming a
    filesystem 'path' param. If the resolver returns None for a specific
    capability/action, that step is left untransformed (we cannot safely
    target its resource).

    Returns PlanTransformation(original, transformed, decisions); every
    decision records provenance (guidance/episode/reflection ids), and each
    transformed step carries its provenance in step.guidance.
    """
    if resource_param_resolver is None:
        raise ValueError(
            "apply_guidance_to_steps requires a registry-driven resource_param_resolver; "
            "refusing to guess a resource param (fail closed)"
        )
    original = [copy.deepcopy(s) for s in steps]
    resolver = resource_param_resolver

    avoid = [g for g in guidance if g.category == "avoid" and g.capability and g.resource]
    prefer_by_action = {
        (g.capability, g.action): g
        for g in guidance
        if g.category == "prefer" and g.capability and g.resource
    }
    prefer_by_capability: dict[str, list[MemoryGuidance]] = {}
    for g in prefer_by_action.values():
        prefer_by_capability.setdefault(g.capability, []).append(g)

    decisions: list[dict[str, Any]] = []
    transformed: list[PlanStep] = []
    for step in original:
        param_key = resolver(step.capability, step.action)
        target = step.params.get(param_key) if param_key else None
        if not isinstance(target, str):
            transformed.append(copy.deepcopy(step))
            continue
        hit = next(
            (g for g in avoid
             if g.capability == step.capability and g.action == step.action and g.resource == target),
            None,
        )
        if hit is None:
            transformed.append(copy.deepcopy(step))
            continue

        s = copy.deepcopy(step)

        # Strategy 1: resource substitution (same capability+action, safe resource).
        # The preferred resource must NOT itself be a known-failing target
        # (race hardening, ADR-016): a 'prefer' from an old episode is not a
        # license to re-target work onto a resource that has since failed.
        alt = prefer_by_action.get((step.capability, step.action))
        alt_avoided = (
            alt is not None and any(
                g.capability == alt.capability and g.action == alt.action and g.resource == alt.resource
                for g in avoid
            )
        )
        if alt is not None and alt.resource and not alt_avoided:
            s.params[param_key] = alt.resource
            provenance = {
                "category": "resource_substitution",
                "capability": step.capability,
                "action": step.action,
                "original_resource": target,
                "new_resource": alt.resource,
                "guidance_id": alt.guidance_id,
                "episode_id": alt.episode_id,
                "reflection_id": alt.reflection_id,
            }
            decisions.append({"step_index": step.index, **provenance})
            s.guidance.append(provenance)
            transformed.append(s)
            continue

        # Strategy 2: action substitution (different action, same capability).
        # Same race guard: never substitute onto an avoided resource.
        alt_action = None
        for g in prefer_by_capability.get(step.capability, []):
            if g.action != step.action and g.resource and not any(
                a.capability == g.capability and a.action == g.action and a.resource == g.resource
                for a in avoid
            ):
                alt_action = g
                break
        if alt_action is not None:
            new_key = resolver(step.capability, alt_action.action)
            s.action = alt_action.action
            if new_key:
                s.params = {new_key: alt_action.resource}
            # adopt the NEW action's verification expectations from the
            # registry (e.g. list -> non_empty, not the old read's schema_keys)
            if action_meta_resolver is not None:
                try:
                    new_spec = action_meta_resolver(step.capability, s.action)
                    if new_spec is not None and new_spec.default_verification:
                        from arion.state.models import VerificationPolicy

                        dv = new_spec.default_verification
                        s.verification = VerificationPolicy(
                            policy=dv.get("policy", "non_empty"),
                            args=dv.get("args", {}),
                        )
                except Exception:
                    pass
            provenance = {
                "category": "action_substitution",
                "strategy": "alternative_action",
                "capability": step.capability,
                "original_action": step.action,
                "new_action": alt_action.action,
                "original_resource": target,
                "new_resource": alt_action.resource,
                "guidance_id": alt_action.guidance_id,
                "episode_id": alt_action.episode_id,
                "reflection_id": alt_action.reflection_id,
            }
            decisions.append({"step_index": step.index, **provenance})
            s.guidance.append(provenance)
            transformed.append(s)
            continue

        # Strategy 3: no safe alternative -> SKIP the doomed step EXPLICITLY.
        # The step is retained in the plan with status SKIPPED and its
        # provenance, so the orchestrator always has a terminal task state and
        # the action is never silently deleted.
        from arion.state.models import StepStatus

        provenance = {
            "category": "step_skipped",
            "capability": step.capability,
            "action": step.action,
            "resource": target,
            "guidance_id": hit.guidance_id,
            "episode_id": hit.episode_id,
            "reflection_id": hit.reflection_id,
            "reason": hit.reason[:200],
        }
        s.status = StepStatus.SKIPPED
        s.skipped_reason = hit.reason[:200] or "no safe alternative per memory guidance"
        s.guidance.append(provenance)
        decisions.append({"step_index": step.index, **provenance})
        transformed.append(s)

    return PlanTransformation(original=original, transformed=transformed, decisions=decisions)
