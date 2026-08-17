"""Memory archival/pruning (ADR-014 addendum, Phase A) - tests first.

`SQLiteMemoryStore.prune` implements the designed archival seam:

- older_than: episodes older than the explicit cutoff are removed (with
  their reflections); never silent, never recent;
- max_episodes: keep the NEWEST N episodes (by created_at);
- keep_importance: protect episodes with importance >= floor from
  age-pruning;
- batch_size bounded [1, 5000], fail closed outside;
- dry_run: returns the would-be count, mutates NOTHING;
- reflections are pruned WITH their episodes; orphan reflections of
  surviving episodes stay;
- consolidations are NEVER pruned (they are the permanent merged
  summary);
- idempotent: a second identical prune removes 0;
- explicit deterministic behavior: ISO timestamps, no wall-clock races;
- authority tables completely untouched (tasks, goals, scheduler work,
  scheduler config, reservations/ceilings/weights, audit events,
  beliefs/preferences/environment_facts/goal_plans).
"""

from __future__ import annotations

import pytest

from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _ep(ep_id: str, task_id: str, created: str, importance: float = 0.5,
        outcome: str = "completed", reflection: bool = False) -> Episode:
    ep = Episode(
        episode_id=ep_id, task_id=task_id, goal_id="g-1",
        goal=f"goal for {ep_id}", outcome=outcome, importance=importance,
        created_at=created, updated_at=created,
    )
    ep.lifecycle = "consolidated"
    if reflection:
        ep.reflection_id = f"refl-{ep_id}"
    return ep


def _seed(memory: SQLiteMemoryStore) -> None:
    """Three episodes at t=0, 10, 20; the middle one has a reflection;
    the newest is salient."""
    for i, (ep_id, t, imp) in enumerate((
            ("ep-old", 0, 0.3),
            ("ep-mid", 10, 0.5),
            ("ep-new", 20, 0.9))):
        memory.record_episode(_ep(ep_id, f"task-{i}", _iso_plus(T0, t), imp,
                                  reflection=(ep_id == "ep-mid")))
    if memory.get_reflection("refl-ep-mid") is None:
        memory.record_reflection(Reflection(
            reflection_id="refl-ep-mid", episode_id="ep-mid",
            what_happened="x", what_worked="", what_failed="", why="",
            lesson="mid lesson", recommendation="", confidence="medium",
            importance=0.5, created_at=_iso_plus(T0, 10)))
    from arion.memory.store import ConsolidationRecord
    memory.record_consolidation(ConsolidationRecord(
        consolidation_id="consol-1", source_episode_ids=["ep-old", "ep-mid"],
        category="completed", merged_lesson="merged", count=2,
        importance=0.6, created_at=_iso_plus(T0, 30)))


# --------------------------------------------------------------------------- #
# older_than
# --------------------------------------------------------------------------- #


