"""Cognitive State facade (ADR-014).

Combines the five cognitive layers into one structured view:

  episodic    - what happened        (MemoryStore - episodes/reflections)
  semantic    - what Arion believes  (CognitiveStore - beliefs)
  procedural  - how to do things     (reflections + guidance + procedural beliefs)
  preference  - user preferences     (CognitiveStore - preferences)
  environment - current world state  (CognitiveStore - environment facts)

The facade offers refresh_from_memory() (derive + store beliefs from recent
experience, idempotent-ish via dedupe) and snapshot() (bounded, structured).

INFORMATIONAL ONLY: the cognitive state is read as advice by intelligence and
never participates in authorization.
"""

from __future__ import annotations

from arion.cognition.deriver import BeliefDeriver, DeterministicBeliefDeriver
from arion.cognition.models import CognitiveSnapshot
from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.retrieval import MemoryRetriever


class CognitiveState:
    """A bounded facade over episodic memory + cognitive stores."""

    def __init__(self, memory, cognition: SQLiteCognitiveStore, deriver: BeliefDeriver | None = None):
        self.memory = memory
        self.cognition = cognition
        self.deriver = deriver or DeterministicBeliefDeriver()

    def refresh_from_memory(self, limit: int = 20) -> int:
        """Derive beliefs from recent episodes+reflections+guidance and store them.

        Deterministic and deduplicated (a derived belief with the same
        statement+category is not re-added at lower confidence). Returns the
        number of NEW beliefs stored.
        """
        from arion.memory.guidance import DeterministicMemoryGuidance

        episodes = self.memory.list_recent(limit=limit)
        reflections = self.memory.list_recent_reflections(limit=limit)
        ref_by_ep = {r.episode_id: r for r in reflections}
        # pair reflections with their episodes for guidance derivation
        paired_refs = [ref_by_ep[e.episode_id] for e in episodes if e.episode_id in ref_by_ep]
        guidance = DeterministicMemoryGuidance().build(episodes, paired_refs)
        beliefs = self.deriver.derive(episodes, paired_refs, guidance)
        new_count = 0
        for b in beliefs:
            existing = self.cognition.list_beliefs(category=b.category, limit=1000)
            if any(e.statement == b.statement and e.confidence >= b.confidence for e in existing):
                continue
            self.cognition.record_belief(b)
            new_count += 1
        return new_count

    def snapshot(self, limit_beliefs: int = 50) -> CognitiveSnapshot:
        beliefs = self.cognition.list_beliefs(limit=limit_beliefs)
        preferences = self.cognition.list_preferences(limit=limit_beliefs)
        environment = self.cognition.list_environment_facts(limit=limit_beliefs)
        return CognitiveSnapshot(
            beliefs=beliefs,
            preferences=preferences,
            environment=environment,
            counts={
                "beliefs": len(beliefs),
                "preferences": len(preferences),
                "environment": len(environment),
            },
        )

    def retrieve(self, goal: str, top_k: int = 10) -> list:
        """Deterministic relevance search over beliefs (goal-token overlap)."""
        from arion.cognition.models import Belief
        import re

        tokens = set(re.findall(r"[a-z0-9_]+", goal.lower()))
        scored = []
        for b in self.cognition.list_beliefs(limit=500):
            bt = set(re.findall(r"[a-z0-9_]+", b.statement.lower()))
            score = len(tokens & bt) + b.importance
            if score > 0:
                scored.append((score, b))
        scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [b for _, b in scored[:top_k]]
