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

import re

from arion.memory.guidance import MemoryGuidance
from arion.state.models import new_id, utcnow

# Goal-context similarity (ADR-015 addendum Phase B): same token-overlap
# formula as consolidation's _similar_goal (overlap / min-length >= 0.5),
# but with a MINIMAL function-word stoplist - the consolidation stopwords
# include goal verbs (inspect/repository/summarize/read...), which would
# make strategy-context matching inert for typical goal descriptions.
_CTX_STOP = {"a", "an", "the", "this", "that", "of", "in", "on", "for",
             "to", "and", "or", "with", "my", "your", "please", "can",
             "you", "me", "i", "it", "is", "are"}


def _goal_context_similar(a: str, b: str, threshold: float = 0.5) -> bool:
    """Deterministic token-overlap similarity of two goal descriptions."""
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower())
                if t not in _CTX_STOP and len(t) > 1}

    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= threshold

# The deterministic strategy vocabulary (ADR-015). Outcome rows and CLI
# surfaces validate against these names (fail closed on anything else).
STRATEGY_NAMES = (
    "direct",
    "avoid_known_failures",
    "defer_retry",
    "capability_verified",
    "blocked_missing_capability",
)

# The durable strategy-outcome lifecycle (ADR-015 addendum, Phase A):
# a plan version is superseded by the next version, or ends as succeeded /
# failed with its goal. Exactly one outcome row per (goal_id, plan_version).
STRATEGY_OUTCOME_STATES = ("superseded", "succeeded", "failed")

# Bounded outcome history considered by the post-rule preference layer:
# the first N rows of the store's deterministic (goal_id, plan_version)
# listing. No timestamps, no wall clock (ADR-015 addendum Phase B).
_OUTCOME_HISTORY_MAX = 20


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


def _validate_outcome_row(row: Any, index: int) -> None:
    """Fail-closed validation of one outcome_history row (Phase B)."""
    if not isinstance(row, dict):
        raise ValueError(
            f"outcome_history[{index}] must be a dict, got {type(row).__name__} "
            f"(fail closed)")
    for key in ("outcome_id", "goal_id", "goal_description", "strategy",
                "plan_version", "outcome", "reason", "episode_id", "created_at"):
        if key not in row:
            raise ValueError(
                f"outcome_history[{index}] is missing key {key!r} (fail closed)")
    if not isinstance(row["outcome_id"], str) or not row["outcome_id"]:
        raise ValueError(
            f"outcome_history[{index}].outcome_id must be a non-empty string "
            f"(fail closed)")
    if not isinstance(row["goal_id"], str) or not row["goal_id"]:
        raise ValueError(
            f"outcome_history[{index}].goal_id must be a non-empty string "
            f"(fail closed)")
    if not isinstance(row["goal_description"], str):
        raise ValueError(
            f"outcome_history[{index}].goal_description must be a string "
            f"(fail closed)")
    if len(row["goal_description"]) > 300:
        # Matches the store's write bound (Phase A); a raw-SQL forged
        # oversized description must fail closed instead of matching many
        # goal contexts (ADR-015 addendum Phase E - preference poisoning).
        raise ValueError(
            f"outcome_history[{index}].goal_description exceeds 300 chars "
            f"(fail closed)")
    if row["strategy"] not in STRATEGY_NAMES:
        raise ValueError(
            f"outcome_history[{index}].strategy must be one of {STRATEGY_NAMES}, "
            f"got {row['strategy']!r} (fail closed)")
    if (isinstance(row["plan_version"], bool)
            or not isinstance(row["plan_version"], int)
            or row["plan_version"] < 1):
        raise ValueError(
            f"outcome_history[{index}].plan_version must be a positive integer "
            f"(fail closed)")
    if row["outcome"] not in STRATEGY_OUTCOME_STATES:
        raise ValueError(
            f"outcome_history[{index}].outcome must be one of "
            f"{STRATEGY_OUTCOME_STATES}, got {row['outcome']!r} (fail closed)")
    if not isinstance(row["reason"], str):
        raise ValueError(
            f"outcome_history[{index}].reason must be a string (fail closed)")
    if len(row["reason"]) > 200:
        # Matches the store's write bound (Phase A); oversized reasons fail
        # closed (ADR-015 addendum Phase E).
        raise ValueError(
            f"outcome_history[{index}].reason exceeds 200 chars (fail closed)")
    if row["episode_id"] is not None and (not isinstance(row["episode_id"], str)
                                          or not row["episode_id"]):
        raise ValueError(
            f"outcome_history[{index}].episode_id must be a non-empty string "
            f"or None (fail closed)")
    if not isinstance(row["created_at"], str):
        raise ValueError(
            f"outcome_history[{index}].created_at must be a string (fail closed)")


