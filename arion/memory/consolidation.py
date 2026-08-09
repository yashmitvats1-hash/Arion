"""Deterministic memory consolidation (learning milestone).

Lightweight, explainable, NO embeddings:

  same goal (token overlap) + same capability + same failure category
  + similar outcome  ->  candidate consolidation

The consolidator NEVER deletes history: it writes explicit
ConsolidationRecord rows (source episode ids, merged lesson, count) and
applies importance decay/boost to episodes. Bounded memory growth is achieved
by consolidating repeated lessons into single records while preserving
provenance.

All logic is deterministic (sorted, token-based) and offline-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from arion.memory.models import Episode
from arion.memory.store import ConsolidationRecord
from arion.state.models import new_id, utcnow

_STOP = {"a", "an", "the", "this", "that", "of", "in", "on", "for", "to", "and", "or",
         "with", "my", "your", "please", "can", "you", "me", "i", "it", "is", "are",
         "read", "list", "inspect", "summarize", "explore", "do", "make", "file", "repository"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower()) if t not in _STOP and len(t) > 1}


def _similar_goal(a: str, b: str, threshold: float = 0.5) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= threshold


def _failure_category(episode: Episode) -> str | None:
    for f in episode.failures:
        if f.get("category"):
            return str(f["category"])
    if episode.authorization.get("denials"):
        return "denied"
    return None


def _episode_key(episode: Episode) -> tuple:
    caps = tuple(sorted({s.get("capability", "") for s in episode.plan_summary}))
    return (episode.outcome, caps, _failure_category(episode))


def decayed_importance(episode: Episode, now: str | None = None, half_life_days: float = 30.0) -> float:
    """Recency/decay: importance decays with age (deterministic)."""
    try:
        created = datetime.fromisoformat(episode.created_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return episode.importance
    try:
        now_dt = datetime.fromisoformat((now or utcnow()).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        now_dt = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now_dt - created).total_seconds() / 86400.0)
    return round(episode.importance * (0.5 ** (age_days / half_life_days)), 3)


def find_consolidation_candidates(episodes: list[Episode], min_similar: int = 2) -> list[list[Episode]]:
    """Group episodes into consolidation candidates (deterministic).

    Groups share: outcome, capability set, failure category, and similar
    goals (>= threshold token overlap). Groups smaller than min_similar are
    ignored.
    """
    groups: list[list[Episode]] = []
    for ep in sorted(episodes, key=lambda e: e.created_at):
        key = _episode_key(ep)
        # find a group with the SAME key whose representative goal is similar
        placed = False
        for group in groups:
            if _episode_key(group[0]) == key and _similar_goal(group[0].goal, ep.goal):
                group.append(ep)
                placed = True
                break
        if not placed:
            groups.append([ep])
    return [g for g in groups if len(g) >= min_similar]


class MemoryConsolidator:
    """Deterministic consolidation over a MemoryStore (explainable)."""

    def __init__(self, store, min_similar: int = 2, threshold: float = 0.5):
        self.store = store
        self.min_similar = min_similar
        self.threshold = threshold

    def consolidate(self, limit: int = 200) -> list[ConsolidationRecord]:
        """Scan recent episodes, write consolidation records, return them.

        Never deletes history; provenance is preserved in each record.
        """
        episodes = self.store.list_recent(limit=limit)
        candidates = find_consolidation_candidates(episodes, min_similar=self.min_similar)
        # idempotency: skip groups already consolidated (same source episode set)
        existing = {frozenset(r.source_episode_ids) for r in self.store.list_consolidations(limit=10000)}
        records: list[ConsolidationRecord] = []
        for group in candidates:
            if frozenset(e.episode_id for e in group) in existing:
                continue
            group_sorted = sorted(group, key=lambda e: e.created_at)
            source_ids = [e.episode_id for e in group_sorted]
            lessons: list[str] = []
            for ep in group_sorted:
                ref = self.store.get_reflection(ep.reflection_id) if ep.reflection_id else None
                lessons.append((ref.lesson if ref else ep.goal)[:200])
            merged = _merge_lessons(lessons)
            record = ConsolidationRecord(
                consolidation_id=new_id("consol"),
                source_episode_ids=source_ids,
                category=_failure_category(group_sorted[0]) or group_sorted[0].outcome,
                merged_lesson=merged,
                count=len(group),
                importance=round(min(1.0, sum(e.importance for e in group_sorted) / len(group_sorted) + 0.1 * (len(group) - 1)), 2),
                created_at=utcnow(),
            )
            self.store.record_consolidation(record)
            records.append(record)
        return records


def _merge_lessons(lessons: list[str]) -> str:
    """Merge repeated lessons into one (dedupe, cap length)."""
    seen: set[str] = set()
    merged: list[str] = []
    for lesson in lessons:
        key = lesson.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(lesson.strip())
    text = " | ".join(merged)
    return text[:800]
