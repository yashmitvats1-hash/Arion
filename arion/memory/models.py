"""Memory domain models: Episodes, Reflections, filters, and the bounded
planning context (ADR-012).

Memory is structured, not a transcript archive: episodes summarize what
happened (goal, plan, actions, outcome, failures, authorization, recovery),
and reflections are structured lessons. No raw prompts/responses or secrets
are stored by default - params appear only as their KEY NAMES, never values.

Memory is INFORMATIONAL ONLY. It lives entirely outside the authorization
chain: nothing here can grant permissions, alter actors, change boundaries,
approve actions, or register capabilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from arion.state.models import new_id, utcnow

# Outcome vocabulary for episodes.
EPISODE_OUTCOMES = ("completed", "failed", "denied", "recovered")


@dataclass
class Episode:
    """A structured record of one meaningful task experience."""

    episode_id: str
    goal: str
    outcome: str  # completed | failed | denied | recovered
    task_id: str | None = None
    goal_id: str | None = None
    plan_summary: list[dict[str, Any]] = field(default_factory=list)  # steps: intent/capability/action/status/params_keys
    actions: list[dict[str, Any]] = field(default_factory=list)      # capability/action/status/attempts
    resources: list[dict[str, Any]] = field(default_factory=list)    # declared resource values: {capability, action, resource}
    verification: dict[str, Any] = field(default_factory=dict)       # passed/failed step indices
    failures: list[dict[str, Any]] = field(default_factory=list)     # step, capability, action, error (bounded), category
    authorization: dict[str, Any] = field(default_factory=dict)      # denials, approvals_required
    recovery: dict[str, Any] = field(default_factory=dict)           # resumed, re_executed
    tags: list[str] = field(default_factory=list)                    # capability names, outcome, categories, context tags
    importance: float = 0.5                                          # 0..1 salience
    reflection_id: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.outcome not in EPISODE_OUTCOMES:
            raise ValueError(f"unknown episode outcome {self.outcome!r} (allowed: {EPISODE_OUTCOMES})")
        if not self.episode_id:
            raise ValueError("episode_id must be non-empty")
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError("importance must be within [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "goal": self.goal,
            "plan_summary": self.plan_summary,
            "actions": self.actions,
            "resources": self.resources,
            "outcome": self.outcome,
            "verification": self.verification,
            "failures": self.failures,
            "authorization": self.authorization,
            "recovery": self.recovery,
            "tags": self.tags,
            "importance": self.importance,
            "reflection_id": self.reflection_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Episode":
        return cls(
            episode_id=d["episode_id"],
            task_id=d.get("task_id"),
            goal_id=d.get("goal_id"),
            goal=d.get("goal", ""),
            plan_summary=d.get("plan_summary", []) or [],
            actions=d.get("actions", []) or [],
            resources=d.get("resources", []) or [],
            outcome=d.get("outcome", "failed"),
            verification=d.get("verification", {}) or {},
            failures=d.get("failures", []) or [],
            authorization=d.get("authorization", {}) or {},
            recovery=d.get("recovery", {}) or {},
            tags=d.get("tags", []) or [],
            importance=float(d.get("importance", 0.5)),
            reflection_id=d.get("reflection_id"),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


@dataclass
class Reflection:
    """Structured reflection on an episode (deterministic or model-backed).

    Reflection is informational: it can RECOMMEND future behavior but can
    never trigger execution. Only a future plan + capability + authorization
    may act.
    """

    reflection_id: str
    episode_id: str
    what_happened: str
    what_worked: str
    what_failed: str
    why: str
    lesson: str
    recommendation: str
    confidence: str  # low | medium | high
    importance: float = 0.5
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reflection_id": self.reflection_id,
            "episode_id": self.episode_id,
            "what_happened": self.what_happened,
            "what_worked": self.what_worked,
            "what_failed": self.what_failed,
            "why": self.why,
            "lesson": self.lesson,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "importance": self.importance,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Reflection":
        return cls(
            reflection_id=d["reflection_id"],
            episode_id=d["episode_id"],
            what_happened=d.get("what_happened", ""),
            what_worked=d.get("what_worked", ""),
            what_failed=d.get("what_failed", ""),
            why=d.get("why", ""),
            lesson=d.get("lesson", ""),
            recommendation=d.get("recommendation", ""),
            confidence=d.get("confidence", "medium"),
            importance=float(d.get("importance", 0.5)),
            created_at=d.get("created_at", utcnow()),
        )


@dataclass
class EpisodeFilter:
    """Structured filters for deterministic retrieval."""

    outcome: str | None = None            # completed | failed | denied | recovered
    capability: str | None = None         # capability name appearing in tags
    failure_category: str | None = None   # e.g. provider_unavailable, schema_validation, ...
    tag: str | None = None                # arbitrary tag
    text: str | None = None               # substring match on goal text
    limit: int = 50


@dataclass
class ContextBudget:
    """Bounded, deterministic context-selection policy (ADR-012).

    The model receives recent relevant episodes + high-importance relevant
    episodes + recent reflections, all capped by these limits and a total
    character budget. Retrieval is bounded and deterministic.
    """

    max_episodes: int = 5
    max_reflections: int = 3
    max_chars: int = 4000
    recency_window: int = 20  # how many recent episodes are considered


@dataclass
class PlanningContext:
    """Explicit context object handed to the planner/model.

    Contains only relevant, bounded memory - never the whole database. Kept
    STRUCTURED internally: historical_facts (episodes), reflections,
    recommendations (guidance), and provenance are separate fields, not one
    opaque string. digest() produces the serializable form shown to a model
    (summaries, no secrets, no raw transcripts).
    """

    goal: str
    episodes: list[Episode] = field(default_factory=list)          # historical_facts
    reflections: list[Reflection] = field(default_factory=list)    # reflections
    guidance: list = field(default_factory=list)                   # recommendations (MemoryGuidance)
    provenance: dict[str, list[str]] = field(default_factory=dict)  # episode_ids, reflection_ids, guidance_ids
    strategy: Any | None = None                                    # Strategy (ADR-015), informational
    environment: list = field(default_factory=list)                # current world-state facts (bounded)
    plan_history: list = field(default_factory=list)               # previous goal plan versions (bounded, immutable)
    recovery: list = field(default_factory=list)                   # ADVISORY mutation-recovery records (ADR-020);
                                                                   # bounded, informational - never authorization
    budget: ContextBudget = field(default_factory=ContextBudget)

    def __post_init__(self) -> None:
        if self.strategy is None and self.provenance:
            self.provenance.setdefault("strategy_ids", [])

    def digest(self) -> dict[str, Any]:
        """Bounded, privacy-safe serialization for model consumption."""
        budget = self.budget
        episodes = self.episodes[: budget.max_episodes]
        reflections = self.reflections[: budget.max_reflections]
        guidance = self.guidance[: budget.max_episodes]

        ep = [
            {
                "episode_id": e.episode_id,
                "goal": e.goal[:300],
                "outcome": e.outcome,
                "tags": e.tags[:20],
                "importance": round(e.importance, 2),
                "plan": [f"{s.get('capability')}/{s.get('action')}" for s in e.plan_summary[:10]],
                "failures": [f.get("error", "")[:200] for f in e.failures[:5]],
                "created_at": e.created_at,
            }
            for e in episodes
        ]
        ref = [
            {
                "reflection_id": r.reflection_id,
                "what_happened": r.what_happened[:200],
                "lesson": r.lesson[:200],
                "recommendation": r.recommendation[:200],
                "confidence": r.confidence,
                "importance": round(r.importance, 2),
            }
            for r in reflections
        ]
        guide = [
            {
                "guidance_id": g.guidance_id,
                "category": g.category,
                "capability": g.capability,
                "action": g.action,
                "resource": g.resource,
                "reason": g.reason[:200],
                "recommendation": g.recommendation[:200],
                "confidence": g.confidence,
                "episode_id": g.episode_id,
                "reflection_id": g.reflection_id,
            }
            for g in guidance
        ]
        # Strategy (ADR-015): the deterministic strategy selected for this goal.
        strategy_block: dict[str, Any] | None = None
        if self.strategy is not None and hasattr(self.strategy, "to_dict"):
            try:
                sd = self.strategy.to_dict()
                strategy_block = {
                    "strategy_id": sd.get("strategy_id"),
                    "name": sd.get("name"),
                    "description": (sd.get("description") or "")[:200],
                    "constraints": sd.get("constraints", {}),
                }
            except Exception:
                strategy_block = None
        # Current world-state facts (bounded, metadata only - values are
        # already structured safe metadata from the environment store).
        env_block = [
            {"key": f.key, "version": f.version, "observed_at": f.observed_at, "source": f.source}
            for f in self.environment[:20]
        ]
        # Previous goal plan versions (immutable history, bounded metadata).
        plan_history_block = [
            {
                "plan_version": p.get("plan_version"),
                "strategy": p.get("strategy"),
                "reason": (p.get("reason") or "")[:100],
                "steps": len(p.get("plan_summary") or []),
            }
            for p in self.plan_history[-3:]
        ]
        # ADVISORY mutation-recovery records (ADR-020): bounded metadata with
        # provenance. Informs planning; NEVER authorizes anything.
        recovery_block = [
            {
                "recovery_id": r.get("recovery_id"),
                "task_id": r.get("task_id"),
                "step_index": r.get("step_index"),
                "capability": r.get("capability"),
                "action": r.get("action"),
                "resource": r.get("resource"),
                "status": r.get("status"),
                "reason": (r.get("reason") or "")[:200],
                "created_at": r.get("created_at"),
            }
            for r in self.recovery[:10]
        ]
        # Provenance: which memories influenced this context (IDs only).
        prov = {
            "episode_ids": [e.episode_id for e in episodes],
            "reflection_ids": [r.reflection_id for r in reflections],
            "guidance_ids": [g.guidance_id for g in guidance],
            "strategy_ids": [self.strategy.strategy_id] if self.strategy is not None else [],
        }
        # Enforce the character budget across the whole digest (truncate).
        total = {
            "episodes": ep,
            "reflections": ref,
            "guidance": guide,
            "strategy": strategy_block,
            "environment": env_block,
            "plan_history": plan_history_block,
            "recovery": recovery_block,
            "provenance": prov,
            "counts": {"episodes": len(ep), "reflections": len(ref), "guidance": len(guide)},
        }
        text = json.dumps(total, separators=(",", ":"))
        if len(text) > budget.max_chars:
            budget_left = budget.max_chars - 80
            # drop guidance first, then reflections, then episodes, until it fits
            while len(json.dumps(total, separators=(",", ":"))) > budget_left and (guide or ref or ep or env_block):
                if env_block:
                    env_block = env_block[: len(env_block) - 1]
                elif guide:
                    guide = guide[: len(guide) - 1]
                elif ref:
                    ref = ref[: len(ref) - 1]
                elif ep:
                    ep = ep[: len(ep) - 1]
                total = {
                    "episodes": ep,
                    "reflections": ref,
                    "guidance": guide,
                    "strategy": strategy_block,
                    "environment": env_block,
                    "plan_history": plan_history_block,
                    "provenance": prov,
                    "counts": {"episodes": len(ep), "reflections": len(ref), "guidance": len(guide)},
                }
            return {
                "episodes": ep,
                "reflections": ref,
                "guidance": guide,
                "strategy": strategy_block,
                "environment": env_block,
                "plan_history": plan_history_block,
                "provenance": prov,
                "counts": {"episodes": len(ep), "reflections": len(ref), "guidance": len(guide)},
                "truncated": True,
            }
        return total

    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        for e in self.episodes:
            seen.update(e.tags)
        return sorted(seen)