class StrategySelector:
    """Deterministic, explainable strategy selection."""

    def select(
        self,
        goal_description: str,
        beliefs: list,
        environment: dict[str, Any] | list,
        guidance: list[MemoryGuidance] | None = None,
        previous_strategies: list[str] | None = None,
        outcome_history: list[dict[str, Any]] | None = None,
    ) -> Strategy:
        guidance = guidance or []
        previous_strategies = previous_strategies or []
        provenance: dict[str, list[str]] = {"belief_ids": [], "episode_ids": [], "guidance_ids": []}
        if previous_strategies:
            provenance["previous_strategies"] = previous_strategies[-10:]

        # Rule 1: environment missing a capability mentioned in the goal.
        # Dotted tokens containing '/' are file paths, not capability names
        # (e.g. "docs/design.md") - never treated as capabilities (ADR-017).
        env = environment if isinstance(environment, dict) else {}
        reg = env.get("registered_capabilities", {})
        caps = reg.get("value", []) if isinstance(reg, dict) else []
        if caps:
            needed = {w for w in goal_description.lower().split() if "." in w and "/" not in w}
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

        # Rule 5 (base): direct. BEFORE returning, the POST-RULE PREFERENCE
        # LAYER (ADR-015 addendum Phase B) may upgrade the selection using
        # durable outcome history. It only ever runs when the base rules
        # would select `direct` (it never overrides a non-direct base).
        if outcome_history is not None:
            for i, row in enumerate(outcome_history):
                _validate_outcome_row(row, i)
            pref = self._prefer_from_history(
                goal_description, guidance, outcome_history, provenance)
            if pref is not None:
                return Strategy(
                    strategy_id=new_id("strat"),
                    name=pref["name"],
                    description=pref["description"],
                    constraints=pref["constraints"],
                    provenance=provenance,
                )
        return Strategy(
            strategy_id=new_id("strat"),
            name="direct",
            description="no strong signal; plan directly",
            constraints={},
            provenance=provenance,
        )

    def _prefer_from_history(self, goal_description: str,
                             guidance: list[MemoryGuidance],
                             outcome_history: list[dict[str, Any]],
                             provenance: dict[str, list[str]]
                             ) -> dict[str, Any] | None:
        """Post-rule preference layer (ADR-015 addendum Phase B).

        Runs ONLY when the base rules would select `direct` (it never
        overrides a non-direct base result). Deterministic, bounded (first
        _OUTCOME_HISTORY_MAX rows of the store's (goal_id, plan_version)
        listing), no wall clock:

        1. SUCCESS preference: a NON-direct strategy that `succeeded` for a
           similar goal context may be preferred. Best = most successes,
           tie-break by strategy name ascending. Direct successes are
           no-ops (the base is already direct).
        2. AVOIDANCE/escalation: when `direct` has >= 2 non-success rows
           (failed OR superseded) for a similar goal context, escalate to
           `defer_retry`. Avoid guidance is NOT consulted here: base==direct
           implies rule 3 found no avoids, so the layer escalates to
           defer_retry only.

        Success evidence beats failure evidence (rule 1 wins over rule 2).
        Insufficient or dissimilar history fabricates nothing (returns
        None). Provenance (outcome_ids of the evidence rows) is filled
        into `provenance` - informational only.
        """
        history = outcome_history[:_OUTCOME_HISTORY_MAX]
        similar = [r for r in history
                   if _goal_context_similar(goal_description,
                                            r["goal_description"])]
        # 1) success preference
        successes: dict[str, list[str]] = {}
        for r in similar:
            if r["outcome"] == "succeeded" and r["strategy"] != "direct":
                successes.setdefault(r["strategy"], []).append(r["outcome_id"])
        if successes:
            best = min(successes, key=lambda s: (-len(successes[s]), s))
            provenance["outcome_ids"] = successes[best]
            return {
                "name": best,
                "description": (
                    f"historical outcomes show {best} succeeded for similar "
                    f"goals ({len(successes[best])} success(es))"),
                "constraints": {},
            }
        # 2) avoidance / escalation for a repeatedly non-successful `direct`
        non_success = [r for r in similar
                       if r["strategy"] == "direct"
                       and r["outcome"] in ("failed", "superseded")]
        if len(non_success) >= 2:
            provenance["outcome_ids"] = [r["outcome_id"] for r in non_success]
            return {
                "name": "defer_retry",
                "description": (
                    "direct failed or was superseded repeatedly for similar "
                    "goals; deferring instead of repeating"),
                "constraints": {},
            }
        return None


def _goal_related(goal: str, statement: str) -> bool:
    """Cheap token overlap: is the belief statement about this goal?"""
    goal_tokens = set(goal.lower().split())
    stmt_tokens = set(statement.lower().split())
    return bool(goal_tokens & stmt_tokens)
