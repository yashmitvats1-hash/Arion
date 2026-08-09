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

    def derive_and_store(self, episodes: list, reflections: list, guidance: list) -> int:
        """Derive beliefs and store them (append-only + versioned).

        A revision with a higher confidence for the same (category, statement)
        supersedes the prior belief (history preserved). Returns the number of
        NEW beliefs stored.
        """
        beliefs = self.deriver.derive(episodes, reflections, guidance)
        new_count = 0
        for b in beliefs:
            existing = self.cognition.list_beliefs(category=b.category, limit=1000)
            match = [e for e in existing if e.statement == b.statement]
            if match:
                best = max(match, key=lambda e: e.confidence)
                if best.confidence >= b.confidence:
                    continue  # already known at >= confidence
                # higher-confidence revision: store new version, supersede old
                b.version = max(e.version for e in match) + 1
                self.cognition.record_belief(b)
                for e in match:
                    if e.superseded_at is None:
                        self.cognition.supersede_belief(e.belief_id)
                new_count += 1
                continue
            self.cognition.record_belief(b)
            new_count += 1
        return new_count

    def refresh_from_memory(self, limit: int = 20) -> int:
        """Derive beliefs from recent episodes+reflections+guidance and store them.

        Deterministic, deduplicated, and versioned (a revised belief with
        higher confidence supersedes the prior one). Returns the number of NEW
        beliefs stored.
        """
        from arion.memory.guidance import DeterministicMemoryGuidance

        episodes = self.memory.list_recent(limit=limit)
        reflections = self.memory.list_recent_reflections(limit=limit)
        ref_by_ep = {r.episode_id: r for r in reflections}
        # pair reflections with their episodes for guidance derivation
        paired_refs = [ref_by_ep[e.episode_id] for e in episodes if e.episode_id in ref_by_ep]
        guidance = DeterministicMemoryGuidance().build(episodes, paired_refs)
        return self.derive_and_store(episodes, paired_refs, guidance)

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
