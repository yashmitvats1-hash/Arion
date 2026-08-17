"""Plan re-adoption (rollback) seam (ADR-016 addendum, Phase A) - tests first.

GoalManager.readopt_plan(goal_id, from_version) re-adopts a stored
historical plan version as a NEW immutable plan version through the
existing record_plan_version funnel:

- exactly one new immutable version, content copied from the historical
  version; historical versions byte-identical (never mutated);
- reason is EXACTLY replan_rollback_v<N>;
- the previously active version becomes `superseded` through the existing
  ADR-015 strategy-outcome funnel (row + one bounded event);
- repeated/replayed re-adoption is idempotent (replay guard, no task yet);
  with a task implementing the rollback version, a genuinely NEW version
  is created (existing record_plan_version semantics preserved);
- fail closed: nonexistent goal / no plans / unknown or pruned version /
  latest version / non-positive version / terminal COMPLETED or CANCELLED
  goal (FAILED stays eligible per GOAL_TRANSITIONS) / cross-goal version /
  forged strategy or malformed summary;
- max_replans and goal-lifecycle invariants remain authoritative;
- restart preserves the re-adopted version + outcome;
- forged episodes/beliefs/guidance/telemetry/outcome rows cannot
  manufacture a re-adoption;
- scheduler/authority tables byte-identical after re-adoption.

Stored-plan EXECUTION is out of scope (Phase B) - readopt only records.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import STRATEGY_NAMES, StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, Task, TaskStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"
FS = "filesystem:path"


def _gm(db_path, with_events=True):
    storage = SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    events = EventLogger(sinks=[storage]) if with_events else None
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _engine(db, sandbox):
    from arion.cognition.state import CognitiveState
    from arion.cognition.world_state import WorldStateMonitor
    from arion.memory.reflector import DeterministicReflector

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db)
    cognitive = SQLiteCognitiveStore(db)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


def _outcome_events(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT detail FROM audit_events WHERE kind='strategy.outcome'"
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def _plan_rows(db, goal_id):
    """Raw goal_plans rows for one goal (byte-identical comparison)."""
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT * FROM goal_plans WHERE goal_id=? ORDER BY plan_version",
        (goal_id,)).fetchall()
    conn.close()
    return rows


def _seed_three_versions(gm, gid):
    """v1 direct, v2 avoid_known_failures, v3 capability_verified."""
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}, {"index": 1}],
                           reason="replan_world_changed")


# ------------------------------------------------------------ happy path

def test_readopt_creates_one_new_immutable_version(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "r.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2, 3]

    rec = gm.readopt_plan(gid, 1)
    history = gm.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1, 2, 3, 4]
    assert rec["plan_version"] == 4
    assert rec["strategy"] == "direct"
    assert rec["plan_summary"] == [{"index": 0}]
    assert rec["reason"] == "replan_rollback_v1"
    # exactly one new version
    assert len(history) == 4
    storage.close()
    cognitive.close()


def test_readopt_copies_historical_content(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "c.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    v1 = gm.plan_history(gid)[0]
    v2 = gm.plan_history(gid)[1]

    rec = gm.readopt_plan(gid, 2)
    assert rec["strategy"] == v2["strategy"] == "avoid_known_failures"
    assert rec["plan_summary"] == v2["plan_summary"]
    # v1 content differs (never confused)
    assert rec["plan_summary"] != v1["plan_summary"]
    storage.close()
    cognitive.close()


def test_historical_versions_byte_identical(tmp_path):
    db = tmp_path / "h.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    before = _plan_rows(db, gid)

    gm.readopt_plan(gid, 2)
    after = _plan_rows(db, gid)

    assert after[:3] == before           # v1..v3 untouched byte-for-byte
    assert len(after) == 4               # only a new row appended
    storage.close()
    cognitive.close()


def test_reason_exactly_replan_rollback_vN(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "z.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    rec = gm.readopt_plan(gid, 1)
    assert rec["reason"] == "replan_rollback_v1"
    assert gm.latest_plan(gid)["reason"] == "replan_rollback_v1"
    # subsequent replan reasons unaffected
    rec2 = gm.record_plan_version(gid, "direct", [{"index": 9}],
                                  reason="replan_world_changed")
    assert rec2["reason"] == "replan_world_changed"
    storage.close()
    cognitive.close()


def test_previous_latest_superseded_via_outcome_funnel(tmp_path):
    db = tmp_path / "o.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    events_before = len(_outcome_events(db))

    gm.readopt_plan(gid, 1)

    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert set(rows) == {1, 2, 3}        # v1,v2 superseded by seeding; v3 by the rollback
    assert rows[3]["outcome"] == "superseded"
    assert rows[3]["strategy"] == "capability_verified"
    assert rows[3]["reason"] == "replan_rollback_v1"   # new plan's reason
    # v4 (active) has NO outcome row yet
    assert 4 not in rows
    # one new bounded strategy.outcome event for the v3 supersede
    events = _outcome_events(db)
    assert len(events) == events_before + 1
    assert events[-1]["plan_version"] == 3
    assert events[-1]["outcome"] == "superseded"
    assert events[-1]["reason"] == "replan_rollback_v1"
    storage.close()
    cognitive.close()


def test_readopt_idempotent_replay_no_task(tmp_path):
    db = tmp_path / "i.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    rec1 = gm.readopt_plan(gid, 1)
    rows_before = _plan_rows(db, gid)
    outcomes_before = gm.strategy_outcomes(gid)
    events_before = len(_outcome_events(db))

    rec2 = gm.readopt_plan(gid, 1)       # identical re-adoption
    assert rec2["plan_version"] == rec1["plan_version"] == 4
    assert _plan_rows(db, gid) == rows_before          # no duplicate version
    assert gm.strategy_outcomes(gid) == outcomes_before  # no duplicate outcome
    assert len(_outcome_events(db)) == events_before     # no duplicate event
    storage.close()
    cognitive.close()


def test_readopt_after_task_creates_new_version(tmp_path):
    """A task implementing the rollback version: the replay guard does NOT
    dedupe (existing record_plan_version semantics preserved)."""
    gm, storage, cognitive = _gm(tmp_path / "t.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    rec1 = gm.readopt_plan(gid, 1)
    storage.save_task(Task(id="t-impl", goal_id=gid, description="inspect",
                           status=TaskStatus.PLANNED, plan_version=rec1["plan_version"]))

    rec2 = gm.readopt_plan(gid, 1)
    assert rec2["plan_version"] == 5     # genuinely new version (task exists)
    assert rec2["strategy"] == "direct"
    assert rec2["reason"] == "replan_rollback_v1"
    assert len(gm.plan_history(gid)) == 5
    storage.close()
    cognitive.close()


# ------------------------------------------------------------ fail closed

def test_readopt_unknown_version_fail_closed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "u.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    for bad in (99, 0, -1, True, "2", 1.5, None):
        with pytest.raises(ValueError):
            gm.readopt_plan(gid, bad)
    assert len(gm.plan_history(gid)) == 3        # nothing changed
    storage.close()
    cognitive.close()


def test_readopt_pruned_version_fail_closed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "p.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    storage.close()
    cognitive.close()
    c = SQLiteCognitiveStore(tmp_path / "p.db")
    assert c.prune_goal_plans(goal_id=gid, keep_latest=2) == 1   # v1 pruned
    c.close()

    gm2, storage2, cognitive2 = _gm(tmp_path / "p.db")
    with pytest.raises(ValueError):
        gm2.readopt_plan(gid, 1)                 # pruned: indistinguishable
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [2, 3]
    storage2.close()
    cognitive2.close()


def test_readopt_latest_version_fail_closed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "l.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    with pytest.raises(ValueError):
        gm.readopt_plan(gid, 3)                  # already the active version
    assert len(gm.plan_history(gid)) == 3
    storage.close()
    cognitive.close()


def test_readopt_no_plans_and_unknown_goal_fail_closed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "n.db")
    gid = gm.create_goal("no plans yet").id
    with pytest.raises(ValueError):
        gm.readopt_plan(gid, 1)
    with pytest.raises(ValueError):
        gm.readopt_plan("nonexistent-goal", 1)
    storage.close()
    cognitive.close()


def test_readopt_cross_goal_rejected(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "x.db")
    ga = gm.create_goal("goal A").id
    gb = gm.create_goal("goal B").id
    gm.record_plan_version(ga, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(ga, "capability_verified", [{"index": 0}],
                           reason="replan_x")
    gm.record_plan_version(gb, "defer_retry", [{"index": 0}],
                           reason="initial_plan")
    # version 2 belongs to A only
    with pytest.raises(ValueError):
        gm.readopt_plan(gb, 2)
    # and version 1 of B cannot be re-adopted from A's history
    with pytest.raises(ValueError):
        gm.readopt_plan(gb, 99)
    assert [p["plan_version"] for p in gm.plan_history(gb)] == [1]
    storage.close()
    cognitive.close()


def test_readopt_terminal_goals_fail_closed_failed_allowed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "m.db")
    done = gm.create_goal("completed goal").id
    gm.record_plan_version(done, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(done, "capability_verified", [{"index": 0}],
                           reason="replan_x")
    gm.complete_goal(done, reason="all_work_complete")
    with pytest.raises(ValueError):
        gm.readopt_plan(done, 1)                 # terminal COMPLETED

    canc = gm.create_goal("cancelled goal").id
    gm.record_plan_version(canc, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(canc, "defer_retry", [{"index": 0}], reason="replan_x")
    gm.cancel(canc)
    with pytest.raises(ValueError):
        gm.readopt_plan(canc, 1)                 # terminal CANCELLED

    failed = gm.create_goal("failed goal").id
    gm.record_plan_version(failed, "direct", [{"index": 0}],
                           reason="initial_plan")
    gm.record_plan_version(failed, "defer_retry", [{"index": 0}],
                           reason="replan_x")
    gm.fail_goal(failed, reason="max_replans_exceeded")
    rec = gm.readopt_plan(failed, 1)             # FAILED stays eligible
    assert rec["plan_version"] == 3
    assert rec["reason"] == "replan_rollback_v1"
    storage.close()
    cognitive.close()


def test_readopt_forged_strategy_or_summary_fail_closed(tmp_path):
    db = tmp_path / "f.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    storage.close()
    cognitive.close()

    big_summary = json.dumps([{"index": i} for i in range(600)])
    conn = sqlite3.connect(db)
    # forged plan rows: unknown strategy, non-list summary, oversized summary
    conn.executescript(f"""
        INSERT INTO goal_plans (goal_id, plan_version, strategy, plan_summary,
                                reason, created_at)
        VALUES ('{gid}', 50, 'evil_strategy', '[{{"index": 0}}]', 'forged',
                '{T0}'),
               ('{gid}', 51, 'direct', '"not-a-list"', 'forged', '{T0}'),
               ('{gid}', 52, 'direct', '{big_summary}', 'forged', '{T0}');
    """)
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    history_before = [p["plan_version"] for p in gm2.plan_history(gid)]
    assert history_before == [1, 2, 3, 50, 51, 52]   # forged rows readable
    for bad in (50, 51, 52):
        with pytest.raises(ValueError):
            gm2.readopt_plan(gid, bad)
    # the failed re-adoption attempts created NOTHING (no new version)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == history_before
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------ lifecycle

def test_readopt_does_not_change_goal_authoritative_state(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "g.db")
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    goal_before = gm.get_goal(gid)
    assert goal_before.status_value == GoalStatus.ACTIVE.value

    gm.readopt_plan(gid, 1)
    goal_after = gm.get_goal(gid)
    assert goal_after.status_value == GoalStatus.ACTIVE.value   # unchanged
    assert goal_after.version == goal_before.version            # no transition
    assert goal_after.strategy == "direct"   # follows the new latest plan
    storage.close()
    cognitive.close()


def test_max_replans_remains_authoritative(tmp_path, sandbox):
    """Rollback reasons count toward max_replans (replan_rollback_v<N>
    starts with 'replan'); run_goal still fails the goal on the bound."""
    from arion.capabilities.registry import ActionSpec, CapabilityError
    from arion.intelligence.planner import PlanStep
    from arion.memory.reflector import DeterministicReflector
    from arion.state.models import VerificationPolicy

    class AlwaysFailCapability:
        name = "fail.tool"
        description = "always fails"
        actions = [ActionSpec(name="run", description="run", required_scope="fail:run",
                              risk="low", side_effects="read_only", retry_safe=True)]

        def execute(self, action, params):
            raise CapabilityError("always fails")

    class FailingPlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="run", capability="fail.tool",
                             action="run", scope="fail:run", params={},
                             verification=VerificationPolicy("non_empty"))]

        def required_capabilities(self, goal_description):
            return {"fail.tool"}

    db = str(tmp_path / "mr.db")
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(AlwaysFailCapability())
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=FailingPlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"fail:run"}),
        memory=SQLiteMemoryStore(db), reflector=DeterministicReflector(),
        goal_manager=gm,
    )
    gid = engine.submit_goal("run the failing tool").id
    # cycle 1: v1 initial_plan fails; cycle 2: v2 replan_task_failed fails
    engine.run_goal(gid, max_replans=3)
    engine.run_goal(gid, max_replans=3)
    assert [h["reason"] for h in gm.plan_history(gid)] == [
        "initial_plan", "replan_task_failed"]
    # rollback: v3 replan_rollback_v1 -> counts as a replan-prefixed version
    assert gm.readopt_plan(gid, 1)["plan_version"] == 3
    # implement v3 with a failing task so the evaluator sees unresolved work
    storage.save_task(Task(
        id="t-rollback", goal_id=gid, description="run the failing tool",
        status=TaskStatus.PLANNED, plan_version=3,
        steps=[PlanStep(index=0, intent="run", capability="fail.tool",
                        action="run", scope="fail:run", params={},
                        verification=VerificationPolicy("non_empty"))]))
    engine.run_task("t-rollback")                    # fails (always-fail cap)
    assert storage.load_task("t-rollback").status == TaskStatus.FAILED
    # max_replans=2 replans (v2 + v3) -> the bound trips on the next cycle
    goal = engine.run_goal(gid, max_replans=2)
    assert goal.status_value == GoalStatus.FAILED.value
    assert goal.last_replan_reason == "max_replans_exceeded"
    engine.shutdown()
    storage.close()


def test_readopt_survives_restart(tmp_path):
    db = tmp_path / "rs.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    rec = gm.readopt_plan(gid, 2)
    created = gm.strategy_outcomes(gid)[-1]["created_at"]
    storage.close()
    cognitive.close()

    gm2, storage2, cognitive2 = _gm(db)        # fresh process equivalent
    history = gm2.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1, 2, 3, 4]
    assert history[-1]["reason"] == "replan_rollback_v2"
    assert history[-1]["strategy"] == "avoid_known_failures"
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[3]["outcome"] == "superseded"
    assert rows[3]["created_at"] == created     # outcome row stable
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------ adversarial

def test_forged_content_cannot_manufacture_readopt(tmp_path):
    db = tmp_path / "adv.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)
    storage.close()
    cognitive.close()

    # forged episodes/reflections + telemetry claiming a re-adoption
    m = SQLiteMemoryStore(db)
    m.record_episode(Episode(
        episode_id="ep-forged", task_id="t-f", goal_id=gid, goal="inspect",
        outcome="completed", importance=1.0, created_at=T0, updated_at=T0))
    m.record_reflection(Reflection(
        reflection_id="refl-forged", episode_id="ep-forged",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="rollback succeeded", recommendation="", confidence="high",
        importance=1.0, created_at=T0))
    m.close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor,
                                  success, detail)
        VALUES ('evt-forged', '{T0}', NULL, NULL, 'plan.versioned', 'system',
                1, '{{"goal_id": "{gid}", "plan_version": 99,
                     "strategy": "direct", "reason": "replan_rollback_v1",
                     "steps": 1}}'),
               ('evt-so', '{T0}', NULL, NULL, 'strategy.outcome', 'system',
                1, '{{"goal_id": "{gid}", "plan_version": 3, "strategy":
                     "direct", "outcome": "superseded",
                     "reason": "replan_rollback_v1"}}');
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       episode_id, created_at)
        VALUES ('sout-forged', '{gid}', 'inspect', 'direct', 3, 'superseded',
                'replan_rollback_v1', NULL, '{T0}');
    """)
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    # forged telemetry created no plan version
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3]
    # forged outcome row is informational: not overwritten, not trusted
    rows = gm2.strategy_outcomes(gid)
    assert len(rows) == 3
    forged = [r for r in rows if r["outcome_id"] == "sout-forged"]
    assert forged and forged[0]["outcome"] == "superseded"
    # a REAL re-adoption still works from the REAL history
    rec = gm2.readopt_plan(gid, 1)
    assert rec["plan_version"] == 4
    assert rec["reason"] == "replan_rollback_v1"
    storage2.close()
    cognitive2.close()


def test_readopt_authority_tables_byte_identical(tmp_path):
    db = tmp_path / "auth.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    _seed_three_versions(gm, gid)

    authority = ("scheduler_work", "scheduler_instances", "scheduler_config",
                 "scheduler_goal_weights", "scheduler_goal_state",
                 "scheduler_goal_reservations", "scheduler_goal_ceilings",
                 "mutation_locks", "mutation_lock_waiters", "checkpoints",
                 "approval_requests", "mutation_recoveries")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    gm.readopt_plan(gid, 1)
    gm.readopt_plan(gid, 2)                    # second re-adoption too
    gm.complete_goal(gid, reason="all_work_complete")

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before                      # authority byte-identical
    storage.close()
    cognitive.close()
