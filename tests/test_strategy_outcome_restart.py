"""Strategy-outcome restart/crash + adversarial hardening (ADR-015 addendum,
Phase D) - tests first.

Authority model (kept explicit, never broadened):

    goal_state + goal_plans  -> AUTHORITATIVE inputs to outcome repair
    strategy_outcomes, episodes, reflections, consolidations, guidance,
    beliefs, planner output, telemetry  -> INFORMATIONAL

D1  restart persistence: outcomes survive store/GoalManager reopen;
    listing/order/counts/created_at deterministic.
D2  crash window (transition committed, outcome missing): repair
    reconstructs the missing row from goal_state + goal_plans only;
    idempotent; no duplicate events; authoritative state unchanged.
D3  crash window (replan committed, supersede missing): repair
    reconstructs from plan-version ordering; same invariants.
D4  partial/corrupt state: fail closed; repair never trusts
    episodes/reflections/telemetry/events; valid rows never overwritten.
D6  ADR-014 prune interaction: prune_goal_plans removes coupled outcome
    rows; latest protected; dry-run byte-identical; idempotent; restart
    consistent; repair does not resurrect pruned history (unless a
    remaining plan version's authoritative state requires it).
D7  adversarial authority boundary: forged rows/events/episodes/planner
    metadata/scheduler telemetry cannot create or change authoritative
    goal/scheduler state, ownership, weights, reservations, ceilings,
    DWRR credit, or admission.

All timestamps fixed; no wall clock in any assertion.
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
from arion.cognition.strategy import StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode, Reflection
from arion.memory.store import ConsolidationRecord, SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"
FS = "filesystem:path"


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


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


def _sql_seed_goal(db, gid, description, status, strategy="direct",
                   version=1, plans=(), last_replan_reason=None):
    """Seed ONE authoritative goal + its plan versions via raw SQL
    (the exact post-crash state: authority committed, no outcomes)."""
    _st = SQLiteStorage(db)      # state schema (goals)
    _st.close()
    _c = SQLiteCognitiveStore(db)  # cognition schema (goal_plans)
    _c.close()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO goals (id, description, source, status, version, "
        "strategy, blockers, progress_metadata, last_evaluated_at, "
        "last_replan_reason, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, description, "test", status, version, strategy, "[]", "{}",
         None, last_replan_reason, T0, _iso_plus(T0, 30)))
    for pv, pstrategy, reason in plans:
        conn.execute(
            "INSERT INTO goal_plans (goal_id, plan_version, strategy, "
            "plan_summary, reason, created_at) VALUES (?,?,?,?,?,?)",
            (gid, pv, pstrategy, json.dumps([{"index": pv - 1}]), reason,
             _iso_plus(T0, pv * 10)))
    conn.commit()
    conn.close()


def _outcome_events(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT detail FROM audit_events WHERE kind='strategy.outcome'"
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def _dump(db, tables):
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                for t in tables}
    finally:
        conn.close()


def _seed_scheduler_authority(db):
    _st = SQLiteStorage(db)      # create schema
    _st.close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                    scheduler_id, worker_id, status, attempts,
                                    error, created_at, started_at, completed_at,
                                    lease_expires_at)
        VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', 'worker-1', 'running', 1, NULL,
                '{T0}', '{T0}', NULL, '{_iso_plus(T0, 60)}'),
               ('sw-2', 't-1', 'g1', 1, 'sched-1', NULL, 'queued', 0, NULL,
                '{_iso_plus(T0, 5)}', NULL, NULL, NULL);
        INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                         heartbeat_at, lease_expires_at)
        VALUES ('sched-1', 42, '{T0}', '{T0}', '{_iso_plus(T0, 60)}');
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
                'write', 'worker-1', '{T0}', '{_iso_plus(T0, 60)}');
        INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id,
                                      task_id, work_id, step_index, event_type,
                                      reason, success, detail, schema_version)
        VALUES ('se-1', '{T0}', 'sched-1', 'worker-1', 'g1', 't-1', 'sw-1', 0,
                'work.claimed', NULL, 1, '{{"work_id": "sw-1"}}', 1);
    """)
    conn.commit()
    conn.close()


