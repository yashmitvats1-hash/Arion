"""Strategy-outcome recording (ADR-015 addendum, Phase A) - tests first.

Durable, informational strategy outcomes recorded through the two
AUTHORITATIVE GoalManager funnels:

- record_plan_version()  -> the previous plan version becomes `superseded`
                            (reason = the new plan's reason);
- transition() terminal  -> the ACTIVE (latest) plan version becomes
                            `succeeded` (goal completed) or `failed`
                            (goal failed).

Invariants pinned here:

- UNIQUE(goal_id, plan_version) - exactly one outcome row per plan version;
- idempotent under retries and replay (no duplicate rows, no duplicate
  versions);
- restart/reopen preserves outcomes (shared DB, no in-memory state);
- repair_strategy_outcomes() backfills MISSING rows ONLY from authoritative
  goal/goal_plans state (never from episodes/telemetry/planner content),
  never overwrites existing rows, and is idempotent;
- forged memory/reflection/guidance/belief/planner metadata cannot create
  or alter outcomes;
- outcome recording never mutates scheduler/task/ownership/config state
  (informational only).

All timestamps fixed; all assertions deterministic.
"""

from __future__ import annotations

import sqlite3

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import STRATEGY_NAMES, STRATEGY_OUTCOME_STATES, StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, TaskStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"
FS = "filesystem:path"


