"""Deterministic consolidation tests (learning milestone).

Duplicate/similar episode detection, merging repeated lessons, importance
decay, bounded growth. Consolidation NEVER deletes history - it writes
explicit records with provenance.
"""

import pytest

from arion.memory.consolidation import (
    MemoryConsolidator,
    decayed_importance,
    find_consolidation_candidates,
)
from arion.memory.models import Episode, Reflection
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.state.models import utcnow


def _episode(episode_id, goal, outcome="failed", category="execution", task_id=None, days_old=0):
    return Episode(
        episode_id=episode_id,
        task_id=task_id or f"task_{episode_id}",
        goal_id="g",
        goal=goal,
        plan_summary=[{"index": 0, "intent": "read", "capability": "filesystem.read",
                       "action": "read", "status": "failed", "params_keys": ["path"]}],
        actions=[],
        resources=[{"step": 0, "capability": "filesystem.read", "action": "read",
                    "resource": "missing.txt", "status": "failed"}],
        outcome=outcome,
        verification={},
        failures=[{"step": 0, "error": "not a file: 'missing.txt'", "category": category}],
        authorization={},
        recovery={},
        tags=["filesystem.read", f"outcome:{outcome}", f"category:{category}"],
        importance=0.6,
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def test_duplicate_episodes_detected():
    eps = [
        _episode("e1", "read the missing file"),
        _episode("e2", "read the missing file"),
        _episode("e3", "order pizza"),
    ]
    candidates = find_consolidation_candidates(eps, min_similar=2)
    assert len(candidates) == 1
    assert {e.episode_id for e in candidates[0]} == {"e1", "e2"}


def test_different_failure_categories_not_merged():
    eps = [
        _episode("e1", "read the file", category="execution"),
        _episode("e2", "read the file", category="schema_validation"),
    ]
    assert find_consolidation_candidates(eps, min_similar=2) == []


def test_consolidator_writes_explicit_records_keeps_history(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "c.db")
    for i in range(3):
        store.record_episode(_episode(f"e{i}", "read the missing file"))
        ref = DeterministicReflector().reflect(store.get_episode(f"e{i}"))
        store.record_reflection(ref)
        store.link_reflection(f"e{i}", ref.reflection_id)

    records = MemoryConsolidator(store).consolidate(limit=100)
    assert len(records) == 1
    rec = records[0]
    assert rec.count == 3
    assert sorted(rec.source_episode_ids) == ["e0", "e1", "e2"]
    assert rec.category == "execution"
    # history is NOT deleted
    assert len(store.list_recent(limit=10)) == 3
    # explicit record is retrievable
    stored = store.list_consolidations(limit=10)
    assert len(stored) == 1 and stored[0].consolidation_id == rec.consolidation_id
    store.close()


def test_consolidation_is_idempotent(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "i.db")
    store.record_episode(_episode("e1", "read the missing file"))
    store.record_episode(_episode("e2", "read the missing file"))
    MemoryConsolidator(store).consolidate(limit=100)
    MemoryConsolidator(store).consolidate(limit=100)
    # same group consolidated once (no duplicate consolidation records)
    assert len(store.list_consolidations(limit=10)) == 1
    store.close()


def test_importance_decay_over_time():
    old = _episode("e1", "read the file")
    fresh = _episode("e2", "read the file")
    old.created_at = "2020-01-01T00:00:00+00:00"
    fresh.created_at = utcnow()
    decayed = decayed_importance(old, now=fresh.created_at, half_life_days=30)
    assert decayed < fresh.importance
    assert decayed < old.importance
    assert 0.0 <= decayed <= 1.0


def test_merged_lessons_deduplicated(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    for i in range(4):
        store.record_episode(_episode(f"e{i}", "read the missing file"))
        ref = DeterministicReflector().reflect(store.get_episode(f"e{i}"))
        store.record_reflection(ref)
        store.link_reflection(f"e{i}", ref.reflection_id)
    records = MemoryConsolidator(store).consolidate(limit=100)
    assert records
    merged = records[0].merged_lesson
    # identical lessons are deduplicated - the lesson appears ONCE, not 4x
    assert merged.lower().count("failed and may need") == 1
    assert len(merged) <= 800
    store.close()


def test_merged_lessons_join_distinct_lessons(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m2.db")
    for i in range(2):
        store.record_episode(_episode(f"e{i}", "read the missing file"))
        ref = DeterministicReflector().reflect(store.get_episode(f"e{i}"))
        store.record_reflection(ref)
        store.link_reflection(f"e{i}", ref.reflection_id)
    # tamper one reflection to a distinct lesson
    ref = store.list_recent_reflections(limit=10)[1]
    store.record_reflection(Reflection(
        reflection_id=ref.reflection_id, episode_id=ref.episode_id,
        what_happened=ref.what_happened, what_worked=ref.what_worked,
        what_failed=ref.what_failed, why=ref.why, lesson="a different lesson",
        recommendation=ref.recommendation, confidence="medium",
        importance=0.6, created_at=ref.created_at,
    ))
    records = MemoryConsolidator(store).consolidate(limit=100)
    assert records and "|" in records[0].merged_lesson  # distinct lessons joined
    store.close()
