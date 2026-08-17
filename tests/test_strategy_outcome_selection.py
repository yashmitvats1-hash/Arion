"""Outcome-conditioned strategy selection (ADR-015 addendum, Phase B) -
tests first.

The optional `outcome_history` seam on StrategySelector.select() is a
POST-RULE PREFERENCE LAYER:

- empty/omitted history -> BYTE-IDENTICAL behavior to the existing five
  deterministic rules;
- the preference layer applies ONLY when the base rules would select
  `direct` (it never overrides a non-direct base result);
- SUCCESS preference: a non-direct strategy that succeeded for a similar
  goal context may be preferred (most successes, then name asc);
- AVOIDANCE/escalation: `direct` with >= 2 non-success rows (failed OR
  superseded) for a similar goal context escalates to
  avoid_known_failures (when avoid guidance exists) or defer_retry;
- success evidence BEATS failure evidence (a strategy that worked for a
  similar goal is preferred over avoiding the base);
- insufficient or dissimilar history fabricates nothing;
- bounded, deterministic: at most the first 20 rows of the store's
  deterministic (goal_id, plan_version) listing are considered; no
  timestamps, no wall clock;
- fail closed on malformed outcome rows (unknown strategy/outcome, missing
  keys, bad types) - forged rows cannot bypass validation;
- provenance carries outcome_ids of the evidence rows; the preference is
  informational only and never touches scheduler/task authority.
"""

from __future__ import annotations

import sqlite3

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import Strategy, StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.guidance import MemoryGuidance
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _sig(s: Strategy) -> dict:
    """Deterministic comparison signature (excludes random strategy_id)."""
    return {
        "name": s.name,
        "description": s.description,
        "constraints": s.constraints,
        "provenance": s.provenance,
    }