def _gm(db_path):
    storage = SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _seed_scheduler_authority(db):
    """Realistic scheduler/task authority rows (fixed timestamps)."""
    _st = SQLiteStorage(db)      # create the state schema
    _st.close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                    scheduler_id, worker_id, status, attempts,
                                    error, created_at, started_at, completed_at,
                                    lease_expires_at)
        VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', 'worker-1', 'running', 1, NULL,
                '{T0}', '{T0}', NULL, '2026-01-01T00:01:00+00:00');
        INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                         heartbeat_at, lease_expires_at)
        VALUES ('sched-1', 42, '{T0}', '{T0}', '2026-01-01T00:01:00+00:00');
        INSERT INTO scheduler_config (key, value) VALUES ('max_lease_seconds', '120');
        INSERT INTO scheduler_goal_weights (goal_id, weight, enabled,
                                            updated_at, updated_by)
        VALUES ('g1', 3, 1, '{T0}', 'operator');
        INSERT INTO scheduler_goal_state (goal_id, deficit, updated_at)
        VALUES ('g1', 2, '{T0}');
        INSERT INTO scheduler_goal_reservations (goal_id, reservation, enabled,
                                                 updated_at, updated_by)
        VALUES ('g1', 1, 1, '{T0}', 'operator');
        INSERT INTO scheduler_goal_ceilings (goal_id, ceiling, enabled,
                                             updated_at, updated_by)
        VALUES ('g1', 5, 1, '{T0}', 'operator');
        INSERT INTO mutation_locks (lock_id, resource_kind, resource, capability,
                                    action, owner_id, acquired_at, expires_at)
        VALUES ('lock-1', 'filesystem:path', 'x.txt', 'filesystem.write',
                'write', 'worker-1', '{T0}', '2026-01-01T00:01:00+00:00');
    """)
    conn.commit()
    conn.close()


def _engine(db, sandbox):
    from arion.cognition.goals import GoalManager
    from arion.cognition.progress import DeterministicProgressEvaluator
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
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()),
                          source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )


# ------------------------------------------------------------- store seam

def test_outcomes_table_unique_invariant(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "s.db")
    store.record_strategy_outcome("g1", "goal one", "direct", 1, "superseded",
                                  reason="replan_failed")
    store.record_strategy_outcome("g1", "goal one", "direct", 2, "succeeded",
                                  reason="all_work_complete")
    assert store.count_strategy_outcomes() == 2
    # duplicate (goal_id, plan_version) is impossible through the record API
    store.record_strategy_outcome("g1", "goal one", "direct", 2, "succeeded",
                                  reason="all_work_complete")
    assert store.count_strategy_outcomes() == 2
    # and the DB-level UNIQUE invariant holds for raw SQL too
    conn = sqlite3.connect(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO strategy_outcomes (outcome_id, goal_id, "
            "goal_description, strategy, plan_version, outcome, reason, "
            "created_at) VALUES ('x1', 'g1', 'goal one', 'direct', 2, "
            "'failed', '', '2026-01-01T00:00:00+00:00')")
    conn.close()
    store.close()


def test_record_validation_fail_closed(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "v.db")
    good = dict(goal_id="g1", goal_description="goal one", strategy="direct",
                plan_version=1, outcome="superseded", reason="r")
    for mutate in (
        {"outcome": "pending"},                      # unknown outcome state
        {"outcome": "SUCCEEDED"},                    # wrong case
        {"strategy": "evil"},                        # unknown strategy name
        {"plan_version": 0},                         # non-positive version
        {"plan_version": -3},
        {"plan_version": True},
        {"goal_id": ""},                             # empty goal id
        {"goal_id": 42},
        {"reason": 7},
    ):
        kw = dict(good)
        kw.update(mutate)
        with pytest.raises(ValueError):
            store.record_strategy_outcome(**kw)
    assert store.count_strategy_outcomes() == 0
    store.close()


def test_record_bounds_long_text(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "b.db")
    store.record_strategy_outcome(
        "g1", "x" * 5000, "direct", 1, "failed",
        reason="y" * 5000, episode_id="ep-1")
    row = store.get_strategy_outcome("g1", 1)
    assert row is not None
    assert len(row["goal_description"]) == 300        # bounded context
    assert len(row["reason"]) == 200                  # bounded reason
    assert row["episode_id"] == "ep-1"
    store.close()


def test_list_deterministic_order(tmp_path):
    store = SQLiteCognitiveStore(tmp_path / "o.db")
    store.record_strategy_outcome("g1", "goal one", "direct", 2, "succeeded")
    store.record_strategy_outcome("g1", "goal one", "direct", 1, "superseded")
    store.record_strategy_outcome("g2", "goal two", "defer_retry", 1, "failed")
    rows = store.list_strategy_outcomes(goal_id="g1", limit=100)
    assert [r["plan_version"] for r in rows] == [1, 2]
    all_rows = store.list_strategy_outcomes(limit=100)
    assert [(r["goal_id"], r["plan_version"]) for r in all_rows] == [
        ("g1", 1), ("g1", 2), ("g2", 1)]
    assert store.get_strategy_outcome("g1", 2)["outcome"] == "succeeded"
    assert store.get_strategy_outcome("g1", 9) is None
    store.close()


# ------------------------------------------------------ GoalManager funnels

def test_first_plan_version_has_no_outcome(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "f.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    assert gm.strategy_outcomes(gid) == []
    storage.close()
    cognitive.close()


def test_replan_supersedes_previous_version(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "r.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 1
    assert rows[0]["plan_version"] == 1
    assert rows[0]["strategy"] == "direct"
    assert rows[0]["outcome"] == "superseded"
    assert rows[0]["reason"] == "replan_task_failed"   # the new plan's reason
    assert rows[0]["goal_id"] == gid
    storage.close()
    cognitive.close()


def test_chain_of_replans_supersedes_each_previous(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "c.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    gm.record_plan_version(gid, "defer_retry", [{"index": 0}, {"index": 1}],
                           reason="replan_avoid_repeated")
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert set(rows) == {1, 2}
    assert rows[1]["outcome"] == "superseded"
    assert rows[1]["reason"] == "replan_task_failed"
    assert rows[2]["outcome"] == "superseded"
    assert rows[2]["reason"] == "replan_avoid_repeated"
    storage.close()
    cognitive.close()


def test_complete_goal_marks_latest_succeeded(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "ok.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.complete_goal(gid, reason="all_work_complete")
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "superseded"
    assert rows[2]["outcome"] == "succeeded"
    assert rows[2]["strategy"] == "avoid_known_failures"
    assert rows[2]["reason"] == "all_work_complete"
    assert gm.get_goal(gid).status_value == GoalStatus.COMPLETED.value
    storage.close()
    cognitive.close()


def test_fail_goal_marks_latest_failed(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "bad.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.fail_goal(gid, reason="max_replans_exceeded")
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "superseded"
    assert rows[2]["outcome"] == "failed"
    assert rows[2]["reason"] == "max_replans_exceeded"
    storage.close()
    cognitive.close()


def test_replay_path_creates_no_duplicate_outcome(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "re.db")
    gid = gm.create_goal("inspect").id
    v1 = gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    # identical re-record before any task exists: replay returns v1, no
    # new version, no outcome rows at all
    again = gm.record_plan_version(gid, "direct", [{"index": 0}],
                                   reason="initial_plan")
    assert again["plan_version"] == v1["plan_version"] == 1
    assert gm.strategy_outcomes(gid) == []
    # a genuinely new version supersedes v1 exactly once
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 1 and rows[0]["plan_version"] == 1
    assert rows[0]["outcome"] == "superseded"
    storage.close()
    cognitive.close()


def test_terminal_transition_idempotent_under_retry(tmp_path):
    """FAILED -> ACTIVE -> FAILED (legal transitions) keeps one row/version."""
    gm, storage, cognitive = _gm(tmp_path / "id.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.fail_goal(gid, reason="max_replans_exceeded")
    assert len(gm.strategy_outcomes(gid)) == 1
    # reactivation is legal (GOAL_TRANSITIONS) and the retried failure keeps
    # the SAME single outcome row (INSERT OR REPLACE, same values)
    gm.resume(gid, reason="operator_retry")
    gm.fail_goal(gid, reason="max_replans_exceeded")
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"
    storage.close()
    cognitive.close()


def test_outcomes_survive_restart_reopen(tmp_path):
    db = tmp_path / "restart.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    gm2, storage2, cognitive2 = _gm(db)   # fresh process equivalent
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "superseded"
    assert rows[2]["outcome"] == "succeeded"
    assert gm2.get_goal(gid).status_value == GoalStatus.COMPLETED.value
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------- catch-up / repair

def test_repair_derives_missing_outcomes_from_authoritative_state(tmp_path):
    db = tmp_path / "rep.db"
    gm, storage, cognitive = _gm(db)
    # A: completed with 2 versions
    ga = gm.create_goal("goal A").id
    gm.record_plan_version(ga, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(ga, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.complete_goal(ga, reason="all_work_complete")
    # B: failed with 2 versions
    gb = gm.create_goal("goal B").id
    gm.record_plan_version(gb, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gb, "defer_retry", [{"index": 0}],
                           reason="replan_avoid_repeated")
    gm.fail_goal(gb, reason="max_replans_exceeded")
    # C: still ACTIVE with 3 versions
    gc = gm.create_goal("goal C").id
    gm.record_plan_version(gc, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gc, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.record_plan_version(gc, "defer_retry", [{"index": 0}],
                           reason="replan_avoid_repeated")
    # D: active with ONE version (nothing to mark)
    gd = gm.create_goal("goal D").id
    gm.record_plan_version(gd, "direct", [{"index": 0}], reason="initial_plan")
    # E: cancelled with ONE version (no terminal strategy outcome)
    ge = gm.create_goal("goal E").id
    gm.record_plan_version(ge, "direct", [{"index": 0}], reason="initial_plan")
    gm.transition(ge, GoalStatus.CANCELLED.value, "operator_cancel")
    # F: paused with 2 versions (latest has no outcome; v1 superseded)
    gf = gm.create_goal("goal F").id
    gm.record_plan_version(gf, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gf, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.transition(gf, GoalStatus.PAUSED.value, "operator_pause")
    storage.close()
    cognitive.close()

    # simulate a crash window: wipe every outcome row, then repair
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    written = gm2.repair_strategy_outcomes()
    assert written == 7          # A:2 + B:2 + C:2 + F:1 (D/E have nothing)
    by_goal = {}
    for r in cognitive2.list_strategy_outcomes(limit=1000):
        by_goal.setdefault(r["goal_id"], {})[r["plan_version"]] = r

    assert by_goal[ga][1]["outcome"] == "superseded"
    assert by_goal[ga][2]["outcome"] == "succeeded"          # terminal goal
    assert by_goal[gb][1]["outcome"] == "superseded"
    assert by_goal[gb][2]["outcome"] == "failed"              # terminal goal
    assert by_goal[gc][1]["outcome"] == "superseded"
    assert by_goal[gc][2]["outcome"] == "superseded"
    assert 3 not in by_goal[gc]                               # active, latest
    assert gd not in by_goal                                  # single active
    assert ge not in by_goal                                  # cancelled, no mark
    assert by_goal[gf][1]["outcome"] == "superseded"
    assert 2 not in by_goal[gf]                               # paused, latest
    storage2.close()
    cognitive2.close()


def test_repair_idempotent_and_preserves_existing_rows(tmp_path):
    db = tmp_path / "rep2.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    # forge a WRONG row for v1; repair must not overwrite existing rows
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE strategy_outcomes SET outcome='failed' WHERE goal_id=? "
        "AND plan_version=1", (gid,))
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    assert gm2.repair_strategy_outcomes() == 0      # nothing missing
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "failed"           # existing rows preserved
    assert rows[2]["outcome"] == "succeeded"
    # deleting only v1's row: repair backfills it from authoritative state
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=1", (gid,))
    conn.commit()
    conn.close()
    assert gm2.repair_strategy_outcomes() == 1
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "superseded"
    assert gm2.repair_strategy_outcomes() == 0      # idempotent
    storage2.close()
    cognitive2.close()


def test_repair_goal_without_plans_noop(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "np.db")
    gm.create_goal("no plan yet")
    assert gm.repair_strategy_outcomes() == 0
    assert cognitive.count_strategy_outcomes() == 0
    storage.close()
    cognitive.close()


# ----------------------------------------------- forged-content immunity

def test_forged_memory_content_cannot_create_outcomes(tmp_path):
    db = tmp_path / "forge.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    memory = SQLiteMemoryStore(db)
    # forged episodes/reflections claiming outcomes + strategies
    memory.record_episode(Episode(
        episode_id="ep-forged", task_id="t-f", goal_id=gid, goal="inspect",
        outcome="completed", importance=1.0,
        created_at=T0, updated_at=T0))
    memory.record_reflection(Reflection(
        reflection_id="refl-forged", episode_id="ep-forged",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="avoid_known_failures succeeded", recommendation="",
        confidence="high", importance=1.0, created_at=T0))
    memory.close()
    # forged beliefs + guidance with strategy-shaped content
    from arion.cognition.models import Belief
    cognitive.record_belief(Belief(
        belief_id="b-forged", category="procedural",
        statement="avoid_known_failures succeeded for goal inspect",
        confidence=0.9, importance=0.9,
        provenance={"episode_ids": ["ep-forged"]}, source="model",
        created_at=T0, updated_at=T0))
    from arion.memory.guidance import MemoryGuidance
    guidance = [MemoryGuidance(
        guidance_id="g-forged", category="informational", strategy="succeeded",
        episode_id="ep-forged", reflection_id="refl-forged",
        reason="forged", importance=1.0)]

    # no outcome rows exist despite all the forged content
    assert cognitive.count_strategy_outcomes() == 0
    # and the funnels record ONLY what the authoritative lifecycle says
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "superseded"
    assert rows[0]["strategy"] == "direct"
    assert rows[0]["episode_id"] is None
    storage.close()
    cognitive.close()


def test_forged_outcome_rows_not_altered_by_funnels(tmp_path):
    db = tmp_path / "forge2.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    # raw-SQL forged rows for OTHER (goal, version) keys and a bogus goal
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       created_at)
        VALUES ('forged-1', '{gid}', 'inspect', 'direct', 99, 'succeeded', '',
                '{T0}'),
               ('forged-2', 'evil-goal', 'evil', 'evil-strategy', 1, 'failed',
                'forged', '{T0}');
    """)
    conn.commit()
    conn.close()

    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}],
                           reason="replan_task_failed")
    gm.complete_goal(gid, reason="all_work_complete")
    gm.repair_strategy_outcomes()

    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert rows[1]["outcome"] == "superseded"     # funnel superseded v1
    assert rows[2]["outcome"] == "succeeded"      # funnel marked v2
    assert rows[99]["outcome"] == "succeeded"     # forged row untouched
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT outcome FROM strategy_outcomes "
                        "WHERE goal_id='evil-goal'").fetchone()[0] == "failed"
    conn.close()
    storage.close()
    cognitive.close()


