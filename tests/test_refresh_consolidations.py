"""Consolidation-fed beliefs (ADR-014 addendum, Phase E) - tests first.

`refresh_from_memory(include_consolidations=True)` lifts merged consolidation
lessons into procedural beliefs with COMPLETE provenance (source episodes +
consolidation id). Requirements:

- flag=false (default) preserves the existing behavior exactly;
- consolidation-derived beliefs are procedural, deterministic, and carry
  provenance {episode_ids: [sources], consolidation_ids: [id]};
- idempotent (a second identical refresh stores nothing);
- deterministic supersession (higher-confidence revision supersedes, lower
  or equal is skipped - identical versioning rule to derive_and_store);
- consolidations without a lesson are skipped;
- informational only: never touches preferences/environment/goal_plans.

All timestamps fixed; all assertions deterministic.
"""

from __future__ import annotations

from arion.cognition.models import Belief
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.models import Episode, Reflection
from arion.memory.store import ConsolidationRecord, SQLiteMemoryStore

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _seed(db, lesson="merged lesson: verify before mutating",
          importance=0.5, n_episodes=2, with_reflection=True):
    memory = SQLiteMemoryStore(db)
    for i in range(n_episodes):
        memory.record_episode(Episode(
            episode_id=f"ep-{i}", task_id=f"t-{i}", goal_id="g", goal=f"goal {i}",
            outcome="completed" if i == 0 else "failed", importance=0.4 + 0.1 * i,
            created_at=_iso_plus(T0, i * 10), updated_at=_iso_plus(T0, i * 10),
            reflection_id=f"refl-{i}" if (with_reflection and i == 0) else None,
        ))
    if with_reflection:
        memory.record_reflection(Reflection(
            reflection_id="refl-0", episode_id="ep-0",
            what_happened="x", what_worked="", what_failed="", why="",
            lesson="reflection lesson", recommendation="", confidence="medium",
            importance=0.5, created_at=_iso_plus(T0, 0)))
    memory.record_consolidation(ConsolidationRecord(
        consolidation_id="consol-1",
        source_episode_ids=[f"ep-{i}" for i in range(n_episodes)],
        category="lesson", merged_lesson=lesson, count=n_episodes,
        importance=importance, created_at=_iso_plus(T0, 100)))
    memory.close()
    return SQLiteCognitiveStore(db)


def _facade(db):
    return CognitiveState(
        memory=SQLiteMemoryStore(db),
        cognition=SQLiteCognitiveStore(db),
    )


def test_flag_false_preserves_existing_behavior(tmp_path):
    db = str(tmp_path / "e.db")
    _seed(db)
    facade = _facade(db)

    base = facade.refresh_from_memory(limit=20)              # flag default False
    beliefs_false = [b.to_dict() for b in
                     facade.cognition.list_beliefs(limit=1000)]

    # a second call with the flag still False must add nothing new
    again = facade.refresh_from_memory(limit=20)
    assert again == 0
    assert [b.to_dict() for b in
            facade.cognition.list_beliefs(limit=1000)] == beliefs_false

    # the consolidation lesson must NOT appear with the flag off
    assert not any("merged lesson" in b["statement"] for b in beliefs_false)
    assert base >= 0
    facade.cognition.close()


def test_include_consolidations_lifts_lessons(tmp_path):
    db = str(tmp_path / "e2.db")
    _seed(db)
    facade = _facade(db)
    facade.refresh_from_memory(limit=20, include_consolidations=True)

    beliefs = facade.cognition.list_beliefs(limit=1000)
    lifted = [b for b in beliefs
              if "merged lesson" in b.statement]
    assert len(lifted) == 1
    b = lifted[0]
    assert b.category == "procedural"
    assert b.statement == "merged lesson: verify before mutating"
    assert b.source == "deterministic"
    assert b.importance == 0.5
    assert b.confidence == 0.75          # round(min(1, 0.5 + 0.5*0.5), 3)
    facade.cognition.close()


def test_complete_provenance(tmp_path):
    db = str(tmp_path / "e3.db")
    _seed(db, n_episodes=3)
    facade = _facade(db)
    facade.refresh_from_memory(limit=20, include_consolidations=True)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "merged lesson" in b.statement][0]
    assert lifted.provenance["episode_ids"] == ["ep-0", "ep-1", "ep-2"]
    assert lifted.provenance["consolidation_ids"] == ["consol-1"]
    assert lifted.provenance["reflection_ids"] == []
    assert lifted.provenance["guidance_ids"] == []
    facade.cognition.close()


def test_idempotent(tmp_path):
    db = str(tmp_path / "e4.db")
    _seed(db)
    facade = _facade(db)
    first = facade.refresh_from_memory(limit=20, include_consolidations=True)
    second = facade.refresh_from_memory(limit=20, include_consolidations=True)
    assert second == 0
    assert facade.cognition.count_beliefs() == first
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "merged lesson" in b.statement]
    assert len(lifted) == 1                # no duplicates
    facade.cognition.close()


