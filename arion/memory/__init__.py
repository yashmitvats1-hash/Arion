"""Memory layer: persistent cognitive memory (ADR-012) + learning loop.

Memory = INFORMATIONAL. It never authorizes. Episodes and reflections are
structured; retrieval is deterministic and bounded; guidance turns prior
experience into planning recommendations; consolidation merges repeated
lessons without deleting history.
"""

from arion.memory.consolidation import (
    MemoryConsolidator,
    decayed_importance,
    find_consolidation_candidates,
)
from arion.memory.guidance import (
    DeterministicMemoryGuidance,
    MemoryGuidance,
    apply_guidance_to_steps,
    build_guidance_for_episode,
)
from arion.memory.interface import MemoryStore
from arion.memory.lifecycle import build_episode_from_task, episode_outcome_for
from arion.memory.model_reflector import ModelReflector
from arion.memory.models import (
    ContextBudget,
    Episode,
    EpisodeFilter,
    PlanningContext,
    Reflection,
)
from arion.memory.reflection_schema import (
    ReflectionValidationError,
    reflection_from_json,
    validate_reflection_dict,
)
from arion.memory.reflector import DeterministicReflector, Reflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import ConsolidationRecord, SQLiteMemoryStore

__all__ = [
    "ConsolidationRecord",
    "ContextBudget",
    "DeterministicMemoryGuidance",
    "DeterministicReflector",
    "Episode",
    "EpisodeFilter",
    "MemoryConsolidator",
    "MemoryGuidance",
    "MemoryRetriever",
    "MemoryStore",
    "ModelReflector",
    "PlanningContext",
    "Reflection",
    "ReflectionValidationError",
    "Reflector",
    "SQLiteMemoryStore",
    "apply_guidance_to_steps",
    "build_episode_from_task",
    "build_guidance_for_episode",
    "build_planning_context",
    "decayed_importance",
    "episode_outcome_for",
    "find_consolidation_candidates",
    "reflection_from_json",
    "validate_reflection_dict",
]
