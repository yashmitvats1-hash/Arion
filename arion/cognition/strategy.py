"""Strategy selection (ADR-015).

After the world state and beliefs, the spine selects a STRATEGY for a goal:

  World State -> Beliefs -> Goals -> Long-Horizon Planning -> STRATEGY
  SELECTION -> Authorization -> ...

StrategySelector is deterministic and explainable: it maps the goal + current
beliefs + environment facts + memory guidance to a Strategy (name, constraints,
provenance). The strategy INFLUENCES what the planner proposes - it can never
authorize anything. Authorization still decides at execution time.

Rules (first match wins):
  - environment lacks a capability the goal needs        -> "blocked_missing_capability"
  - procedural/semantic beliefs say goal is unachievable -> "defer_retry"
  - guidance contains avoid entries                      -> "avoid_known_failures"
  - semantic belief says approach is achievable          -> "capability_verified"
  - otherwise                                            -> "direct"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arion.memory.guidance import MemoryGuidance
from arion.state.models import new_id, utcnow


@dataclass
class Strategy:
    """A deterministic strategy selected for a goal."""

    strategy_id: str
    name: str
    description: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, list[str]] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "description": self.description,
            "constraints": self.constraints,
            "provenance": self.provenance,
            "created_at": self.created_at,
        }


class StrategySelector:
    """Deterministic, explainable strategy selection."""

    def select(
        self,
        goal_description: str,
        beliefs: list,
        environment: dict[str, Any] | list,
        guidance: list[MemoryGuidance] | None = None,
        previous_strategies: list[str] | None = None,
    ) -> Strategy:
        guidance = guidance or []
        previous_strategies = previous_strategies or []
        provenance: dict[str, list[str]] = {"belief_ids": [], "episode_ids": [], "guidance_ids": []}
        if previous_strategies:
            provenance["previous_strategies"] = previous_strategies[-10:]

        # Rule 1: environment missing a capability mentioned in the goal
        env = environment if isinstance(environment, dict) else {}
        reg = env.get("registered_capabilities", {})
        caps = reg.get("value", []) if isinstance(reg, dict) else []
        if caps:
            needed = {w for w in goal_description.lower().split() if "." in w}
            missing = [c for c in needed if c not in caps]
            if missing:
                return Strategy(
                    strategy_id=new_id("strat"),
                    name="blocked_missing_capability",
                    description=f"goal needs capabilities {missing} not present in the current world state",
                    constraints={"missing_capabilities": missing},
                    provenance={"belief_ids": [], "episode_ids": [], "guidance_ids": []},
                )

        # Rule 2: beliefs say the goal is unachievable
        for b in beliefs:
            stmt = b.statement.lower()
            if b.category in ("semantic", "procedural") and ("not permitted" in stmt or "failed" in stmt) and \
               _goal_related(goal_description, b.statement):
                provenance["belief_ids"].append(b.belief_id)
                return Strategy(
                    strategy_id=new_id("strat"),
                    name="defer_retry",
                    description="beliefs indicate the goal is currently blocked; defer and retry later",
                    constraints={"blocking_belief": b.belief_id},
                    provenance=provenance,
                )

        # Rule 3: memory guidance says avoid specific targets
        avoids = [g for g in guidance if g.category == "avoid" and g.capability]
        if avoids:
            for g in avoids:
                provenance["guidance_ids"].append(g.guidance_id)
                if g.episode_id:
                    provenance["episode_ids"].append(g.episode_id)
            # Strategy escalation (ADR-016): if we already attempted
            # avoid_known_failures and the goal still has failures, escalate to
            # defer_retry rather than blindly repeating the same strategy.
            if "avoid_known_failures" in previous_strategies:
                return Strategy(
                    strategy_id=new_id("strat"),
                    name="defer_retry",
                    description=(
                        "avoid_known_failures was already attempted for this goal; "
                        "deferring to avoid repeating the same failing strategy"
                    ),
                    constraints={"avoid": [{"capability": g.capability, "action": g.action, "resource": g.resource}
                                           for g in avoids[:10]]},
                    provenance=provenance,
                )
            return Strategy(
                strategy_id=new_id("strat"),
                name="avoid_known_failures",
                description=f"memory shows {len(avoids)} known-failing target(s); plan around them",
                constraints={"avoid": [{"capability": g.capability, "action": g.action, "resource": g.resource}
                                       for g in avoids[:10]]},
                provenance=provenance,
            )

        # Rule 4: a semantic belief says the approach is achievable
        for b in beliefs:
            if b.category == "semantic" and "achievable" in b.statement.lower() and \
               _goal_related(goal_description, b.statement):
                provenance["belief_ids"].append(b.belief_id)
                return Strategy(
                    strategy_id=new_id("strat"),
                    name="capability_verified",
                    description="a prior belief confirms the approach is achievable",
                    constraints={},
                    provenance=provenance,
                )

        return Strategy(
            strategy_id=new_id("strat"),
            name="direct",
            description="no strong signal; plan directly",
            constraints={},
            provenance=provenance,
        )


def _goal_related(goal: str, statement: str) -> bool:
    """Cheap token overlap: is the belief statement about this goal?"""
    goal_tokens = set(goal.lower().split())
    stmt_tokens = set(statement.lower().split())
    return bool(goal_tokens & stmt_tokens)
