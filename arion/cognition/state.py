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
from arion.cognition.models import Belief, CognitiveSnapshot
from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.retrieval import MemoryRetriever


class CognitiveState:
    """A bounded facade over episodic memory + cognitive stores."""

    def __init__(self, memory, cognition: SQLiteCognitiveStore, deriver: BeliefDeriver | None = None):
        self.memory = memory
        self.cognition = cognition
        self.deriver = deriver or DeterministicBeliefDeriver()

    def derive_and_store(self, episodes: list, reflections: list, guidance: list) -> int:
        """Derive beliefs and store them under the durable invariant.

        A revision with a STRICTLY higher confidence for the same
        ``(category, statement)`` atomically supersedes the prior active
        belief (history preserved, version bumped); equal/lower confidence
        observations adopt the canonical active revision without writing.
        Returns the number of NEW belief revisions stored (the count of
        durable changes - concurrent losers and idempotent observations
        are not counted).
        """
        beliefs = self.deriver.derive(episodes, reflections, guidance)
        new_count = 0
        for b in beliefs:
            if self._persist_belief(b):
                new_count += 1
        return new_count

    def persist_belief(self, b: Belief) -> bool:
        """Authoritative belief persistence (see ``SQLiteCognitiveStore.persist_belief``).

        Routes through the storage-layer transactional claim so the facade
        and the engine cannot diverge. Returns True only when this call
        inserted a NEW revision (a durable change); False when the
        observation was adopted by an equal/higher canonical active belief.
        """
        result = self.cognition.persist_belief(b)
        canonical = result.belief
        # Reflect the canonical row's authoritative identity/version back
        # onto the caller's object (id/version/timestamps are decided
        # durably inside the transaction; a concurrent higher-confidence
        # writer may have superseded our insert).
        b.belief_id = canonical.belief_id
        b.version = canonical.version
        b.superseded_at = canonical.superseded_at
        b.confidence = canonical.confidence
        b.importance = canonical.importance
        b.provenance = canonical.provenance
        b.source = canonical.source
        b.created_at = canonical.created_at
        b.updated_at = canonical.updated_at
        return result.created

    # Backwards-compatible alias for the previous private seam.
    _persist_belief = persist_belief

    def refresh_from_memory(self, limit: int = 20,
                            include_consolidations: bool = False) -> int:
        """Derive beliefs from recent episodes+reflections+guidance and store them.

        Deterministic, deduplicated, and versioned (a revised belief with
        higher confidence supersedes the prior one). Returns the number of NEW
        beliefs stored.

        With include_consolidations=True, merged consolidation lessons are
        additionally lifted into procedural beliefs with complete provenance
        (source episode ids + consolidation id). Default False preserves the
        pre-ADR-014-addendum behavior exactly.
        """
        from arion.memory.guidance import DeterministicMemoryGuidance

        episodes = self.memory.list_recent(limit=limit)
        reflections = self.memory.list_recent_reflections(limit=limit)
        ref_by_ep = {r.episode_id: r for r in reflections}
        # pair reflections with their episodes for guidance derivation
        paired_refs = [ref_by_ep[e.episode_id] for e in episodes if e.episode_id in ref_by_ep]
        guidance = DeterministicMemoryGuidance().build(episodes, paired_refs)
        new_count = self.derive_and_store(episodes, paired_refs, guidance)
        if include_consolidations:
            consolidations = self.memory.list_consolidations(limit=max(1, limit))
            for b in self._consolidation_beliefs(consolidations):
                if self._persist_belief(b):
                    new_count += 1
        return new_count

    def _consolidation_beliefs(self, consolidations: list) -> list[Belief]:
        """Lift merged consolidation lessons into procedural beliefs.

        Complete provenance: source episode ids + the consolidation id.
        Confidence is deterministic from the consolidation importance:
        round(min(1.0, 0.5 + 0.5 * importance), 3). Lessons are bounded to
        500 chars (same bound as the reflection path). Consolidations without
        a lesson are skipped. Informational only.
        """
        from arion.state.models import new_id

        out: list[Belief] = []
        for c in consolidations:
            statement = (c.merged_lesson or "").strip()
            if not statement:
                continue
            confidence = round(min(1.0, 0.5 + 0.5 * float(c.importance)), 3)
            out.append(Belief(
                belief_id=new_id("belief"),
                category="procedural",
                statement=statement[:500],
                confidence=confidence,
                importance=float(c.importance),
                provenance={
                    "episode_ids": list(c.source_episode_ids),
                    "reflection_ids": [],
                    "guidance_ids": [],
                    "consolidation_ids": [c.consolidation_id],
                },
                source="deterministic",
                created_at=c.created_at,
                updated_at=c.created_at,
            ))
        return out

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