def test_deterministic_supersession(tmp_path):
    db = str(tmp_path / "e5.db")
    _seed(db, lesson="verify before mutating")
    facade = _facade(db)
    # pre-existing procedural belief, same statement, lower confidence
    facade.cognition.record_belief(Belief(
        belief_id="old-proc", category="procedural",
        statement="verify before mutating", confidence=0.4, importance=0.3,
        provenance={"episode_ids": ["ep-x"]}, source="deterministic",
        created_at=_iso_plus(T0, 1), updated_at=_iso_plus(T0, 1)))

    new_count = facade.refresh_from_memory(limit=20, include_consolidations=True)
    assert new_count >= 1
    old = facade.cognition.get_belief("old-proc")
    assert old is not None and old.superseded_at is not None  # superseded
    active = facade.cognition.list_beliefs(category="procedural", limit=100)
    newer = [b for b in active if b.statement == "verify before mutating"]
    assert len(newer) == 1
    assert newer[0].confidence == 0.75      # consolidation confidence wins
    assert newer[0].version == 2            # versioned append, history kept

    # re-run: the revision is now >= confidence -> nothing new (idempotent)
    assert facade.refresh_from_memory(limit=20, include_consolidations=True) == 0
    facade.cognition.close()


def test_equal_or_higher_confidence_skips(tmp_path):
    db = str(tmp_path / "e6.db")
    _seed(db, lesson="verify before mutating")
    facade = _facade(db)
    facade.cognition.record_belief(Belief(
        belief_id="strong", category="procedural",
        statement="verify before mutating", confidence=0.9, importance=0.8,
        provenance={"episode_ids": ["ep-y"]}, source="deterministic",
        created_at=_iso_plus(T0, 1), updated_at=_iso_plus(T0, 1)))
    facade.refresh_from_memory(limit=20, include_consolidations=True)
    # the consolidation lesson must NOT store: existing >= its confidence
    assert facade.cognition.get_belief("strong").superseded_at is None
    matches = [b for b in facade.cognition.list_beliefs(limit=1000)
               if b.statement == "verify before mutating"]
    assert len(matches) == 1 and matches[0].belief_id == "strong"
    facade.cognition.close()


def test_consolidation_without_lesson_skipped(tmp_path):
    db = str(tmp_path / "e7.db")
    _seed(db, lesson="   ")
    facade = _facade(db)
    facade.refresh_from_memory(limit=20, include_consolidations=True)
    assert not any("merged lesson" in b.statement
                   for b in facade.cognition.list_beliefs(limit=1000))
    facade.cognition.close()


def test_importance_maps_to_confidence(tmp_path):
    db = str(tmp_path / "e8.db")
    _seed(db, lesson="high value lesson", importance=0.9)
    facade = _facade(db)
    facade.refresh_from_memory(limit=20, include_consolidations=True)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "high value lesson" in b.statement][0]
    assert lifted.confidence == 0.95         # round(min(1, 0.5+0.5*0.9), 3)
    assert lifted.importance == 0.9
    facade.cognition.close()


def test_lesson_statements_bounded(tmp_path):
    db = str(tmp_path / "e9.db")
    long_lesson = "x" * 5000
    _seed(db, lesson=long_lesson)
    facade = _facade(db)
    facade.refresh_from_memory(limit=20, include_consolidations=True)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "x" in b.statement and len(b.statement) > 400]
    assert len(lifted) == 1
    assert len(lifted[0].statement) == 500   # bounded to STATEMENT_MAX
    facade.cognition.close()


def test_consolidation_feed_is_informational_only(tmp_path):
    db = str(tmp_path / "e10.db")
    _seed(db)
    facade = _facade(db)
    facade.cognition.record_preference(__import__(
        "arion.cognition.models", fromlist=["Preference"]).Preference(
        preference_id="p1", key="k", value="v", user="u", source="inferred",
        provenance={"episode_ids": ["e"]}))
    facade.cognition.record_environment_fact(__import__(
        "arion.cognition.models", fromlist=["EnvironmentFact"]).EnvironmentFact(
        fact_id="f1", key="fk", value={"a": 1}, source="system", version=1,
        observed_at=_iso_plus(T0, 1), created_at=_iso_plus(T0, 1),
        updated_at=_iso_plus(T0, 1)))
    facade.cognition.record_goal_plan("g1", 1, "direct", [{"v": 1}])
    facade.refresh_from_memory(limit=20, include_consolidations=True)

    assert facade.cognition.get_preference("k", user="u") is not None
    assert facade.cognition.get_environment_fact("fk") is not None
    assert len(facade.cognition.list_goal_plans("g1")) == 1
    facade.cognition.close()
