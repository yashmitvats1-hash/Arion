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
    def get_episode_by_task(self, task_id: str) -> Episode | None: ...
    def set_episode_lifecycle(self, episode_id: str, state: str) -> None: ...
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

    # ---- archival / pruning seam (ADR-014) ----
    # Consolidation PRESERVES history and therefore does not bound physical
    # storage. This is the DESIGNED seam for a future archival/pruning policy
    # (delete/archive episodes older than X, cap episode count, etc.). It is
    # intentionally NOT implemented yet - memory is never deleted in this
    # milestone. Implementations should raise NotImplementedError until the
    # archival policy is designed and approved.

    def prune(self, older_than: str | None = None, max_episodes: int | None = None) -> int: ...

    def close(self) -> None: ...