def test_prune_older_than_removes_old_episodes_and_reflections(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    removed = memory.prune(older_than=_iso_plus(T0, 5))
    assert removed == 1  # only ep-old (t=0 < 5); ep-mid (10) and ep-new stay
    assert memory.get_episode("ep-old") is None
    assert memory.get_episode("ep-mid") is not None
    assert memory.get_episode("ep-new") is not None
    # ep-mid survived, so its reflection survives
    assert memory.get_reflection("refl-ep-mid") is not None
    memory.close()


def test_prune_older_than_removes_reflection_with_episode(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    removed = memory.prune(older_than=_iso_plus(T0, 15))
    assert removed == 2  # ep-old + ep-mid (with refl-ep-mid)
    assert memory.get_episode("ep-mid") is None
    assert memory.get_reflection("refl-ep-mid") is None  # pruned WITH it
    assert memory.get_episode("ep-new") is not None
    memory.close()


def test_prune_older_than_never_touches_recent(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    removed = memory.prune(older_than=_iso_plus(T0, 25))  # cutoff beyond all
    assert removed == 3
    assert memory.list_recent(limit=100) == []
    # a second identical prune removes nothing (idempotent)
    assert memory.prune(older_than=_iso_plus(T0, 25)) == 0
    memory.close()


# --------------------------------------------------------------------------- #
# max_episodes (count-capped, keeps newest)
# --------------------------------------------------------------------------- #


def test_prune_max_episodes_keeps_newest(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    removed = memory.prune(max_episodes=2)
    assert removed == 1
    remaining = [e.episode_id for e in memory.list_recent(limit=100)]
    assert remaining == ["ep-new", "ep-mid"]  # newest two kept
    assert memory.get_reflection("refl-ep-mid") is not None
    memory.close()


def test_prune_max_episodes_zero_or_negative_fails_closed(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    with pytest.raises(ValueError):
        memory.prune(max_episodes=0)
    with pytest.raises(ValueError):
        memory.prune(max_episodes=-5)
    assert len(memory.list_recent(limit=100)) == 3  # nothing changed
    memory.close()


# --------------------------------------------------------------------------- #
# keep_importance
# --------------------------------------------------------------------------- #


def test_prune_keep_importance_protects_salient_episodes(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)  # ep-old imp 0.3, ep-mid 0.5, ep-new 0.9
    # cutoff removes ep-old + ep-mid, but the 0.9 floor protects nothing
    # extra here (ep-new is newest); use a floor that protects ep-mid
    removed = memory.prune(older_than=_iso_plus(T0, 15),
                           keep_importance=0.4)
    assert removed == 1  # only ep-old (imp 0.3 < 0.4); ep-mid protected
    assert memory.get_episode("ep-mid") is not None
    assert memory.get_episode("ep-old") is None
    memory.close()


def test_prune_keep_importance_out_of_range_fails_closed(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    with pytest.raises(ValueError):
        memory.prune(older_than=_iso_plus(T0, 5), keep_importance=-0.1)
    with pytest.raises(ValueError):
        memory.prune(older_than=_iso_plus(T0, 5), keep_importance=1.5)
    assert len(memory.list_recent(limit=100)) == 3
    memory.close()


# --------------------------------------------------------------------------- #
# batch_size
# --------------------------------------------------------------------------- #


def test_prune_batch_size_bounded_and_drains(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    for i in range(25):
        memory.record_episode(_ep(f"ep-{i}", f"task-{i}", _iso_plus(T0, i)))
    # batch_size 10: the loop drains all 25 in bounded batches
    removed = memory.prune(older_than=_iso_plus(T0, 1000), batch_size=10)
    assert removed == 25
    assert memory.list_recent(limit=100) == []
    memory.close()


def test_prune_batch_size_validation_fails_closed(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    for bad in (0, -1, 5001, 1.5, "10"):
        with pytest.raises(ValueError):
            memory.prune(older_than=_iso_plus(T0, 5),
                         batch_size=bad)  # type: ignore[arg-type]
    assert len(memory.list_recent(limit=100)) == 3
    memory.close()


# --------------------------------------------------------------------------- #
# dry_run
# --------------------------------------------------------------------------- #


def test_prune_dry_run_mutates_nothing(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    before = {
        "eps": [e.episode_id for e in memory.list_recent(limit=100)],
        "refs": [r.reflection_id
                 for r in memory.list_recent_reflections(limit=100)],
        "cons": [c.consolidation_id for c in memory.list_consolidations()],
    }
    would = memory.prune(older_than=_iso_plus(T0, 15), dry_run=True)
    assert would == 2  # ep-old + ep-mid
    after = {
        "eps": [e.episode_id for e in memory.list_recent(limit=100)],
        "refs": [r.reflection_id
                 for r in memory.list_recent_reflections(limit=100)],
        "cons": [c.consolidation_id for c in memory.list_consolidations()],
    }
    assert after == before  # byte-identical: dry-run is mutation-free
    # the real prune then removes exactly the predicted count
    assert memory.prune(older_than=_iso_plus(T0, 15)) == 2
    memory.close()


def test_prune_dry_run_with_max_episodes(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    assert memory.prune(max_episodes=2, dry_run=True) == 1
    assert len(memory.list_recent(limit=100)) == 3  # nothing removed
    memory.close()


# --------------------------------------------------------------------------- #
# consolidations never pruned
# --------------------------------------------------------------------------- #


def test_prune_never_touches_consolidations(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    memory.prune(older_than=_iso_plus(T0, 1000))  # wipe ALL episodes
    cons = memory.list_consolidations()
    assert len(cons) == 1
    assert cons[0].consolidation_id == "consol-1"
    assert cons[0].source_episode_ids == ["ep-old", "ep-mid"]  # provenance kept
    memory.close()


# --------------------------------------------------------------------------- #
# authority isolation
# --------------------------------------------------------------------------- #


def test_prune_touches_only_memory_tables(db_path: str, sandbox):
    """Tasks, goals, scheduler work/config/policy, audit events, and the
    cognitive tables are byte-identical after pruning everything."""
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    cognitive = SQLiteCognitiveStore(db_path)
    from arion.cognition.models import Belief
    cognitive.record_belief(Belief(
        belief_id="b-1", category="semantic", statement="X is achievable",
        confidence=0.8, importance=0.6))

    storage = SQLiteStorage(db_path)
    goal = storage.save_goal(__import__("arion.state.models", fromlist=["Goal"])
                             .Goal(id="goal-1", description="g", source="cli"))
    storage.save_task(__import__("arion.state.models", fromlist=["Task"]).Task(
        id="task-x", goal_id="goal-1", description="t",
        status=TaskStatus.COMPLETED))
    storage.set_scheduler_global_max(4)
    row = storage.create(task_id="task-x", goal_id="goal-1", step_index=0,
                         scheduler_id="sched-1")
    storage.claim(row.work_id, "w-1", 60.0, None, 600.0,
                  scheduler_id="sched-1")
    storage.set_goal_reservation("goal-1", 2)
    storage.set_goal_ceiling("goal-1", 4)
    storage.set_goal_weight("goal-1", 3)
    events_before = storage.scheduler_event_count()

    memory.prune(older_than=_iso_plus(T0, 1000))
    assert storage.load_task("task-x") is not None
    assert storage.load_goal("goal-1") is not None
    assert storage.get_scheduler_global_max() == 4
    work = storage.list_work()
    assert len(work) == 1 and work[0].worker_id == "w-1"
    assert storage.get_goal_reservation("goal-1") == 2
    assert storage.get_goal_ceiling("goal-1") == 4
    assert storage.get_goal_weight("goal-1") == 3
    assert storage.scheduler_event_count() == events_before
    assert cognitive.get_belief("b-1") is not None  # cognition untouched
    memory.close()
    cognitive.close()
    storage.close()


def test_prune_invalid_timestamp_fails_closed(db_path: str):
    memory = SQLiteMemoryStore(db_path)
    _seed(memory)
    with pytest.raises(ValueError):
        memory.prune(older_than="not-a-timestamp")
    assert len(memory.list_recent(limit=100)) == 3
    memory.close()
