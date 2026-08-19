"""Durable goal-plan version-allocation invariant.

A previous defect let two concurrent replanners corrupt immutable plan
lineage:

    Worker A: latest_plan() -> N
    Worker B: latest_plan() -> N
    Worker A: record_goal_plan(N+1)   INSERT OR REPLACE
    Worker B: record_goal_plan(N+1)   INSERT OR REPLACE  (silently replaces A)

The ``goal_plans`` table identity is ``(goal_id, plan_version)``. Version
allocation was a best-effort read-decide-write sequence, and
``record_goal_plan`` used destructive ``INSERT OR REPLACE``.

This module proves the DURABLE guarantee:

- allocation is append-only and monotonic at ``MAX(plan_version) + 1``;
- an existing ``(goal_id, plan_version)`` row is never overwritten;
- divergent concurrent claims all survive as distinct versions;
- identical concurrent claims of an unimplemented latest plan adopt one
  canonical row (first-writer-wins);
- an equivalent replan after a task references the latest plan creates a
  NEW version;
- only the creator of a new version emits ``plan.versioned``;
- pruning gaps are valid (dense numbering is not required);
- a crash after the plan insert but before the strategy-outcome write is
  healed by ``repair_strategy_outcomes`` without corrupting lineage;
- ``readopt_plan`` / ``diff_plans`` / the stored-plan fast path stay
  compatible with the claim funnel.

Concurrency is proven with genuinely independent SQLite connections and
real subprocesses sharing the same database.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import GoalPlanClaimResult, SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.observability.events import EventLogger
from arion.state.models import Task, TaskStatus
from arion.state.store import SQLiteStorage

from conftest import MemorySink

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _gm(db_path, events=None):
    storage = SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _plans(store, goal_id="g1"):
    return store.list_goal_plans(goal_id)


# --------------------------------------------------------------------------- #
# 1. In-process divergent concurrent claims
# --------------------------------------------------------------------------- #


def test_in_process_divergent_concurrent_claims(tmp_path):
    """Two independent connections claim DIVERGENT plans at the same
    moment. Both rows must survive as distinct versions; neither
    immutable plan is overwritten."""
    db = tmp_path / "div.db"
    store_a = SQLiteCognitiveStore(db)
    store_b = SQLiteCognitiveStore(db)
    barrier = threading.Barrier(2)
    results: dict[str, GoalPlanClaimResult] = {}
    errors: list[BaseException] = []

    def worker(store, tag, summary):
        try:
            barrier.wait(timeout=30)
            results[tag] = store.claim_goal_plan(
                "g1", "direct", summary, reason=f"replan_{tag}")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    t_a = threading.Thread(
        target=worker, args=(store_a, "A", [{"index": 0, "intent": "list"}]))
    t_b = threading.Thread(
        target=worker, args=(store_b, "B", [{"index": 1, "intent": "read"}]))
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors
    assert results["A"].created and results["B"].created
    assert results["A"].plan["plan_version"] != results["B"].plan["plan_version"]

    plans = _plans(store_a)
    versions = sorted(p["plan_version"] for p in plans)
    assert versions == [1, 2]
    intents = {p["plan_summary"][0]["intent"] for p in plans}
    assert intents == {"list", "read"}
    reasons = {p["reason"] for p in plans}
    assert reasons == {"replan_A", "replan_B"}
    store_a.close()
    store_b.close()


# --------------------------------------------------------------------------- #
# 2. Real-subprocess divergent concurrent claims
# --------------------------------------------------------------------------- #


_HELPER = textwrap.dedent(r"""
    import json, os, sys, time
    sys.path.insert(0, %r)
    from arion.cognition.store import SQLiteCognitiveStore
    db, tag, intent, barrier_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    store = SQLiteCognitiveStore(db)
    ready = os.path.join(barrier_dir, "ready-" + tag)
    open(ready, "w").close()
    for _ in range(200):
        if (os.path.exists(os.path.join(barrier_dir, "ready-A"))
                and os.path.exists(os.path.join(barrier_dir, "ready-B"))):
            break
        time.sleep(0.01)
    r = store.claim_goal_plan(
        "g1", "direct", [{"index": 0, "intent": intent}],
        reason="replan_" + tag)
    print(json.dumps({
        "created": r.created,
        "plan_version": r.plan["plan_version"],
        "intent": r.plan["plan_summary"][0]["intent"],
        "reason": r.plan["reason"],
    }))
    store.close()
