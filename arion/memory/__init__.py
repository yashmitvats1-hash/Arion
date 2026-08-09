"""Memory layer: persistent cognitive memory (ADR-012).

Memory = INFORMATIONAL. It never authorizes. Episodes and reflections are
structured; retrieval is deterministic and bounded; SQLite is the current
store behind the MemoryStore protocol (Postgres/vector/encrypted later).
"""

from arion.memory.interface import MemoryStore
from arion.memory.lifecycle import build_episode_from_task, episode_outcome_for
from arion.memory.models import (
    ContextBudget,
    Episode,
    EpisodeFilter,
    PlanningContext,
    Reflection,
)
from arion.memory.reflector import DeterministicReflector, Reflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore

__all__ = [
    "ContextBudget",
    "DeterministicReflector",
    "Episode",
    "EpisodeFilter",
    "MemoryRetriever",
    "MemoryStore",
    "PlanningContext",
    "Reflection",
    "Reflector",
    "SQLiteMemoryStore",
    "build_episode_from_task",
    "build_planning_context",
    "episode_outcome_for",
]