def _outcome(outcome_id, goal_id, goal_description, strategy, plan_version,
             outcome, reason=""):
    return {
        "outcome_id": outcome_id,
        "goal_id": goal_id,
        "goal_description": goal_description,
        "strategy": strategy,
        "plan_version": plan_version,
        "outcome": outcome,
        "reason": reason,
        "episode_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _avoid_guidance(episode_id="ep-1"):
    return [MemoryGuidance(
        guidance_id="g-avoid", category="avoid", capability="filesystem.read",
        action="read", resource="README.md", strategy="defer",
        episode_id=episode_id, reason="denied", importance=0.8)]


def _achiev_belief():
    from arion.cognition.models import Belief

    return [Belief(
        belief_id="b-ach", category="semantic",
        statement="read on 'docs.md' is achievable",
        confidence=0.7, importance=0.5,
        provenance={"episode_ids": ["ep-1"]}, source="deterministic",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00")]


def _blocked_belief():
    from arion.cognition.models import Belief

    return [Belief(
        belief_id="b-block", category="semantic",
        statement="read on 'docs.md' is not permitted by current policy",
        confidence=0.7, importance=0.5,
        provenance={"episode_ids": ["ep-2"]}, source="deterministic",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00")]


# ------------------------------------------- signature / byte-identity

def test_select_accepts_outcome_history_seam(tmp_path):
    sel = StrategySelector()
    s1 = sel.select("inspect this repository", [], {}, [],
                    outcome_history=None)
    s2 = sel.select("inspect this repository", [], {}, [],
                    outcome_history=[])
    s3 = sel.select("inspect this repository", [], {}, [],
                    outcome_history=[_outcome("o1", "g1", "unrelated goal",
                                              "capability_verified", 2,
                                              "succeeded")])
    assert _sig(s1) == _sig(s2) == _sig(s3)


def test_empty_history_byte_identical_across_base_rules(tmp_path):
    sel = StrategySelector()
    cases = [
        ("inspect this repository", [], {}, [], []),                 # direct
        ("read docs", _achiev_belief(), {}, [], []),                 # verified
        ("read docs", _blocked_belief(), {}, [], []),                # defer
        ("read docs", [], {},
         _avoid_guidance("ep-1"), []),                               # avoid
        ("read docs", [], {}, _avoid_guidance("ep-1"),
         ["avoid_known_failures"]),                                  # escalate
        ("inspect repo", [], {"registered_capabilities": {"value":
         ["filesystem.read", "http.get"]}}, [], []),                # blocked cap
    ]
    for goal, beliefs, env, guidance, prev in cases:
        base = sel.select(goal, beliefs, env, guidance,
                          previous_strategies=prev)
        empty = sel.select(goal, beliefs, env, guidance,
                           previous_strategies=prev, outcome_history=[])
        none = sel.select(goal, beliefs, env, guidance,
                          previous_strategies=prev, outcome_history=None)
        assert _sig(base) == _sig(empty) == _sig(none), goal


def test_dissimilar_history_does_not_change_selection(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "write a python script", "defer_retry", 1,
                 "succeeded"),
        _outcome("o2", "g1", "write a python script", "defer_retry", 2,
                 "failed"),
    ]
    base = sel.select("read the notes", [], {}, [])
    with_hist = sel.select("read the notes", [], {}, [],
                           outcome_history=history)
    assert _sig(base) == _sig(with_hist)


def test_malformed_outcome_rows_fail_closed(tmp_path):
    sel = StrategySelector()
    good = _outcome("o1", "g1", "inspect repo", "capability_verified", 1,
                    "succeeded")
    for mutate in (
        {},                                                     # not a dict
        {"outcome_id": "o1"},                                   # missing keys
        {**good, "strategy": "evil"},                           # unknown strat
        {**good, "outcome": "pending"},                         # unknown outcome
        {**good, "plan_version": 0},                            # bad version
        {**good, "goal_id": ""},                                # empty goal
        {**good, "goal_description": 7},                        # bad type
        {**good, "reason": None},                               # bad type
    ):
        with pytest.raises(ValueError):
            sel.select("inspect this repository", [], {}, [],
                       outcome_history=[mutate])
    # a valid row mixed with one malformed row still fails closed
    with pytest.raises(ValueError):
        sel.select("inspect this repository", [], {}, [],
                   outcome_history=[good, {"bad": True}])


# --------------------------------------------------- preference rules

def test_success_preference_changes_selection(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "capability_verified", 2,
                 "succeeded", reason="all_work_complete"),
        _outcome("o2", "g1", "inspect repository", "direct", 1,
                 "superseded", reason="replan_task_failed"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "capability_verified"
    assert s.provenance.get("outcome_ids") == ["o1"]     # evidence only
    assert "o2" not in s.provenance.get("outcome_ids", [])


def test_success_preference_requires_similar_context(tmp_path):
    sel = StrategySelector()
    history = [_outcome("o1", "g1", "write a python script",
                        "capability_verified", 1, "succeeded")]
    s = sel.select("read the notes", [], {}, [], outcome_history=history)
    assert s.name == "direct"                      # dissimilar: no preference


def test_success_preference_tie_break_count_then_name(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "defer_retry", 1, "succeeded"),
        _outcome("o2", "g2", "inspect repository", "capability_verified", 1,
                 "succeeded"),
    ]
    # tie at one success each -> strategy name ascending
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "capability_verified"          # "capability_verified" < "defer_retry"
    # a second success breaks the tie by count
    history.append(_outcome("o3", "g3", "inspect repository", "defer_retry",
                            2, "succeeded"))
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "defer_retry"


def test_direct_successes_never_preferred(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "direct", 1, "succeeded"),
        _outcome("o2", "g1", "inspect repository", "direct", 2, "succeeded"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "direct"                       # no-op candidates skipped


def test_two_direct_failures_escalate_defer(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("o2", "g2", "inspect repository", "direct", 1, "failed"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "defer_retry"
    assert s.provenance.get("outcome_ids") == ["o1", "o2"]


def test_avoids_handled_by_base_rule_not_preference_layer(tmp_path):
    """With avoid guidance present, BASE rule 3 fires before the preference
    layer ever runs (base==direct implies no avoids), so the layer's
    avoidance branch is unreachable by construction."""
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("o2", "g2", "inspect repository", "direct", 1, "failed"),
    ]
    s = sel.select("inspect repository and summarize", [], {},
                   _avoid_guidance("ep-9"), outcome_history=history)
    assert s.name == "avoid_known_failures"          # base rule 3, not the layer
    assert "outcome_ids" not in s.provenance         # the layer never ran
    assert s.constraints["avoid"] == [{"capability": "filesystem.read",
                                       "action": "read",
                                       "resource": "README.md"}]


def test_single_failure_insufficient_no_fabrication(tmp_path):
    sel = StrategySelector()
    history = [_outcome("o1", "g1", "inspect repository", "direct", 1,
                        "failed")]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "direct"                        # 1 < 2: no preference


def test_superseded_counts_as_nonsuccess_evidence(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("o2", "g1", "inspect repository", "direct", 2, "superseded"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "defer_retry"                   # failed + superseded >= 2


def test_success_beats_failure_avoidance(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("o2", "g2", "inspect repository", "direct", 1, "failed"),
        _outcome("o3", "g3", "inspect repository", "defer_retry", 1,
                 "succeeded"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "defer_retry"                   # success evidence wins
    assert s.provenance.get("outcome_ids") == ["o3"]


def test_preference_layer_never_overrides_base_rules(tmp_path):
    sel = StrategySelector()
    history = [_outcome("o1", "g1", "inspect repository",
                        "capability_verified", 1, "succeeded")]
    # base = blocked_missing_capability
    env = {"registered_capabilities": {"value": ["filesystem.read"]}}
    s = sel.select("inspect http.get repo", [], env, [], outcome_history=history)
    assert s.name == "blocked_missing_capability"
    # base = defer_retry (belief)
    s = sel.select("read docs", _blocked_belief(), {}, [],
                   outcome_history=history)
    assert s.name == "defer_retry"
    # base = avoid_known_failures (guidance)
    s = sel.select("read docs", [], {}, _avoid_guidance("ep-1"),
                   outcome_history=history)
    assert s.name == "avoid_known_failures"
    # base = escalation defer_retry (previous strategy)
    s = sel.select("read docs", [], {}, _avoid_guidance("ep-1"),
                   previous_strategies=["avoid_known_failures"],
                   outcome_history=history)
    assert s.name == "defer_retry"
    # base = capability_verified (belief)
    s = sel.select("read docs", _achiev_belief(), {}, [],
                   outcome_history=history)
    assert s.name == "capability_verified"


def test_history_bounded_to_first_20_rows(tmp_path):
    sel = StrategySelector()
    history = []
    # 20 dissimilar rows (goal ids sort before the similar one)
    for i in range(20):
        history.append(_outcome(f"o{i:02d}", f"g{i:02d}", "write a script",
                                "defer_retry", 1, "succeeded"))
    # a similar-context success that sorts AFTER the first 20 rows
    history.append(_outcome("o20", "zz-sim", "inspect repository",
                            "capability_verified", 1, "succeeded"))
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "direct"                        # bounded out of the window
    # the same success inside the window is considered
    history[19] = _outcome("o19", "aa-sim", "inspect repository",
                           "capability_verified", 1, "succeeded")
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert s.name == "capability_verified"


# ---------------------------------------------- provenance / authority

def test_preference_provenance_carries_outcome_ids(tmp_path):
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "capability_verified", 2,
                 "succeeded"),
        _outcome("o2", "g1", "inspect repository", "direct", 1, "superseded"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    assert set(s.provenance) == {"belief_ids", "episode_ids", "guidance_ids",
                                 "outcome_ids"}
    assert s.provenance["outcome_ids"] == ["o1"]
    # base selection without history has NO outcome_ids key at all
    base = sel.select("inspect repository and summarize", [], {}, [])
    assert "outcome_ids" not in base.provenance


def _engine(db, sandbox):
    from arion.cognition.state import CognitiveState
    from arion.cognition.world_state import WorldStateMonitor

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db)
    cognitive = SQLiteCognitiveStore(db)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()),
                          source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


def test_engine_feeds_outcome_history_into_selection(tmp_path, sandbox):
    """A similar goal's completed `capability_verified` plan influences the
    next similar goal's selection (informational cross-goal learning)."""
    db = str(tmp_path / "eng.db")
    engine, gm, storage = _engine(db, sandbox)

    # Seed goal G1's plan history + outcome DIRECTLY (authoritative funnels):
    # v1 direct (superseded by replan), v2 capability_verified (succeeded).
    g1 = gm.create_goal("inspect repository")
    gm.record_plan_version(g1.id, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(g1.id, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(g1.id, reason="all_work_complete")

    # New similar goal through the REAL engine: base rules would say
    # `direct` (no avoids, no blocking beliefs); outcome history prefers
    # capability_verified.
    g2 = engine.submit_goal("inspect repository and summarize")
    g2 = engine.run_goal(g2.id)
    assert g2.status_value == GoalStatus.COMPLETED.value
    history = gm.plan_history(g2.id)
    assert history[0]["strategy"] == "capability_verified", history
    outcomes = {r["plan_version"]: r for r in gm.strategy_outcomes(g2.id)}
    assert outcomes[1]["outcome"] == "succeeded"
    engine.shutdown()
    storage.close()


def test_engine_preference_informational_only(tmp_path, sandbox):
    """Outcome-conditioned selection never touches scheduler/task/ownership
    authority (byte-identical before/after the run)."""
    db = str(tmp_path / "auth2.db")
    engine, gm, storage = _engine(db, sandbox)
    g1 = gm.create_goal("inspect repository")
    gm.record_plan_version(g1.id, "capability_verified", [{"index": 0}],
                           reason="initial_plan")
    gm.complete_goal(g1.id, reason="all_work_complete")

    # scheduler_work/instances/checkpoints are legitimately created by
    # execution (random ids); the strategy preference must never touch the
    # deterministic authority/config tables
    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests",
                 "mutation_recoveries")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    g2 = engine.submit_goal("inspect repository and summarize")
    engine.run_goal(g2.id)
    engine.shutdown()
    storage.close()

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before