""" % str(REPO))


def test_subprocess_divergent_concurrent_claims(tmp_path):
    """Two real processes sharing one database claim divergent plans.
    The durable topology — not process-local return values — is the
    authority: both versions exist and neither row was replaced."""
    db = str(tmp_path / "xproc.db")
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    SQLiteCognitiveStore(db).close()  # pre-init schema so DDL does not race
    p_a = subprocess.Popen(
        [sys.executable, "-c", _HELPER, db, "A", "list", str(barrier_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p_b = subprocess.Popen(
        [sys.executable, "-c", _HELPER, db, "B", "read", str(barrier_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out_a, err_a = p_a.communicate(timeout=60)
    out_b, err_b = p_b.communicate(timeout=60)
    assert p_a.returncode == 0, err_a
    assert p_b.returncode == 0, err_b
    ra = json.loads(out_a.strip().splitlines()[-1])
    rb = json.loads(out_b.strip().splitlines()[-1])
    assert ra["created"] and rb["created"]
    assert ra["plan_version"] != rb["plan_version"]

    store = SQLiteCognitiveStore(db)
    plans = _plans(store)
    versions = sorted(p["plan_version"] for p in plans)
    assert versions == [1, 2]
    intents = {p["plan_summary"][0]["intent"] for p in plans}
    assert intents == {"list", "read"}
    store.close()


# --------------------------------------------------------------------------- #
# 3. Identical concurrent claims adopt one canonical plan
# --------------------------------------------------------------------------- #


def test_identical_concurrent_claims_adopt_canonical(tmp_path):
    """Two independent connections claim the SAME unimplemented plan.
    Exactly one row is inserted; the loser adopts the canonical version."""
    db = tmp_path / "ident.db"
    store_a = SQLiteCognitiveStore(db)
    store_b = SQLiteCognitiveStore(db)
    barrier = threading.Barrier(2)
    results: dict[str, GoalPlanClaimResult] = {}
    summary = [{"index": 0, "intent": "list"}]

    def worker(store, tag):
        barrier.wait(timeout=30)
        results[tag] = store.claim_goal_plan(
            "g1", "direct", summary, reason="initial_plan")

    t_a = threading.Thread(target=worker, args=(store_a, "A"))
    t_b = threading.Thread(target=worker, args=(store_b, "B"))
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)

    created = [tag for tag, r in results.items() if r.created]
    assert len(created) == 1
    plans = _plans(store_a)
    assert len(plans) == 1 and plans[0]["plan_version"] == 1
    assert results["A"].plan["plan_version"] == results["B"].plan["plan_version"] == 1
    assert results["A"].plan["reason"] == results["B"].plan["reason"] == "initial_plan"
    store_a.close()
    store_b.close()


# --------------------------------------------------------------------------- #
# 4. Repeated concurrent claims: monotonic allocation (incl. prune gaps)
# --------------------------------------------------------------------------- #


def test_repeated_concurrent_claims_monotonic_versions(tmp_path):
    """N concurrent divergent claims produce N distinct monotonic versions
    at MAX(plan_version)+1. After pruning leaves a gap, the next claim
    continues past the historical max — dense numbering is not required."""
    db = tmp_path / "mono.db"
    seed = SQLiteCognitiveStore(db)
    for v in (1, 2, 3):
        seed.record_goal_plan("g1", v, "direct", [{"v": v}], reason=f"r{v}")
    assert seed.prune_goal_plans(goal_id="g1", keep_latest=2) == 1  # v1 gone
    remaining = [p["plan_version"] for p in _plans(seed)]
    assert remaining == [2, 3]
    seed.close()

    n = 6
    stores = [SQLiteCognitiveStore(db) for _ in range(n)]
    barrier = threading.Barrier(n)
    results: list[GoalPlanClaimResult] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(store, i):
        try:
            barrier.wait(timeout=30)
            r = store.claim_goal_plan(
                "g1", "direct", [{"index": i}], reason=f"replan_{i}")
            with lock:
                results.append(r)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(stores[i], i))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert all(r.created for r in results)
    claimed = sorted(r.plan["plan_version"] for r in results)
    assert claimed == list(range(4, 4 + n))  # gap at 1 is preserved
    assert len(set(claimed)) == n

    witness = SQLiteCognitiveStore(db)
    versions = [p["plan_version"] for p in _plans(witness)]
    assert versions == [2, 3] + list(range(4, 4 + n))
    witness.close()
    for s in stores:
        s.close()


# --------------------------------------------------------------------------- #
# 5. Replay dedup when no task implements the latest plan
# --------------------------------------------------------------------------- #


def test_replay_dedup_when_no_task_implements_latest(tmp_path):
    """Re-recording the same (strategy, summary, reason) with NO task
    implementing the latest plan adopts the existing version."""
    gm, storage, cognitive = _gm(tmp_path / "replay.db")
    gid = gm.create_goal("inspect").id
    summary = [{"index": 0, "intent": "list"}]
    v1 = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    v1b = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    assert v1b["plan_version"] == v1["plan_version"] == 1
    assert len(gm.plan_history(gid)) == 1
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 6. Equivalent replan after a task references the previous plan
# --------------------------------------------------------------------------- #


def test_equivalent_replan_after_task_creates_new_version(tmp_path):
    """A task that already references the latest equivalent plan forces a
    genuinely NEW version even when strategy/summary/reason match."""
    gm, storage, cognitive = _gm(tmp_path / "impl.db")
    gid = gm.create_goal("inspect").id
    summary = [{"index": 0}]
    v1 = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    storage.save_task(Task(
        id="task_1", goal_id=gid, description="inspect",
        status=TaskStatus.FAILED, plan_version=v1["plan_version"]))
    v2 = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    assert v2["plan_version"] == 2
    assert len(gm.plan_history(gid)) == 2
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2]
    # historical v1 is byte-identical to the new equivalent v2 content
    history = gm.plan_history(gid)
    assert history[0]["plan_summary"] == history[1]["plan_summary"]
    assert history[0]["strategy"] == history[1]["strategy"]
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 7. Destructive overwrite refusal
# --------------------------------------------------------------------------- #


def test_destructive_overwrite_refused(tmp_path):
    """record_goal_plan is a plain INSERT: a colliding (goal_id,
    plan_version) raises IntegrityError and the original row is
    preserved byte-for-byte."""
    store = SQLiteCognitiveStore(tmp_path / "ow.db")
    store.record_goal_plan("g1", 1, "direct", [{"a": 1}], reason="first")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_goal_plan(
            "g1", 1, "avoid_known_failures", [{"a": 2}], reason="clobber")
    row = store.latest_goal_plan("g1")
    assert row is not None
    assert row["strategy"] == "direct"
    assert row["plan_summary"] == [{"a": 1}]
    assert row["reason"] == "first"
    assert len(_plans(store)) == 1
    store.close()


# --------------------------------------------------------------------------- #
# 8. Exactly one plan.versioned event per created version
# --------------------------------------------------------------------------- #


def test_one_plan_versioned_event_per_created_version(tmp_path):
    sink = MemorySink()
    storage = SQLiteStorage(tmp_path / "ev.db")
    cognitive = SQLiteCognitiveStore(tmp_path / "ev.db")
    events = EventLogger(sinks=[storage, sink])
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    # replay of the latest unimplemented plan: no new version, no event
    gm.record_plan_version(gid, "avoid_known_failures",
                           [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    assert sink.count("plan.versioned") == 2
    persisted = [e for e in storage.list_events() if e.kind == "plan.versioned"]
    assert len(persisted) == 2
    assert [e.detail["plan_version"] for e in persisted] == [1, 2]
    assert len(gm.plan_history(gid)) == 2
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 9. Exactly one total event across identical concurrent claims
# --------------------------------------------------------------------------- #


def test_one_event_across_identical_concurrent_claims(tmp_path):
    db = tmp_path / "ev2.db"
    st_a = SQLiteStorage(db)
    st_b = SQLiteStorage(db)
    c_a = SQLiteCognitiveStore(db)
    c_b = SQLiteCognitiveStore(db)
    sink = MemorySink()
    gm_a = GoalManager(
        storage=st_a, cognitive_store=c_a,
        events=EventLogger(sinks=[st_a, sink]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    gm_b = GoalManager(
        storage=st_b, cognitive_store=c_b,
        events=EventLogger(sinks=[st_b, sink]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    gid = gm_a.create_goal("inspect").id
    summary = [{"index": 0, "intent": "list"}]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(gm):
        try:
            barrier.wait(timeout=30)
            gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t_a = threading.Thread(target=worker, args=(gm_a,))
    t_b = threading.Thread(target=worker, args=(gm_b,))
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors
    assert sink.count("plan.versioned") == 1
    witness = SQLiteStorage(db)
    persisted = [e for e in witness.list_events() if e.kind == "plan.versioned"]
    assert len(persisted) == 1
    assert persisted[0].detail["plan_version"] == 1
    assert len(c_a.list_goal_plans(gid)) == 1
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 10. Strategy-outcome crash / repair
# --------------------------------------------------------------------------- #


def test_strategy_outcome_crash_repair(tmp_path):
    """A crash after the plan INSERT but before the predecessor
    superseded outcome is recorded does not corrupt plan lineage.
    repair_strategy_outcomes reconstructs the missing outcome and is
    idempotent."""
    db = tmp_path / "crash.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2]
    assert len(gm.strategy_outcomes(gid)) == 1

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.commit()
    conn.close()

    # lineage is the authority — wiping outcomes must not touch plans
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2]
    written = gm.repair_strategy_outcomes()
    assert written == 1
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 1
    assert rows[0]["plan_version"] == 1
    assert rows[0]["outcome"] == "superseded"
    assert rows[0]["reason"] == "replan_task_failed"
    assert gm.repair_strategy_outcomes() == 0  # idempotent
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2]
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 11. readopt_plan parity
# --------------------------------------------------------------------------- #


def test_readopt_plan_parity(tmp_path):
    """readopt_plan still goes through record_plan_version / claim_goal_plan:
    a rollback creates a NEW immutable version; repeating it with no
    implementing task adopts the canonical rollback version."""
    gm, storage, cognitive = _gm(tmp_path / "readopt.db")
    gid = gm.create_goal("inspect").id
    v1_summary = [{"index": 0, "capability": "filesystem.read", "action": "list"}]
    v2_summary = [{"index": 0, "capability": "filesystem.read", "action": "read"}]
    gm.record_plan_version(gid, "direct", v1_summary, reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", v2_summary,
                           reason="replan_task_failed")
    rolled = gm.readopt_plan(gid, 1)
    assert rolled["plan_version"] == 3
    assert rolled["strategy"] == "direct"
    assert rolled["plan_summary"] == v1_summary
    assert rolled["reason"] == "replan_rollback_v1"
    again = gm.readopt_plan(gid, 1)
    assert again["plan_version"] == 3
    assert len(gm.plan_history(gid)) == 3
    # predecessor of the rollback is superseded via the existing funnel
    outcomes = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert outcomes[2]["outcome"] == "superseded"
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 12. diff_plans / stored-plan fast-path compatibility
# --------------------------------------------------------------------------- #


def test_diff_plans_and_stored_plan_fast_path(tmp_path):
    """Plans allocated through the claim funnel remain readable by
    diff_plans and surface as the stored latest plan the engine fast
    path consumes (no implementing task, non-empty summary)."""
    gm, storage, cognitive = _gm(tmp_path / "diff.db")
    gid = gm.create_goal("inspect").id
    a = [{"index": 0, "capability": "filesystem.read", "action": "list"}]
    b = [{"index": 0, "capability": "filesystem.read", "action": "read"},
         {"index": 1, "capability": "filesystem.read", "action": "list"}]
    gm.record_plan_version(gid, "direct", a, reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", b,
                           reason="replan_task_failed")

    diff = gm.diff_plans(gid, 1, 2)
    assert diff["goal_id"] == gid
    assert diff["version_a"] == 1 and diff["version_b"] == 2
    assert diff["strategy_a"] == "direct"
    assert diff["strategy_b"] == "avoid_known_failures"
    assert diff["identical"] is False
    assert diff["steps_a"] == 1 and diff["steps_b"] == 2

    same = gm.diff_plans(gid, 2, 2)
    assert same["identical"] is True
    assert same["added"] == [] and same["removed"] == []

    latest = gm.latest_plan(gid)
    assert latest is not None
    assert latest["plan_version"] == 2
    assert latest["plan_summary"] == b
    assert latest["strategy"] == "avoid_known_failures"
    # stored-plan fast-path preconditions: latest exists, has a stored
    # summary, and no task implements it.
    assert not gm._any_task_for_plan(gid, latest["plan_version"])
    assert isinstance(latest["plan_summary"], list) and latest["plan_summary"]
    storage.close()
    cognitive.close()
