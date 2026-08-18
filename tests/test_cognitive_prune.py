"""Cognitive archival/pruning (ADR-014 addendum, Phase B).

Tests-first suite for the two cognitive prune seams on SQLiteCognitiveStore:

- prune_superseded_beliefs: superseded-history pruning with per-lineage
  keep_versions; ACTIVE beliefs are never pruned (fail closed);
  bounded batches; dry_run; idempotent; only the beliefs table touched.
- prune_goal_plans: replan-history bounding with keep_latest; the latest
  plan version per goal is never pruned (replay safety); bounded batches;
  dry_run; idempotent; only the goal_plans table touched.

All timestamps are fixed (no wall clock); all ordering is deterministic
(superseded_at / plan_version).
"""

import pytest

from arion.cognition.models import Belief
from arion.cognition.store import SQLiteCognitiveStore

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _belief(belief_id, category, statement, confidence=0.5, version=1,
            created_at=T0, superseded_at=None, updated_at=None):
    return Belief(
        belief_id=belief_id, category=category, statement=statement,
        confidence=confidence, importance=0.5,
        provenance={"episode_ids": [f"ep-{belief_id}"]},
        source="deterministic", version=version,
        superseded_at=superseded_at,
        created_at=created_at,
        updated_at=updated_at or superseded_at or created_at,
    )


def _superseded_lineage(store, lineage, n, start_seconds=0, step=10):
    """Create `n` superseded belief versions in one lineage, each newer by
    `step` seconds (superseded_at = created_at + 5s for stability)."""
    ids = []
    for i in range(n):
        bid = f"b-{lineage}-{i}"
        created = _iso_plus(T0, start_seconds + i * step)
        store.record_belief(_belief(
            bid, "semantic", f"statement {lineage}",
            confidence=0.5 + i * 0.05, version=i + 1,
            created_at=created,
            superseded_at=_iso_plus(T0, start_seconds + i * step + 5),
        ))
        ids.append(bid)
    return ids


def _dump(store) -> dict[str, list]:
    """Deterministic full-table dump for byte-identical comparisons."""
    out = {}
    for table in ("beliefs", "preferences", "environment_facts", "goal_plans"):
        rows = store._conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        out[table] = rows
    return out


# ---------------------------------------------------------------- beliefs

