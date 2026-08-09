"""Deterministic memory retrieval + bounded context construction (ADR-012).

Retrieval ranks episodes by structured signals: goal-token overlap, shared
capabilities, outcome, failure category, tags, importance, recency. It is
deterministic and testable - no embeddings or vector DB (deferred).

build_planning_context implements the bounded, deterministic context policy:

    recent relevant episodes + high-importance relevant episodes
    + recent reflections

The result is a PlanningContext object (never a raw concatenation of strings)
whose digest() is what a model receives - relevant memory, not the whole DB.
"""

from __future__ import annotations

import re
from typing import Protocol

from arion.memory.interface import MemoryStore
from arion.memory.models import ContextBudget, Episode, EpisodeFilter, PlanningContext

_STOP = {"a", "an", "the", "this", "that", "of", "in", "on", "for", "to", "and", "or",
         "with", "my", "your", "please", "can", "you", "me", "i", "it", "is", "are",
         "read", "list", "inspect", "summarize", "explore", "do", "make", "file"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower()) if t not in _STOP and len(t) > 1}


class MemoryRetriever:
    """Deterministic relevance ranking over a MemoryStore."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def retrieve(
        self,
        goal: str,
        top_k: int = 5,
        capability: str | None = None,
        outcome: str | None = None,
        failure_category: str | None = None,
        tag: str | None = None,
    ) -> list[Episode]:
        """Return the top-k episodes ranked by relevance (score, then recency)."""
        candidates = self.store.search_episodes(
            EpisodeFilter(outcome=outcome, capability=capability, failure_category=failure_category,
                          tag=tag, limit=max(50, top_k * 4))
        )
        goal_tokens = _tokens(goal)
        scored: list[tuple[float, Episode]] = []
        for episode in candidates:
            # goal-token overlap
            overlap = goal_tokens & _tokens(episode.goal)
            # shared capabilities (episodes carry capability tags)
            cap_tags = {t for t in episode.tags if "." in t}
            # RELEVANCE GATE: an episode must share at least one goal token or
            # capability with the current goal to be retrieved at all. An
            # unrelated episode (even recent or high-importance) is excluded.
            if not overlap and not cap_tags:
                continue
            score = 0.0
            score += 2.0 * len(overlap)
            score += 1.0 * len(cap_tags)
            # outcome salience: failed/denied episodes carry more signal
            if episode.outcome == "failed":
                score += 1.5
            elif episode.outcome == "denied":
                score += 1.0
            elif episode.outcome == "recovered":
                score += 0.5
            # importance
            score += 0.5 * episode.importance
            scored.append((score, episode))
        # deterministic: score desc, then created_at desc (recency breaks ties)
        scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [ep for _, ep in scored[:top_k]]


def build_planning_context(
    retriever: MemoryRetriever,
    goal: str,
    budget: ContextBudget | None = None,
) -> PlanningContext:
    """Bounded, deterministic context: relevant episodes + recent reflections."""
    budget = budget or ContextBudget()

    # relevant episodes, ranked (score desc, recency tie-break) and bounded.
    # Recency is a tie-breaker within relevance - an irrelevant episode is
    # never included just because it is recent.
    episodes = retriever.retrieve(goal, top_k=budget.max_episodes)

    # recent reflections (prefer linked to selected episodes, then recent)
    reflections: list = []
    seen_ref: set[str] = set()
    for ep in episodes:
        if ep.reflection_id and ep.reflection_id not in seen_ref:
            ref = retriever.store.get_reflection(ep.reflection_id)
            if ref is not None:
                reflections.append(ref)
                seen_ref.add(ref.reflection_id)
    for ref in retriever.store.list_recent_reflections(limit=budget.max_reflections * 3):
        if ref.reflection_id not in seen_ref:
            reflections.append(ref)
            seen_ref.add(ref.reflection_id)
            if len(reflections) >= budget.max_reflections:
                break
    reflections = sorted(reflections, key=lambda r: r.created_at, reverse=True)[: budget.max_reflections]

    return PlanningContext(goal=goal, episodes=episodes, reflections=reflections, budget=budget)
