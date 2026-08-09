"""Deterministic belief derivation (ADR-014).

Turns experience (episodes + reflections + guidance) into structured beliefs
with FULL provenance:

  every derived belief carries:
    - provenance: source episode/reflection/guidance ids
    - confidence (mapped from reflection confidence / importance)
    - timestamps (created_at/updated_at)
    - source marker ("deterministic" | "model")

The deterministic deriver is the reference path (offline-testable); model-
backed derivation would implement the same Belief protocol and stay strictly
validated + advisory.

INFORMATIONAL ONLY: beliefs can never authorize anything.
"""

from __future__ import annotations

from typing import Protocol

from arion.cognition.models import Belief
from arion.memory.guidance import MemoryGuidance
from arion.memory.models import Episode, Reflection
from arion.state.models import new_id, utcnow

_CONFIDENCE_MAP = {"low": 0.4, "medium": 0.7, "high": 0.9}


class BeliefDeriver(Protocol):
    """Derives beliefs from experience."""

    def derive(self, episodes: list[Episode], reflections: list[Reflection], guidance: list[MemoryGuidance]) -> list[Belief]: ...


def _provenance_for(episode: Episode, reflection: Reflection | None, guidance: MemoryGuidance | None) -> dict[str, list[str]]:
    return {
        "episode_ids": [episode.episode_id],
        "reflection_ids": [reflection.reflection_id] if reflection else [],
        "guidance_ids": [guidance.guidance_id] if guidance else [],
    }


def _confidence(reflection: Reflection | None, importance: float) -> float:
    base = _CONFIDENCE_MAP.get(reflection.confidence, 0.5) if reflection else 0.5
    return round(min(1.0, base * (0.5 + 0.5 * importance)), 3)


class DeterministicBeliefDeriver:
    """Rule-based belief derivation: deterministic, offline, explainable.

    Rules:
      - avoid (denied)   -> semantic belief: "X on R is not permitted by current policy"
      - avoid (failed)   -> semantic belief: "X on R fails (category)"
      - prefer           -> semantic belief: "X on R is achievable"
      - reflection lesson-> procedural belief: how-to lesson (bounded)
      - recommendation   -> procedural belief: recommended future behavior
    Deduplicated by (category, statement) keeping the highest confidence.
    """

    def derive(self, episodes: list[Episode], reflections: list[Reflection], guidance: list[MemoryGuidance]) -> list[Belief]:
        ref_by_ep = {r.episode_id: r for r in reflections}
        beliefs: list[Belief] = []
        for g in guidance:
            ep = next((e for e in episodes if e.episode_id == g.episode_id), None)
            if ep is None:
                continue
            ref = ref_by_ep.get(ep.episode_id)
            prov = _provenance_for(ep, ref, g)
            conf = _confidence(ref, g.importance)
            if g.category == "avoid":
                statement = f"{g.capability}/{g.action} on {g.resource!r} is not permitted by current policy"
                beliefs.append(Belief(
                    belief_id=new_id("belief"), category="semantic", statement=statement,
                    confidence=conf, importance=g.importance, provenance=prov, source="deterministic",
                ))
            elif g.category == "prefer":
                statement = f"{g.capability}/{g.action} on {g.resource!r} is achievable"
                beliefs.append(Belief(
                    belief_id=new_id("belief"), category="semantic", statement=statement,
                    confidence=conf, importance=g.importance, provenance=prov, source="deterministic",
                ))
        # procedural beliefs from reflections (bounded statements)
        for ref in reflections:
            prov = {"episode_ids": [ref.episode_id], "reflection_ids": [ref.reflection_id], "guidance_ids": []}
            conf = _CONFIDENCE_MAP.get(ref.confidence, 0.5)
            if ref.lesson:
                beliefs.append(Belief(
                    belief_id=new_id("belief"), category="procedural",
                    statement=ref.lesson[:500], confidence=conf, importance=ref.importance,
                    provenance=prov, source="deterministic",
                ))
        return _dedupe(beliefs)


def _dedupe(beliefs: list[Belief]) -> list[Belief]:
    best: dict[tuple, Belief] = {}
    order: list[tuple] = []
    for b in beliefs:
        key = (b.category, b.statement)
        if key not in best or b.confidence > best[key].confidence:
            best[key] = b
            order.append(key)
    seen: set = set()
    out: list[Belief] = []
    for key in order:
        if key in seen:
            continue
        seen.add(key)
        out.append(best[key])
    out.sort(key=lambda b: (-b.importance, b.created_at))
    return out