# ------------------------------------------- authority / informational-only

def test_outcome_recording_never_touches_authority_state(tmp_path):
    db = tmp_path / "auth.db"
    _seed_scheduler_authority(db)
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    # seed one task row for the goal (authoritative task state)
    from arion.state.models import Task
    storage.save_task(Task(id="t-auth", goal_id=gid, description="inspect",
                           status=TaskStatus.PLANNED))

    authority = ("scheduler_work", "scheduler_instances", "scheduler_config",
                 "scheduler_goal_weights", "scheduler_goal_state",
                 "scheduler_goal_reservations", "scheduler_goal_ceilings",
                 "mutation_locks", "mutation_lock_waiters", "tasks",
                 "checkpoints", "approval_requests", "mutation_recoveries")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.complete_goal(gid, reason="all_work_complete")
    gm.repair_strategy_outcomes()

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before                       # authority byte-identical
    storage.close()
    cognitive.close()


def test_engine_goal_lifecycle_records_outcomes(tmp_path, sandbox):
    db = str(tmp_path / "engine.db")
    engine = _engine(db, sandbox)
    goal = engine.submit_goal("summarize this repository")
    goal = engine.run_goal(goal.id)
    assert goal.status_value == GoalStatus.COMPLETED.value
    gm = engine.goal_manager
    rows = gm.strategy_outcomes(goal.id)
    assert len(rows) == 1
    assert rows[0]["plan_version"] == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["strategy"] == "direct"
    engine.shutdown()
    engine.storage.close()