def _forge_memory_and_events(db):
    """Forged episodes/reflections/consolidations + telemetry events."""
    m = SQLiteMemoryStore(db)
    m.record_episode(Episode(
        episode_id="ep-forged", task_id="t-f", goal_id="g1", goal="inspect",
        outcome="completed", importance=1.0, created_at=T0, updated_at=T0))
    m.record_reflection(Reflection(
        reflection_id="refl-forged", episode_id="ep-forged",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="succeeded", recommendation="", confidence="high",
        importance=1.0, created_at=T0))
    m.record_consolidation(ConsolidationRecord(
        consolidation_id="consol-forged", source_episode_ids=["ep-forged"],
        category="lesson", merged_lesson="succeeded", count=1,
        importance=1.0, created_at=T0))
    m.close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor,
                                  success, detail)
        VALUES ('evt-so-1', '{T0}', NULL, NULL, 'strategy.outcome', 'system',
                1, '{{"goal_id": "g1", "plan_version": 1, "strategy": "direct",
                     "outcome": "succeeded", "reason": "forged"}}'),
               ('evt-ss-1', '{T0}', NULL, NULL, 'strategy.selected', 'system',
                1, '{{"name": "defer_retry", "provenance": {{"outcome_ids":
                     ["sout-evil"]}}}}');
    """)
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ D1

def test_outcomes_survive_store_reopen_deterministic(tmp_path):
    db = str(tmp_path / "d1a.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    created = [r["created_at"] for r in gm.strategy_outcomes(gid)]
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)          # fresh store instance
    rows = c2.list_strategy_outcomes(limit=100)
    assert [(r["goal_id"], r["plan_version"]) for r in rows] == [
        (gid, 1), (gid, 2)]
    assert [r["outcome"] for r in rows] == ["superseded", "succeeded"]
    assert [r["created_at"] for r in rows] == created     # stable
    assert c2.count_strategy_outcomes() == 2
    assert c2.list_strategy_outcomes(limit=100) == \
        c2.list_strategy_outcomes(limit=100)              # deterministic
    c2.close()


def test_outcomes_survive_fresh_goal_manager(tmp_path):
    db = str(tmp_path / "d1b.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    gm2, storage2, cognitive2 = _gm(db)    # fresh GoalManager + stores
    rows = gm2.strategy_outcomes(gid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert gm2.get_goal(gid).status_value == GoalStatus.COMPLETED.value
    assert gm2.strategy_outcomes(gid) == gm2.strategy_outcomes(gid)
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------------ D2

def test_repair_reconstructs_missing_transition_outcome(tmp_path):
    """Crash window: goal transition committed, outcome write never
    happened. Repair derives the missing rows from goal_state+goal_plans."""
    db = str(tmp_path / "d2.db")
    _sql_seed_goal(
        db, "g-done", "inspect", GoalStatus.COMPLETED.value, strategy="capability_verified",
        version=2,
        plans=[(1, "direct", "initial_plan"),
               (2, "capability_verified", "replan_world_changed")])
    gm, storage, _ = _gm(db)
    written = gm.repair_strategy_outcomes()
    assert written == 2                      # exactly one per missing version
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes("g-done")}
    assert set(rows) == {1, 2}
    assert rows[1]["strategy"] == "direct"
    assert rows[1]["outcome"] == "superseded"
    assert rows[1]["reason"] == "replan_world_changed"   # next plan's reason
    assert rows[2]["strategy"] == "capability_verified"
    assert rows[2]["outcome"] == "succeeded"
    assert rows[2]["reason"] == "all_work_complete"
    storage.close()


def test_repair_idempotent_no_duplicate_events_state_unchanged(tmp_path):
    db = str(tmp_path / "d2b.db")
    _sql_seed_goal(
        db, "g-done", "inspect", GoalStatus.COMPLETED.value,
        version=2,
        plans=[(1, "direct", "initial_plan"),
               (2, "capability_verified", "replan_world_changed")])
    authority_before = _dump(db, ("goals", "goal_plans"))
    gm, storage, _ = _gm(db)
    assert gm.repair_strategy_outcomes() == 2
    assert gm.repair_strategy_outcomes() == 0          # idempotent
    assert len(_outcome_events(db)) == 2               # one event per row, no dups
    assert _dump(db, ("goals", "goal_plans")) == authority_before
    storage.close()


# ------------------------------------------------------------------ D3

def test_repair_reconstructs_missing_supersede_from_ordering(tmp_path):
    """Crash window: replan committed, previous-version supersede absent.
    Repair derives superseded rows purely from plan-version ordering."""
    db = str(tmp_path / "d3.db")
    _sql_seed_goal(
        db, "g-act", "inspect", GoalStatus.ACTIVE.value, strategy="defer_retry",
        version=1,
        plans=[(1, "direct", "initial_plan"),
               (2, "avoid_known_failures", "replan_task_failed"),
               (3, "defer_retry", "replan_avoid_repeated")])
    gm, storage, _ = _gm(db)
    written = gm.repair_strategy_outcomes()
    assert written == 2                        # v1, v2 superseded; v3 active
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes("g-act")}
    assert set(rows) == {1, 2}
    assert rows[1]["outcome"] == "superseded"
    assert rows[1]["reason"] == "replan_task_failed"
    assert rows[2]["outcome"] == "superseded"
    assert rows[2]["reason"] == "replan_avoid_repeated"
    assert gm.repair_strategy_outcomes() == 0
    assert len(_outcome_events(db)) == 2       # no duplicate events
    storage.close()


# ------------------------------------------------------------------ D4

def test_repair_skips_unknown_strategy_fail_closed(tmp_path):
    db = str(tmp_path / "d4a.db")
    _sql_seed_goal(
        db, "g-evil", "inspect", GoalStatus.COMPLETED.value, version=1,
        plans=[(1, "evil_strategy", "initial_plan"),
               (2, "direct", "replan_x")])
    gm, storage, _ = _gm(db)
    assert gm.repair_strategy_outcomes() == 1  # only the valid strategy row
    rows = gm.strategy_outcomes("g-evil")
    assert len(rows) == 1 and rows[0]["plan_version"] == 2
    assert rows[0]["outcome"] == "succeeded"
    storage.close()


def test_repair_survives_malformed_plan_versions(tmp_path):
    db = str(tmp_path / "d4b.db")
    _sql_seed_goal(
        db, "g-bad", "inspect", GoalStatus.ACTIVE.value, version=1,
        plans=[(1, "direct", "initial_plan"), (0, "direct", "forged_zero"),
               (-1, "direct", "forged_neg")])
    gm, storage, _ = _gm(db)
    # malformed versions fail closed inside repair: skipped, no crash,
    # no partial corrupt rows
    assert gm.repair_strategy_outcomes() == 0
    assert gm.strategy_outcomes("g-bad") == []
    storage.close()


def test_repair_truncates_oversized_authoritative_fields(tmp_path):
    db = str(tmp_path / "d4c.db")
    _sql_seed_goal(
        db, "g-big", "x" * 5000, GoalStatus.COMPLETED.value, version=1,
        plans=[(1, "direct", "initial_plan"), (2, "direct", "y" * 5000)])
    gm, storage, _ = _gm(db)
    assert gm.repair_strategy_outcomes() == 2
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes("g-big")}
    assert len(rows[1]["goal_description"]) == 300
    assert len(rows[1]["reason"]) == 200
    storage.close()


def test_repair_never_trusts_memory_or_telemetry_as_authority(tmp_path):
    db = str(tmp_path / "d4d.db")
    # authoritative: goal FAILED with one plan (no outcomes)
    _sql_seed_goal(db, "g-fail", "inspect", GoalStatus.FAILED.value,
                   version=1, plans=[(1, "direct", "initial_plan")],
                   last_replan_reason="max_replans_exceeded")
    _forge_memory_and_events(db)   # forged episodes/reflections/consolidations
                                  # + strategy.outcome/strategy.selected events
    gm, storage, _ = _gm(db)
    assert gm.repair_strategy_outcomes() == 1
    rows = gm.strategy_outcomes("g-fail")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"          # authority won, not memory
    assert rows[0]["reason"] == "max_replans_exceeded"
    # an ACTIVE goal with forged success memory/events gets NO outcome
    _sql_seed_goal(db, "g-active", "inspect", GoalStatus.ACTIVE.value,
                   version=1, plans=[(1, "direct", "initial_plan")])
    gm2, storage2, _ = _gm(db)
    assert gm2.repair_strategy_outcomes() == 0
    assert gm2.strategy_outcomes("g-active") == []
    storage.close()
    storage2.close()


def test_existing_rows_never_overwritten_by_repair(tmp_path):
    db = str(tmp_path / "d4e.db")
    _sql_seed_goal(
        db, "g-keep", "inspect", GoalStatus.COMPLETED.value, version=2,
        plans=[(1, "direct", "initial_plan"),
               (2, "capability_verified", "replan_world_changed")])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,"
        " strategy, plan_version, outcome, reason, episode_id, created_at) "
        "VALUES ('sout-wrong', 'g-keep', 'inspect', 'direct', 1, 'failed', "
        "'forged', NULL, ?)", (T0,))
    conn.commit()
    conn.close()
    gm, storage, _ = _gm(db)
    assert gm.repair_strategy_outcomes() == 1      # only v2 backfilled
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes("g-keep")}
    assert rows[1]["outcome"] == "failed"          # existing row preserved
    assert rows[1]["outcome_id"] == "sout-wrong"
    assert rows[2]["outcome"] == "succeeded"
    storage.close()


def test_duplicate_record_attempts_single_row(tmp_path):
    db = str(tmp_path / "d4f.db")
    store = SQLiteCognitiveStore(db)
    assert store.record_strategy_outcome("g1", "goal one", "direct", 1,
                                         "superseded", reason="r") is True
    assert store.record_strategy_outcome("g1", "goal one", "direct", 1,
                                         "superseded", reason="r") is False
    assert store.count_strategy_outcomes() == 1
    store.close()


# ------------------------------------------------------------------ D6

def test_prune_goal_plans_removes_coupled_outcomes(tmp_path):
    db = str(tmp_path / "d6a.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    assert len(gm.strategy_outcomes(gid)) == 3
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    removed = c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    assert removed == 2                          # v1, v2 plans pruned
    assert [p["plan_version"] for p in c2.list_goal_plans(gid)] == [3]
    rows = c2.list_strategy_outcomes(goal_id=gid)
    assert [r["plan_version"] for r in rows] == [3]   # coupled outcomes gone
    assert rows[0]["outcome"] == "succeeded"     # latest outcome protected
    assert c2.latest_goal_plan(gid)["plan_version"] == 3
    c2.close()


def test_prune_goal_plans_dry_run_outcome_byte_identical(tmp_path):
    db = str(tmp_path / "d6b.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    before = _dump(db, ("goal_plans", "strategy_outcomes"))

    c2 = SQLiteCognitiveStore(db)
    would = c2.prune_goal_plans(goal_id=gid, keep_latest=1, dry_run=True)
    assert would == 1
    assert _dump(db, ("goal_plans", "strategy_outcomes")) == before
    c2.close()


def test_prune_coupled_outcomes_idempotent_and_restart_consistent(tmp_path):
    db = str(tmp_path / "d6c.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=1) == 1
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=1) == 0  # idempotent
    c2.close()

    # restart after prune: consistent, and repair does NOT resurrect the
    # intentionally pruned historical plan/outcome
    gm2, storage2, cognitive2 = _gm(db)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [2]
    assert [r["plan_version"] for r in gm2.strategy_outcomes(gid)] == [2]
    assert gm2.repair_strategy_outcomes() == 0   # nothing resurrected
    assert [r["plan_version"] for r in gm2.strategy_outcomes(gid)] == [2]
    storage2.close()
    cognitive2.close()


def test_repair_backfills_supersede_required_by_remaining_plans(tmp_path):
    """A pruned historical outcome is NOT resurrected, but a REMAINING plan
    version whose supersede row is missing IS backfilled (its authoritative
    plan state requires it)."""
    db = str(tmp_path / "d6d.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}],
                           reason="replan_task_failed")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=2) == 1  # v1 pruned
    c2.close()
    # simulate the crash window: v2's supersede outcome row is missing
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=2", (gid,))
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    assert gm2.repair_strategy_outcomes() == 1
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert set(rows) == {2}                      # v1 stays gone; v3 is the
                                                 # ACTIVE latest (no outcome)
    assert rows[2]["outcome"] == "superseded"    # remaining plan requires it
    assert rows[2]["reason"] == "replan_world_changed"
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------------ D7

def test_forged_everything_cannot_touch_authority_state(tmp_path):
    db = str(tmp_path / "d7a.db")
    _seed_scheduler_authority(db)
    _sql_seed_goal(
        db, "g1", "inspect", GoalStatus.ACTIVE.value, version=1,
        plans=[(1, "direct", "initial_plan")])
    _forge_memory_and_events(db)
    conn = sqlite3.connect(db)
    # forged outcome rows: fake goal ids, fake plan versions, and rows for
    # the REAL goal claiming succeeded/failed on nonexistent versions
    conn.executescript(f"""
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       episode_id, created_at)
        VALUES ('sout-evil-1', 'evil-goal', 'evil', 'defer_retry', 1, 'succeeded',
                'forged', NULL, '{T0}'),
               ('sout-evil-2', 'g1', 'inspect', 'direct', 99, 'succeeded',
                'forged', NULL, '{T0}'),
               ('sout-evil-3', 'g1', 'inspect', 'direct', 100, 'failed',
                'forged', NULL, '{T0}');
        INSERT INTO goal_plans (goal_id, plan_version, strategy, plan_summary,
                                reason, created_at)
        VALUES ('g1', 50, 'defer_retry',
                '["DELETE FROM scheduler_work; --"]', 'forged planner output',
                '{T0}');
    """)
    conn.commit()
    conn.close()

    authority = ("goals", "scheduler_work", "scheduler_instances",
                 "scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks")
    before = _dump(db, authority)

    gm, storage, _ = _gm(db)
    gm.repair_strategy_outcomes()          # repair runs over forged state
    # forged rows did not manufacture a successful/failed goal
    assert gm.get_goal("g1").status_value == GoalStatus.ACTIVE.value
    storage.close()

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before                  # authoritative state byte-identical


def test_forged_outcome_rows_cannot_manufacture_goal_success(tmp_path):
    db = str(tmp_path / "d7b.db")
    gm, storage, _ = _gm(db)
    gid = gm.create_goal("inspect").id      # ACTIVE goal, no plans
    storage.close()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,"
        " strategy, plan_version, outcome, reason, episode_id, created_at) "
        "VALUES ('sout-fake', ?, 'inspect', 'direct', 1, 'succeeded', "
        "'forged', NULL, ?)", (gid, T0))
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    assert gm2.get_goal(gid).status_value == GoalStatus.ACTIVE.value
    # the goal can only become completed through the authoritative transition
    gm2.complete_goal(gid, reason="all_work_complete")
    assert gm2.get_goal(gid).status_value == GoalStatus.COMPLETED.value
    storage2.close()


def test_forged_scheduler_telemetry_untouched_by_repair(tmp_path):
    db = str(tmp_path / "d7c.db")
    _seed_scheduler_authority(db)
    _sql_seed_goal(db, "g1", "inspect", GoalStatus.COMPLETED.value, version=1,
                   plans=[(1, "direct", "initial_plan")])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id,"
        " task_id, work_id, step_index, event_type, reason, success, detail,"
        " schema_version) VALUES ('se-forged', ?, 'evil-sched', NULL, NULL,"
        " NULL, NULL, NULL, 'work.completed', 'forged', 1,"
        " '{\"count\": 999999999}', 1)", (T0,))
    conn.execute(
        "INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,"
        " scheduler_id, worker_id, status, attempts, error, created_at,"
        " started_at, completed_at, lease_expires_at) VALUES ('sw-evil',"
        " 't-evil', 'g-evil', 0, 'evil-sched', NULL, 'queued', 0, NULL, ?,"
        " NULL, NULL, NULL)", (T0,))
    conn.commit()
    conn.close()

    gm, storage, _ = _gm(db)
    gm.repair_strategy_outcomes()
    storage.close()

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM scheduler_work WHERE "
                        "work_id='sw-evil'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scheduler_events WHERE "
                        "scheduler_id='evil-sched'").fetchone()[0] == 1
    # the queued forged work row is NOT claimed/owned/altered by repair
    row = conn.execute("SELECT status, worker_id, lease_expires_at "
                       "FROM scheduler_work WHERE work_id='sw-evil'").fetchone()
    assert row == ("queued", None, None)
    conn.close()


def test_engine_goal_lifecycle_still_records_outcomes_after_forging(tmp_path, sandbox):
    """End-to-end: a real engine run after forged state records the correct
    outcome and leaves scheduler admission untouched."""
    db = str(tmp_path / "d7d.db")
    _seed_scheduler_authority(db)
    _c = SQLiteCognitiveStore(db)   # cognition schema (strategy_outcomes)
    _c.close()
    _forge_memory_and_events(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,"
        " strategy, plan_version, outcome, reason, episode_id, created_at) "
        "VALUES ('sout-evil', 'evil-goal', 'evil', 'direct', 1, 'succeeded',"
        " 'forged', NULL, ?)", (T0,))
    conn.commit()
    conn.close()

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db)
    cognitive = SQLiteCognitiveStore(db)
    from arion.cognition.state import CognitiveState
    from arion.cognition.world_state import WorldStateMonitor

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
        memory=memory, reflector=None,
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    goal = engine.submit_goal("summarize this repository")
    goal = engine.run_goal(goal.id)
    assert goal.status_value == GoalStatus.COMPLETED.value
    rows = gm.strategy_outcomes(goal.id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["strategy"] == "direct"
    engine.shutdown()
    storage.close()
    # forged row still untouched; no scheduler work created for evil-goal
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT outcome FROM strategy_outcomes WHERE "
                        "goal_id='evil-goal'").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM scheduler_work WHERE "
                        "goal_id='g-evil'").fetchone()[0] == 0
    conn.close()
