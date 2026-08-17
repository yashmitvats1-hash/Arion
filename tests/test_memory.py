"""Memory store tests (ADR-012): CRUD, persistence across restart, retrieval,
filtering, malformed records, bounded context."""

import pytest

from arion.memory.models import (
    ContextBudget,
    Episode,
    EpisodeFilter,
    Reflection,
)
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.state.models import utcnow


def _episode(goal="inspect the repository", outcome="completed", tags=None, importance=0.5,
             task_id="task_1", authorization=None):
    return Episode(
        episode_id=f"ep_{goal[:4]}_{task_id}",
        task_id=task_id,
        goal_id="goal_1",
        goal=goal,
        plan_summary=[{"index": 0, "intent": "read", "capability": "filesystem.read",
                       "action": "read", "status": "succeeded", "params_keys": ["path"]}],
        actions=[{"capability": "filesystem.read", "action": "read", "status": "succeeded", "attempts": 1}],
        outcome=outcome,
        verification={"passed": [0], "failed": []},
        failures=[],
        authorization=authorization or {"denials": [], "approvals_required": False},
        recovery={"resumed": False},
        tags=tags or ["filesystem.read", f"outcome:{outcome}"],
        importance=importance,
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def _reflection(episode_id, lesson="lessons learned"):
    return Reflection(
        reflection_id=f"refl_{episode_id}",
        episode_id=episode_id,
        what_happened="a task ran",
        what_worked="steps passed",
        what_failed="nothing",
        why="ok",
        lesson=lesson,
        recommendation="repeat",
        confidence="high",
        importance=0.6,
        created_at=utcnow(),
    )


# ---- CRUD ----


def test_record_and_get_episode(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    ep = _episode()
    store.record_episode(ep)
    got = store.get_episode(ep.episode_id)
    assert got is not None
    assert got.goal == ep.goal
    assert got.outcome == "completed"
    assert got.tags == ep.tags
    assert got.plan_summary == ep.plan_summary
    assert "path" in got.plan_summary[0]["params_keys"]  # keys stored, values never
    store.close()


def test_record_and_get_reflection(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    ep = _episode()
    store.record_episode(ep)
    ref = _reflection(ep.episode_id)
    store.record_reflection(ref)
    store.link_reflection(ep.episode_id, ref.reflection_id)
    got = store.get_reflection(ref.reflection_id)
    assert got is not None
    assert got.lesson == "lessons learned"
    assert store.get_episode(ep.episode_id).reflection_id == ref.reflection_id
    store.close()


def test_get_missing_returns_none(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    assert store.get_episode("nope") is None
    assert store.get_reflection("nope") is None
    store.close()


def test_malformed_record_rejected(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    with pytest.raises(ValueError):
        store.record_episode(Episode(episode_id="ep_bad", goal="", outcome="bogus_outcome"))
    with pytest.raises(ValueError):
        store.record_episode(Episode(episode_id="", goal="x", outcome="completed"))
    store.close()


# ---- persistence across restart ----


def test_episodes_persist_across_restart(tmp_path):
    db = tmp_path / "m.db"
    store_a = SQLiteMemoryStore(db)
    store_a.record_episode(_episode(task_id="task_1"))
    store_a.record_episode(_episode(goal="list the files", outcome="failed", tags=["filesystem.read", "outcome:failed"], task_id="task_2"))
    store_a.close()

    # fresh process, same DB
    store_b = SQLiteMemoryStore(db)
    recent = store_b.list_recent(limit=10)
    assert len(recent) == 2
    goals = {e.goal for e in recent}
    assert goals == {"inspect the repository", "list the files"}
    assert store_b.get_episode(recent[0].episode_id) is not None
    store_b.close()


def test_reflections_persist_across_restart(tmp_path):
    db = tmp_path / "m.db"
    store_a = SQLiteMemoryStore(db)
    ep = _episode(task_id="task_1")
    store_a.record_episode(ep)
    store_a.record_reflection(_reflection(ep.episode_id))
    store_a.close()

    store_b = SQLiteMemoryStore(db)
    refs = store_b.list_recent_reflections(limit=5)
    assert len(refs) == 1
    assert refs[0].episode_id == ep.episode_id
    store_b.close()


# ---- retrieval / filtering ----


def test_search_filters(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.record_episode(_episode(goal="inspect the repository", outcome="completed", task_id="t1"))
    store.record_episode(_episode(goal="read the readme file", outcome="failed",
                                  tags=["filesystem.read", "outcome:failed", "category:execution"], task_id="t2"))
    store.record_episode(_episode(goal="format the disk", outcome="denied",
                                  tags=["storage.write", "outcome:denied", "authorization:denied"],
                                  authorization={"denials": [{"scope": "storage:write"}], "approvals_required": False},
                                  task_id="t3"))

    assert len(store.search_episodes(EpisodeFilter(outcome="failed"))) == 1
    assert len(store.search_episodes(EpisodeFilter(capability="storage.write"))) == 1
    assert len(store.search_episodes(EpisodeFilter(text="readme"))) == 1
    assert len(store.search_episodes(EpisodeFilter(tag="authorization:denied"))) == 1
    assert len(store.search_episodes(EpisodeFilter(limit=1))) == 1
    assert len(store.search_episodes(EpisodeFilter())) == 3
    store.close()


def test_retrieval_ranks_by_relevance(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.record_episode(_episode(goal="inspect the repository", task_id="t1"))   # token overlap high
    store.record_episode(_episode(goal="order some pizza", task_id="t2"))          # no overlap
    retriever = MemoryRetriever(store)
    results = retriever.retrieve("inspect the repository", top_k=5)
    assert results[0].task_id == "t1"
    assert "pizza" not in {r.goal for r in results[:1]}
    store.close()


def test_retrieval_prefers_failures_for_related_goal(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.record_episode(_episode(goal="read the readme file", outcome="failed", task_id="t1"))
    store.record_episode(_episode(goal="read the readme file", outcome="completed", task_id="t2"))
    retriever = MemoryRetriever(store)
    results = retriever.retrieve("read the readme file", top_k=2)
    assert results[0].task_id == "t1"  # failed episodes score higher
    store.close()


# ---- bounded context ----


def test_bounded_context_respects_budget(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    for i in range(20):
        store.record_episode(_episode(goal=f"inspect repository number {i}", task_id=f"t{i}",
                                      importance=0.4 + (i % 3) * 0.1))
        store.record_reflection(_reflection(f"ep_insp_{i}", lesson=f"lesson {i}"))
    retriever = MemoryRetriever(store)
    ctx = build_planning_context(retriever, "inspect repository", ContextBudget(max_episodes=5, max_reflections=3))
    assert len(ctx.episodes) <= 5
    assert len(ctx.reflections) <= 3
    digest = ctx.digest()
    assert digest["counts"]["episodes"] <= 5
    assert digest["counts"]["reflections"] <= 3
    store.close()


def test_context_digest_is_privacy_safe(tmp_path):
    """Digest exposes summaries (goal, plan capability/action) but NEVER param
    VALUES or raw transcripts - the privacy guarantee of the memory layer."""
    store = SQLiteMemoryStore(tmp_path / "m.db")
    ep = _episode(goal="inspect the vault", task_id="t1")
    # simulate that a (sensitive) value was passed to the capability
    ep.plan_summary[0]["params_keys"] = ["path"]
    store.record_episode(ep)
    ctx = build_planning_context(MemoryRetriever(store), "inspect", ContextBudget())
    import json

    raw = json.dumps(ctx.digest())
    assert "params_keys" not in raw          # the digest never exposes param keys/values
    assert "path" not in raw.lower()          # capability/action pairs only, no params
    # goal text IS part of a bounded summary - but that's the only free text
    assert '"goal": "inspect the vault"' in raw
    store.close()


def test_tiny_budget_truncates(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    for i in range(10):
        store.record_episode(_episode(goal=f"inspect repository {i} with lots of words to make it long", task_id=f"t{i}"))
    retriever = MemoryRetriever(store)
    ctx = build_planning_context(retriever, "inspect", ContextBudget(max_episodes=10, max_chars=400))
    digest = ctx.digest()
    import json

    assert len(json.dumps(digest)) <= 400 + 200  # truncated (allows small slack for the envelope)
    assert digest.get("truncated") is True
    store.close()