def test_prune_superseded_requires_valid_keep_versions(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for bad in (0, -1, True, "2"):
        with pytest.raises(ValueError):
            store.prune_superseded_beliefs(keep_versions=bad)
    store.close()


def test_prune_superseded_batch_size_bounded(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for bad in (0, -1, 5001, True, "500"):
        with pytest.raises(ValueError):
            store.prune_superseded_beliefs(batch_size=bad)
    store.close()


def test_prune_superseded_invalid_older_than_fails_closed(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for bad in ("not-a-timestamp", "2026-13-99T00:00:00+00:00", ""):
        with pytest.raises(ValueError):
            store.prune_superseded_beliefs(older_than=bad)
    store.close()


def test_active_beliefs_never_pruned(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    # Two DISTINCT active beliefs (each (category, statement) has exactly
    # one active revision, per the durable belief-identity invariant): one
    # unversioned lineage and one at v2 with a superseded predecessor.
    store.record_belief(_belief("active-old", "semantic", "active stmt A",
                                created_at=_iso_plus(T0, 0)))
    store.record_belief(_belief("active-superseded", "semantic", "active stmt B",
                                created_at=_iso_plus(T0, 50),
                                superseded_at=_iso_plus(T0, 60)))
    store.record_belief(_belief("active-new", "semantic", "active stmt B",
                                confidence=0.9, version=2,
                                created_at=_iso_plus(T0, 100)))
    # dead lineage: older superseded version + newest superseded version
    store.record_belief(_belief("dead-old", "semantic", "old stmt",
                                created_at=_iso_plus(T0, 0),
                                superseded_at=_iso_plus(T0, 10)))
    store.record_belief(_belief("dead-new", "semantic", "old stmt",
                                confidence=0.8, version=2,
                                created_at=_iso_plus(T0, 20),
                                superseded_at=_iso_plus(T0, 30)))
    assert store.count_beliefs() == 2

    removed = store.prune_superseded_beliefs(older_than=_iso_plus(T0, 200))
    # dead-old is pruned (oldest superseded of the "old stmt" lineage beyond
    # keep_versions=1); active-superseded is the newest kept superseded row
    # of the "active stmt B" lineage and is retained.
    assert removed == 1
    assert store.count_beliefs() == 2          # active count unchanged (2 distinct active statements)
    active = [b.belief_id for b in store.list_beliefs(limit=100)]
    assert set(active) == {"active-new", "active-old"}
    assert store.get_belief("active-old") is not None
    assert store.get_belief("active-new") is not None
    store.close()


def test_active_never_pruned_even_without_cutoff(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 3)
    store.record_belief(_belief("a-active", "semantic", "statement A",
                                confidence=0.99, version=9,
                                created_at=_iso_plus(T0, 500)))
    before = {b.belief_id: b for b in
              store.list_beliefs(limit=100, include_superseded=True)}
    removed = store.prune_superseded_beliefs()  # bare call, keep_versions=1
    assert removed == 2                          # newest superseded kept
    after = {b.belief_id: b for b in
             store.list_beliefs(limit=100, include_superseded=True)}
    assert "a-active" in after                   # active untouched
    assert after["a-active"] == before["a-active"]
    assert store.count_beliefs() == 1
    store.close()


def test_prune_superseded_older_than_explicit_cutoff(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "X", 4, start_seconds=0, step=20)
    # superseded_at of version i = 20*i + 5; cut at 45 -> v0,v1 pruned,
    # v2 (45) retained at the boundary, v3 retained.
    removed = store.prune_superseded_beliefs(older_than=_iso_plus(T0, 45))
    assert removed == 2
    remaining = {b.belief_id for b in
                 store.list_beliefs(limit=100, include_superseded=True)}
    assert remaining == {"b-X-2", "b-X-3"}
    store.close()


def test_keep_versions_per_lineage(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 3)          # superseded at +5,+15,+25
    _superseded_lineage(store, "B", 2)          # superseded at +5,+15
    removed = store.prune_superseded_beliefs(keep_versions=1)
    assert removed == 3                          # 3+2 - 1 - 1
    remaining = {b.belief_id for b in
                 store.list_beliefs(limit=100, include_superseded=True)}
    assert remaining == {"b-A-2", "b-B-1"}       # newest superseded per lineage
    store.close()


def test_keep_versions_two_per_lineage(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 4)
    removed = store.prune_superseded_beliefs(keep_versions=2)
    assert removed == 2
    remaining = {b.belief_id for b in
                 store.list_beliefs(limit=100, include_superseded=True)}
    assert remaining == {"b-A-2", "b-A-3"}       # two newest superseded kept
    store.close()


def test_prune_superseded_drains_bounded_batches(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for l in range(5):                           # 5 lineages x 5 versions
        _superseded_lineage(store, str(l), 5, start_seconds=l * 100)
    removed = store.prune_superseded_beliefs(keep_versions=1, batch_size=7)
    assert removed == 20                         # 25 - 5 kept
    remaining = {b.belief_id for b in
                 store.list_beliefs(limit=1000, include_superseded=True)}
    assert len(remaining) == 5
    assert all(f"b-{l}-4" in remaining for l in range(5))
    store.close()


def test_prune_superseded_dry_run_mutates_nothing(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 4)
    store.record_belief(_belief("active", "semantic", "keep me",
                                created_at=_iso_plus(T0, 100)))
    before = _dump(store)
    # superseded_at: v0=+5, v1=+15, v2=+25, v3=+35; cutoff +20 -> v0,v1
    would = store.prune_superseded_beliefs(older_than=_iso_plus(T0, 20),
                                           dry_run=True)
    assert would == 2
    after = _dump(store)
    assert after == before                       # byte-identical, mutation-free
    store.close()


def test_prune_superseded_idempotent(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 3)
    first = store.prune_superseded_beliefs(keep_versions=1)
    second = store.prune_superseded_beliefs(keep_versions=1)
    assert first == 2
    assert second == 0
    store.close()


def test_prune_superseded_only_touches_beliefs(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 3)
    store.record_preference(__import__("arion.cognition.models", fromlist=["Preference"]).Preference(
        preference_id="p1", key="k", value="v", user="u", source="inferred",
        provenance={"episode_ids": ["e"]}))
    store.record_environment_fact(__import__("arion.cognition.models", fromlist=["EnvironmentFact"]).EnvironmentFact(
        fact_id="f1", key="fk", value={"a": 1}, source="s", version=1,
        observed_at=_iso_plus(T0, 1), created_at=_iso_plus(T0, 1),
        updated_at=_iso_plus(T0, 1)))
    store.record_goal_plan("g1", 1, "direct", [{"step": "s"}], reason="r")
    store.prune_superseded_beliefs(keep_versions=1)
    assert store.get_preference("k", user="u") is not None
    assert store.get_environment_fact("fk") is not None
    assert len(store.list_goal_plans("g1")) == 1
    store.close()


# ------------------------------------------------------------- goal plans

def test_prune_goal_plans_requires_valid_keep_latest(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for bad in (0, -1, True, "2"):
        with pytest.raises(ValueError):
            store.prune_goal_plans(keep_latest=bad)
    store.close()


def test_prune_goal_plans_batch_size_bounded(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for bad in (0, -1, 5001, True):
        with pytest.raises(ValueError):
            store.prune_goal_plans(batch_size=bad)
    store.close()


def test_latest_goal_plan_never_pruned(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 6):
        store.record_goal_plan("g1", v, "direct", [{"v": v}], reason=f"r{v}")
    removed = store.prune_goal_plans(keep_latest=1)
    assert removed == 4
    remaining = store.list_goal_plans("g1")
    assert [p["plan_version"] for p in remaining] == [5]
    latest = store.latest_goal_plan("g1")
    assert latest is not None and latest["plan_version"] == 5
    assert latest["plan_summary"] == [{"v": 5}]
    store.close()


def test_prune_goal_plans_keeps_newest_n(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 6):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    removed = store.prune_goal_plans(keep_latest=3)
    assert removed == 2
    assert [p["plan_version"] for p in store.list_goal_plans("g1")] == [3, 4, 5]
    store.close()


def test_prune_goal_plans_scoped_to_goal(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 4):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    for v in range(1, 4):
        store.record_goal_plan("g2", v, "direct", [{"v": v}])
    removed = store.prune_goal_plans(goal_id="g1", keep_latest=1)
    assert removed == 2
    assert [p["plan_version"] for p in store.list_goal_plans("g1")] == [3]
    assert [p["plan_version"] for p in store.list_goal_plans("g2")] == [1, 2, 3]
    store.close()


def test_prune_goal_plans_across_all_goals(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for g in ("g1", "g2"):
        for v in range(1, 4):
            store.record_goal_plan(g, v, "direct", [{"v": v}])
    removed = store.prune_goal_plans(keep_latest=1)
    assert removed == 4
    assert [p["plan_version"] for p in store.list_goal_plans("g1")] == [3]
    assert [p["plan_version"] for p in store.list_goal_plans("g2")] == [3]
    store.close()


def test_prune_goal_plans_drains_bounded_batches(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 26):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    removed = store.prune_goal_plans(keep_latest=1, batch_size=7)
    assert removed == 24
    assert [p["plan_version"] for p in store.list_goal_plans("g1")] == [25]
    store.close()


def test_prune_goal_plans_dry_run_mutates_nothing(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 5):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    before = _dump(store)
    would = store.prune_goal_plans(keep_latest=2, dry_run=True)
    assert would == 2
    assert _dump(store) == before               # byte-identical, mutation-free
    store.close()


def test_prune_goal_plans_idempotent(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 4):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    first = store.prune_goal_plans(keep_latest=1)
    second = store.prune_goal_plans(keep_latest=1)
    assert first == 2
    assert second == 0
    store.close()


def test_replay_and_latest_semantics_intact_after_prune(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    for v in range(1, 6):
        store.record_goal_plan("g1", v, "direct", [{"step": f"s{v}", "n": v}],
                               reason=f"why-{v}")
    store.prune_goal_plans(keep_latest=2)
    plans = store.list_goal_plans("g1")
    # ascending plan_version order preserved (replay order)
    assert [p["plan_version"] for p in plans] == [4, 5]
    # plan_summary JSON round-trip intact; reason intact; strategy intact
    assert plans[0]["plan_summary"] == [{"step": "s4", "n": 4}]
    assert plans[0]["reason"] == "why-4"
    assert plans[0]["strategy"] == "direct"
    assert plans[1]["plan_summary"] == [{"step": "s5", "n": 5}]
    assert store.latest_goal_plan("g1")["plan_version"] == 5
    store.close()


def test_prune_goal_plans_only_touches_goal_plans(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "c.db")
    _superseded_lineage(store, "A", 2)
    store.record_preference(__import__("arion.cognition.models", fromlist=["Preference"]).Preference(
        preference_id="p1", key="k", value="v", user="u", source="inferred",
        provenance={"episode_ids": ["e"]}))
    store.record_environment_fact(__import__("arion.cognition.models", fromlist=["EnvironmentFact"]).EnvironmentFact(
        fact_id="f1", key="fk", value={"a": 1}, source="s", version=1,
        observed_at=_iso_plus(T0, 1), created_at=_iso_plus(T0, 1),
        updated_at=_iso_plus(T0, 1)))
    for v in range(1, 4):
        store.record_goal_plan("g1", v, "direct", [{"v": v}])
    store.prune_goal_plans(keep_latest=1)
    beliefs = store.list_beliefs(limit=100, include_superseded=True)
    assert len(beliefs) == 2                     # beliefs untouched
    assert store.get_preference("k", user="u") is not None
    assert store.get_environment_fact("fk") is not None
    store.close()
