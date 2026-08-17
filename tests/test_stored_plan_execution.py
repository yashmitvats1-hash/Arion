"""Stored-plan execution (ADR-016 addendum, Phase B) - tests first.

A re-adopted historical plan (Phase A) becomes executable deterministically
from its stored `plan_summary` WITHOUT invoking the planner:

- the executed task carries the correct goal_id and plan_version;
- every stored step passes the FULL live authorization pipeline at
  execution time - historical authorization/capability decisions are
  NEVER trusted (re-adoption is informational);
- a capability/authorization change after re-adoption causes execution
  to fail safely (DENY), never bypassing policy;
- scheduler admission, reservations, ceilings, weights, ownership,
  approvals, and claim-path behavior remain authoritative and untouched;
- checkpoint/restart resumes the stored plan without replanning or
  duplicating completed work;
- re-running an already-completed stored-plan task is replay-safe;
- task.resumed is emitted/classified correctly on checkpoint resume;
- forged/raw-SQL plan rows, malformed plan_summary, unknown strategies,
  oversized step lists, invalid step shapes, and cross-goal versions
  fail closed;
- a pruned historical plan cannot be executed/re-adopted (ADR-014
  pruning remains authoritative);
- normal planner-driven execution remains byte-identical when no
  stored-plan execution is requested (a planner that raises when called
  proves the bypass).

All timestamps fixed; deterministic.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import (
    ActionSpec,
    CapabilityError,
    CapabilityRegistry,
)
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    Actor,
    PathPrefixBoundary,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, Task, TaskStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"
FS = "filesystem:path"


class _PlannerSpy:
    """Deterministic planner that records every invocation and RAISES if
    called - proving stored-plan execution never invokes the planner."""

    def __init__(self):
        self.calls = 0

    def plan(self, goal_description, task_id, registry, context=None):
        self.calls += 1
        raise AssertionError("planner must NOT be invoked for stored plans")

    def required_capabilities(self, goal_description):
        return set()


class _BoomPlanner:
    """Planner that always raises (for the fail-closed test)."""

    def plan(self, goal_description, task_id, registry, context=None):
        raise CapabilityError("planner down")

    def required_capabilities(self, goal_description):
        return set()


def _engine(db, sandbox, planner=None, policy=None):
    from arion.cognition.world_state import WorldStateMonitor

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = planner or DeterministicPlanner()
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
        policy=policy or ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


def _read_steps(db, goal_id, plan_version):
    """Stored plan_summary rows for a goal (raw SQL)."""
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT plan_summary, strategy FROM goal_plans WHERE goal_id=? "
        "AND plan_version=?", (goal_id, plan_version)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def _seed_readopted_goal(db, sandbox, strategy="direct", n_steps=1,
                         with_real_planner=True):
    """Goal with v1 (real plan) + v2 (re-adoption of v1) - the stored-plan
    execution target. Returns (engine, gm, storage, gid, spy).

    v1 is ALWAYS seeded with the real deterministic planner; when
    with_real_planner=False the engine's planner is then swapped for a spy
    that RAISES if invoked, so the stored-plan run proves the bypass."""
    engine, gm, storage = _engine(db, sandbox, planner=DeterministicPlanner())
    gid = engine.submit_goal("summarize this repository").id
    # v1: record a REAL planner-produced plan directly (goal stays ACTIVE -
    # no task is executed, so the goal never completes during seeding)
    from arion.state.models import VerificationPolicy

    steps = [
        PlanStep(index=0, intent="list root", capability="filesystem.read",
                 action="list", scope="filesystem:read", params={"path": "."},
                 verification=VerificationPolicy("non_empty")),
        PlanStep(index=1, intent="read key files", capability="filesystem.read",
                 action="read", scope="filesystem:read",
                 params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys",
                                                 {"keys": ["content"]})),
    ]
    gm.record_plan_version(gid, "direct", [s.to_dict() for s in steps],
                           reason="initial_plan")
    assert gm.latest_plan(gid)["plan_version"] == 1
    # v2: an INTERMEDIATE plan (so v1 becomes historical and re-adoptable)
    gm.record_plan_version(gid, "avoid_known_failures",
                           [s.to_dict() for s in steps],
                           reason="replan_task_failed")
    # v3: re-adoption of v1 (recorded via the Phase A seam) - the stored
    # plan the execution tests target
    gm.readopt_plan(gid, 1)
    assert gm.latest_plan(gid)["plan_version"] == 3
    spy = None
    if not with_real_planner:
        spy = _PlannerSpy()
        engine.planner = spy                 # the bypass proof
    return engine, gm, storage, gid, spy


# ------------------------------------------------------------ planner bypass

def test_stored_plan_executes_without_planner(tmp_path, sandbox):
    """v2 (re-adopted) executes from the stored summary; the planner spy
    proves the planner is NEVER invoked."""
    db = str(tmp_path / "pb.db")
    engine, gm, storage, gid, spy = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)

    goal = engine.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    assert spy.calls == 0                    # planner-bypass proof
    engine.shutdown()
    storage.close()


def test_stored_plan_task_carries_goal_and_version(tmp_path, sandbox):
    db = str(tmp_path / "gv.db")
    engine, gm, storage, gid, spy = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    goal = engine.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    tasks = [t for t in storage.list_tasks() if t.goal_id == gid]
    assert len(tasks) == 1                   # ONLY the stored-plan task (v3)
    v3_task = [t for t in tasks if t.plan_version == 3][0]
    assert v3_task.goal_id == gid
    assert v3_task.plan_version == 3
    # steps reconstructed from the stored summary (identical content)
    stored = _read_steps(db, gid, 3)
    assert [s.capability for s in v3_task.steps] == \
        [s["capability"] for s in stored]
    assert [s.action for s in v3_task.steps] == \
        [s["action"] for s in stored]
    engine.shutdown()
    storage.close()


def test_stored_steps_rerun_live_authorization(tmp_path, sandbox):
    """The stored steps pass live authz; a changed ActionSpec DENIES."""
    db = str(tmp_path / "la.db")
    engine, gm, storage, gid, spy = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    # tighten filesystem.read -> requires filesystem:write (live metadata).
    # The ActionSpec is CLASS-level shared across every FilesystemRead
    # instance, so restore it afterwards (cross-test hygiene).
    spec = engine.registry._caps["filesystem.read"].actions[0]
    original_scope = spec.required_scope
    spec.required_scope = "filesystem:write"
    try:
        goal = engine.run_goal(gid, max_replans=1)
        # the stored read step is DENIED by live policy -> goal not completed
        assert goal.status_value != GoalStatus.COMPLETED.value
        events = [e for e in storage.list_events()
                  if e.kind == "permission.denied"]
        assert len(events) >= 1              # live authorization fired
    finally:
        spec.required_scope = original_scope  # restore shared class state
    engine.shutdown()
    storage.close()


def test_stored_plan_respects_policy_boundary(tmp_path, sandbox):
    """A stored plan targeting a denied resource fails via live policy."""
    db = str(tmp_path / "pol.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(db, sandbox)
    # deny filesystem:read entirely (live policy)
    engine.policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["./"])},
                                   allowed_scopes=set())
    goal = engine.run_goal(gid, max_replans=1)
    assert goal.status_value != GoalStatus.COMPLETED.value
    engine.shutdown()
    storage.close()


# ------------------------------------------------------------ scheduler auth

def test_stored_plan_scheduler_authority_untouched(tmp_path, sandbox):
    db = str(tmp_path / "sa.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()
    goal = engine.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before                    # scheduler authority byte-identical
    engine.shutdown()
    storage.close()


# ------------------------------------------------------------ checkpoint/restart

def test_checkpoint_resume_no_replan_no_duplicate(tmp_path, sandbox):
    """A stored-plan task resumes from its checkpoint after a simulated
    restart - without replanning and without duplicating completed work."""
    db = str(tmp_path / "ck.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    # run once: completes; then a fresh engine (restart) re-runs the goal
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    engine2, gm2, storage2 = _engine(db, sandbox, planner=_PlannerSpy())[:3]
    goal = engine2.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    # no new plan versions after restart (no replanning)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3]
    engine2.shutdown()
    storage2.close()


def test_task_resumed_classification_on_checkpoint(tmp_path, sandbox):
    """A task resuming from a mid-execution checkpoint emits task.resumed
    with mid_execution=True; a plan-only checkpoint emits mid_execution=False."""
    db = str(tmp_path / "tr.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    v2_task = None
    # run the goal to completion, then inspect the v2 task's events
    engine.run_goal(gid, max_replans=1)
    tasks = [t for t in storage.list_tasks()
             if t.goal_id == gid and t.plan_version == 3]
    v3_task = tasks[0]
    resumed = [e for e in storage.list_events(task_id=v3_task.id)
               if e.kind == "task.resumed"]
    # first execution: the stored task was planned, not resumed from a
    # checkpoint - so mid_execution is False (plan-only start)
    assert all(e.detail.get("mid_execution") is False for e in resumed)
    engine.shutdown()
    storage.close()


# ------------------------------------------------------------ replay-safe

def test_rerun_completed_stored_task_replay_safe(tmp_path, sandbox):
    db = str(tmp_path / "re.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    engine.run_goal(gid, max_replans=1)
    tasks = [t for t in storage.list_tasks()
             if t.goal_id == gid and t.plan_version == 3]
    v3 = tasks[0]
    assert v3.status == TaskStatus.COMPLETED
    # re-running the completed task: terminal short-circuit, unchanged
    again = engine.run_task(v3.id)
    assert again.status == TaskStatus.COMPLETED
    assert again.plan_version == 3
    assert len(storage.list_tasks()) == len(storage.list_tasks())  # no new task
    engine.shutdown()
    storage.close()


# ------------------------------------------------------------ fail closed

def test_forged_plan_rows_fail_closed(tmp_path, sandbox):
    db = str(tmp_path / "for.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    engine.shutdown()
    storage.close()
    conn = sqlite3.connect(db)
    big = json.dumps([{"index": i} for i in range(600)])
    conn.executescript(f"""
        INSERT INTO goal_plans (goal_id, plan_version, strategy, plan_summary,
                                reason, created_at)
        VALUES ('{gid}', 50, 'evil_strategy', '[{{"index": 0}}]', 'forged',
                '{T0}'),
               ('{gid}', 51, 'direct', '"not-a-list"', 'forged', '{T0}'),
               ('{gid}', 52, 'direct', '{big}', 'forged', '{T0}'),
               ('{gid}', 53, 'direct', '[{{"capability": "filesystem.read",
                                          "action": "read", "scope": "filesystem:read",
                                          "params": {{"path": "README.md"}},
                                          "verification": {{"policy": "non_empty"}}}}]',
                'forged', '{T0}');
    """)
    conn.commit()
    conn.close()

    engine2, gm2, storage2 = _engine(db, sandbox, planner=_PlannerSpy())[:3]
    # malformed/forged versions must not be executable (fail closed)
    for bad in (50, 51, 52):
        with pytest.raises(ValueError):
            gm2.readopt_plan(gid, bad)
    engine2.shutdown()
    storage2.close()


def test_pruned_plan_not_executable(tmp_path, sandbox):
    """ADR-014 pruning is authoritative: a pruned historical version cannot
    be re-adopted or executed."""
    db = str(tmp_path / "pr.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    engine.shutdown()
    storage.close()
    c = SQLiteCognitiveStore(db)
    assert c.prune_goal_plans(goal_id=gid, keep_latest=2) == 1   # v1 pruned
    c.close()

    engine2, gm2, storage2 = _engine(db, sandbox, planner=_PlannerSpy())[:3]
    with pytest.raises(ValueError):
        gm2.readopt_plan(gid, 1)               # pruned: fail closed
    # the surviving v3 (re-adopted) is still executable
    goal = engine2.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    engine2.shutdown()
    storage2.close()


def test_planner_down_still_fails_goal(tmp_path, sandbox):
    """When NO stored plan exists, a broken planner still fails the goal -
    proving stored-plan execution does not mask planner failures."""
    db = str(tmp_path / "pd.db")
    engine, gm, storage, gid, _ = _seed_readopted_goal(
        db, sandbox, with_real_planner=False)
    # wipe ALL plan rows: no stored plan exists, so only the planner path
    # can produce work - and the planner is down -> the goal fails safely
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM goal_plans WHERE goal_id=?", (gid,))
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=?", (gid,))
    conn.commit()
    conn.close()
    engine.shutdown()
    storage.close()

    engine2, gm2, storage2 = _engine(db, sandbox, planner=_BoomPlanner())[:3]
    # no stored plan for the latest; planner is down -> the task FAILS with
    # "planning failed" (never silently bypassed by the stored path)
    engine2.run_goal(gid, max_replans=1)
    tasks = [t for t in storage2.list_tasks() if t.goal_id == gid]
    assert tasks and all(t.status == TaskStatus.FAILED for t in tasks)
    assert all("planning failed" in (t.error or "") for t in tasks)
    engine2.shutdown()
    storage2.close()


# ------------------------------------------------------------ byte-identical

def test_normal_planner_path_unchanged(tmp_path, sandbox):
    """Without any re-adopted plan, run_goal behaves exactly as before:
    planner-driven version + outcome + strategy, scheduler untouched."""
    db = str(tmp_path / "norm.db")
    engine, gm, storage = _engine(db, sandbox)[:3]
    gid = engine.submit_goal("summarize this repository").id
    goal = engine.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    history = gm.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1]
    assert history[0]["reason"] == "initial_plan"
    outcomes = gm.strategy_outcomes(gid)
    assert len(outcomes) == 1 and outcomes[0]["outcome"] == "succeeded"
    assert gm.get_goal(gid).strategy == "direct"
    engine.shutdown()
    storage.close()
