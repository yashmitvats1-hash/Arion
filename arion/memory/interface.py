"""Memory interface (ADR-012).

A stable protocol for memory operations. SQLite is the current implementation
(SQLiteMemoryStore in store.py); future implementations (Postgres, vector/
embedding retrieval, distributed memory, encrypted storage) implement the same
protocol without touching orchestration or intelligence.

Memory is NOT an authorization mechanism. None of these operations can grant
permissions, alter actors, change boundaries, or register capabilities.
"""

from __future__ import annotations

from typing import Protocol

from arion.memory.models import Episode, EpisodeFilter, Reflection


class MemoryStore(Protocol):
    """Persistence contract for episodic memory and reflections."""

    # ---- episodes ----

    def record_episode(self, episode: Episode) -> None: ...
    def get_episode(self, episode_id: str) -> Episode | None: ...
    def search_episodes(self, filters: EpisodeFilter) -> list[Episode]: ...
    def list_recent(self, limit: int = 10) -> list[Episode]: ...

    # ---- reflections ----

    def record_reflection(self, reflection: Reflection) -> None: ...
    def get_reflection(self, reflection_id: str) -> Reflection | None: ...
    def list_recent_reflections(self, limit: int = 10) -> list[Reflection]: ...
    def link_reflection(self, episode_id: str, reflection_id: str) -> None: ...

    # ---- consolidations ----

    def record_consolidation(self, record: Any) -> None: ...
    def list_consolidations(self, limit: int = 50) -> list: ...

    def close(self) -> None: ...
