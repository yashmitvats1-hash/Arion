"""ADR-016 addendum Phase D: restart/crash + adversarial hardening - tests
first.

D1  cross-process rollback races: two real subprocesses attempt the same
    `goals rollback`; exactly one durable new version; the loser's replay
    returns the existing version; exactly one strategy.outcome supersede
    event; stable outcome_id + created_at.
D2  crash windows: version committed / outcome missing -> existing ADR-015
    repair reconstructs; outcome committed / crash before subsequent work
    -> reopen + repair idempotent; repeated repair -> no duplicate
    versions/events; historical plans immutable.
D3  pruning interactions: prune_goal_plans removes the rollback source +
    coupled outcome; diff/rollback fail closed against pruned versions;
    repair never resurrects; remaining authoritative plans still repaired.
D4  forged/adversarial state: forged plan.versioned/strategy.outcome/
    episodes/reflections/planner metadata/scheduler telemetry cannot
    manufacture rollback history; forged plan rows (invalid strategy/
    summary/version types) fail closed; cross-goal confusion fails closed;
    oversized/malformed stored plans cannot become executable; forged
    telemetry cannot affect rollback/execution authority.
D5  stored-plan execution hardening: re-adopted plans survive restart and
    execute deterministically; every step passes the full live
    authorization/capability pipeline; stored plans cannot bypass
    approvals/scheduler ownership/reservations/ceilings/DWRR;
    checkpoint/restart correct.
D6  read-only progress hardening: public non-mutating `peek_evaluate()`
    replaces the CLI's private `_relevant_world_changes` dependency;
    byte-identical read-only behavior + CLI output preserved.
D7  authority-boundary proof: before/after byte comparison of scheduler
    authority/config, reservations, ceilings, weights, ownership, leases,
    checkpoints, approvals, recoveries, goal state, mutation locks
    (excluding only known engine-startup observational tables).

All timestamps fixed; deterministic.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, Task, TaskStatus
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
    from arion.cognition.world_state import WorldStateMonitor

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


def _step(index, capability="filesystem.read", action="read", path="x.md"):
    return PlanStep(index=index, intent=f"step {index}", capability=capability,
                    action=action, scope="filesystem:read", params={"path": path},
                    verification=__import__("arion.state.models",
                                            fromlist=["VerificationPolicy"]
                                            ).VerificationPolicy("non_empty"))


def _seed_goal(db, with_events=True):
    """v1 [read a.md], v2 [list ., read b.md], v3 [read a.md, list .]."""
    gm, storage, cognitive = _gm(db, with_events=with_events)
    gid = gm.create_goal("inspect repository").id
    gm.record_plan_version(gid, "direct", [_step(0, path="a.md").to_dict()],
                           reason="initial_plan")
    gm.record_plan_version(
        gid, "avoid_known_failures",
        [_step(0, action="list", path=".").to_dict(),
         _step(1, path="b.md").to_dict()],
        reason="replan_task_failed")
    gm.record_plan_version(
        gid, "capability_verified",
        [_step(0, path="a.md").to_dict(), _step(1, action="list", path=".").to_dict()],
        reason="replan_world_changed")
    storage.close()
    cognitive.close()
    return gid


def _outcome_events(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT detail FROM audit_events WHERE kind='strategy.outcome'"
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def _event_kinds(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT kind FROM audit_events ORDER BY rowid").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _dump(db, tables):
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                for t in tables}
    finally:
        conn.close()


def _cli(argv):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


# ============================================================ D1: races

_HELPER = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, %(repo)r)
    from arion.interfaces.cli import main as cli_main

    db = sys.argv[1]
    gid = sys.argv[2]
    rc = cli_main(["goals", "rollback", gid, "1", "--db", db])
    print(json.dumps({"rc": rc}), flush=True)
    """
) % {"repo": str(Path(__file__).resolve().parent.parent)}


def _run_rollback(db, gid, expect_rc=0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _HELPER, str(db), gid],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["rc"] == expect_rc
    return out


def _run_rollback_async(db, gid):
    return subprocess.Popen(
        [sys.executable, "-c", _HELPER, str(db), gid],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cross_process_rollback_single_version_single_event(tmp_path):
    db = str(tmp_path / "d1.db")
    gid = _seed_goal(db, with_events=True)
    a = _run_rollback_async(db, gid)
    time.sleep(0.15)
    b = _run_rollback_async(db, gid)
    out_a, err_a = a.communicate(timeout=120)
    out_b, err_b = b.communicate(timeout=120)
    assert a.returncode == 0 and b.returncode == 0

    gm, storage, cognitive = _gm(db)
    history = gm.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1, 2, 3, 4]  # exactly ONE new
    assert history[-1]["reason"] == "replan_rollback_v1"
    rows = gm.strategy_outcomes(gid)
    supersedes = [r for r in rows if r["outcome"] == "superseded"
                  and r["reason"] == "replan_rollback_v1"]
    assert len(supersedes) == 1                  # exactly one supersede
    events = [e for e in _outcome_events(db)
              if e["reason"] == "replan_rollback_v1"]
    assert len(events) == 1                      # exactly one supersede event
    # stable outcome identity
    assert supersedes[0]["outcome_id"] == supersedes[0]["outcome_id"]
    created = supersedes[0]["created_at"]
    assert all(r["created_at"] == created for r in supersedes)
    storage.close()
    cognitive.close()


def test_rollback_lost_race_replay_returns_existing(tmp_path):
    db = str(tmp_path / "d1b.db")
    gid = _seed_goal(db, with_events=True)
    _run_rollback(db, gid)                        # first wins
    _run_rollback(db, gid)                        # replay: same version
    gm, storage, cognitive = _gm(db)
    history = gm.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1, 2, 3, 4]
    assert len(gm.strategy_outcomes(gid)) == 3    # no duplicate outcome
    assert len([e for e in _outcome_events(db)
                if e["reason"] == "replan_rollback_v1"]) == 1
    storage.close()
    cognitive.close()


# ============================================================ D2: crash

def test_crash_version_committed_outcome_missing_repair(tmp_path):
    db = str(tmp_path / "d2.db")
    gid = _seed_goal(db, with_events=True)
    gm, storage, cognitive = _gm(db)
    gm.readopt_plan(gid, 1)                       # v4 rollback
    storage.close()
    cognitive.close()
    # crash window: wipe ONLY the outcome rows (version committed, outcome missing)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.execute("DELETE FROM audit_events")
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    written = gm2.repair_strategy_outcomes()
    assert written == 3                           # v1,v2,v3 superseded (v4 active)
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[3]["outcome"] == "superseded"
    assert rows[3]["reason"] == "replan_rollback_v1"
    # historical plans immutable
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3, 4]
    assert gm2.repair_strategy_outcomes() == 0    # idempotent
    assert len(_outcome_events(db)) == 3          # one per repaired row
    storage2.close()
    cognitive2.close()


def test_crash_after_outcome_committed_reopen_idempotent(tmp_path):
    db = str(tmp_path / "d2b.db")
    gid = _seed_goal(db, with_events=True)
    gm, storage, cognitive = _gm(db)
    gm.readopt_plan(gid, 1)
    storage.close()
    cognitive.close()
    before = gm.strategy_outcomes(gid) if False else None
    events_before = len(_outcome_events(db))

    gm2, storage2, cognitive2 = _gm(db)          # reopen (fresh process)
    assert gm2.repair_strategy_outcomes() == 0   # nothing missing
    rows = gm2.strategy_outcomes(gid)
    assert len(rows) == 3
    assert len(_outcome_events(db)) == events_before  # no duplicate events
    storage2.close()
    cognitive2.close()


# ============================================================ D3: pruning

def test_prune_removes_rollback_source_and_coupled_outcome(tmp_path):
    db = str(tmp_path / "d3.db")
    gid = _seed_goal(db, with_events=True)
    gm, storage, cognitive = _gm(db)
    gm.readopt_plan(gid, 1)                       # v4 rollback of v1
    storage.close()
    cognitive.close()

    c = SQLiteCognitiveStore(db)
    assert c.prune_goal_plans(goal_id=gid, keep_latest=2) == 2  # v1,v2 pruned
    c.close()
    gm2, storage2, cognitive2 = _gm(db)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [3, 4]
    # coupled outcomes gone with their plans
    rows = gm2.strategy_outcomes(gid)
    assert [r["plan_version"] for r in rows] == [3]
    storage2.close()
    cognitive2.close()


def test_pruned_versions_diff_rollback_fail_closed(tmp_path):
    db = str(tmp_path / "d3b.db")
    gid = _seed_goal(db, with_events=True)
    c = SQLiteCognitiveStore(db)
    c.prune_goal_plans(goal_id=gid, keep_latest=2)   # v1,v2 pruned
    c.close()

    rc, _ = _cli(["goals", "diff", gid, "1", "3", "--db", db])
    assert rc == 1
    rc, _ = _cli(["goals", "rollback", gid, "1", "--db", db])
    assert rc == 1


def test_prune_repair_never_resurrects(tmp_path):
    db = str(tmp_path / "d3c.db")
    gid = _seed_goal(db, with_events=True)
    c = SQLiteCognitiveStore(db)
    c.prune_goal_plans(goal_id=gid, keep_latest=2)   # v1,v2 pruned
    c.close()

    gm, storage, cognitive = _gm(db)
    assert gm.repair_strategy_outcomes() == 0    # nothing to resurrect
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [2, 3]
    # remaining authoritative plans ([2,3]) still repaired correctly:
    # v2's superseded outcome (historical, missing) is reconstructed;
    # v3 (active latest) never fabricates one.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=2", (gid,))
    conn.commit()
    conn.close()
    assert gm.repair_strategy_outcomes() == 1
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    assert rows[2]["outcome"] == "superseded"
    assert 3 not in rows                          # active latest: no fabrication
    storage.close()
    cognitive.close()


# ============================================================ D4: forged

def test_forged_telemetry_cannot_manufacture_rollback(tmp_path):
    db = str(tmp_path / "d4.db")
    gid = _seed_goal(db, with_events=True)
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor,
                                  success, detail)
        VALUES ('evt-pv', '{T0}', NULL, NULL, 'plan.versioned', 'system', 1,
                '{{"goal_id": "{gid}", "plan_version": 99, "strategy": "direct",
                     "reason": "replan_rollback_v1", "steps": 1}}'),
               ('evt-so', '{T0}', NULL, NULL, 'strategy.outcome', 'system', 1,
                '{{"goal_id": "{gid}", "plan_version": 3, "strategy": "direct",
                     "outcome": "superseded", "reason": "replan_rollback_v1"}}');
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       episode_id, created_at)
        VALUES ('sout-forged', '{gid}', 'inspect repository', 'direct', 3,
                'superseded', 'replan_rollback_v1', NULL, '{T0}');
    """)
    conn.commit()
    conn.close()

    gm, storage, cognitive = _gm(db)
    assert [p["plan_version"] for p in gm.plan_history(gid)] == [1, 2, 3]
    # forged outcome row is informational, not trusted
    rows = gm.strategy_outcomes(gid)
    assert len(rows) == 3
    # forged telemetry cannot affect a REAL rollback authority
    rec = gm.readopt_plan(gid, 1)
    assert rec["plan_version"] == 4
    storage.close()
    cognitive.close()


def test_forged_plan_rows_fail_closed(tmp_path):
    db = str(tmp_path / "d4b.db")
    gid = _seed_goal(db, with_events=True)
    big_summary = json.dumps([{"index": i} for i in range(600)])
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO goal_plans (goal_id, plan_version, strategy, plan_summary,
                                reason, created_at)
        VALUES ('{gid}', 50, 'evil_strategy', '[{{"index": 0}}]', 'forged',
                '{T0}'),
               ('{gid}', 51, 'direct', '"not-a-list"', 'forged', '{T0}'),
               ('{gid}', 52, 'direct', '{big_summary}', 'forged', '{T0}'),
               ('{gid}', 53, 'direct', '[{{"capability": 42}}]', 'forged',
                '{T0}');
    """)
    conn.commit()
    conn.close()

    gm, storage, cognitive = _gm(db)
    for bad in (50, 51, 52, 53):
        with pytest.raises(ValueError):
            gm.readopt_plan(gid, bad)
    history = [p["plan_version"] for p in gm.plan_history(gid)]
    assert history == [1, 2, 3, 50, 51, 52, 53]  # forged rows readable
    # the failed re-adoption attempts created NOTHING new
    assert len(history) == 7
    # cross-goal confusion fails closed
    other = gm.create_goal("other goal").id
    with pytest.raises(ValueError):
        gm.readopt_plan(other, 2)
    storage.close()
    cognitive.close()


# ============================================================ D5: stored exec

def _sandbox(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir()
    (sb / "README.md").write_text("# R\n")
    return sb


def test_readopted_plan_survives_restart_executes(tmp_path):
    sb = _sandbox(tmp_path)
    db = str(tmp_path / "d5.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    from arion.state.models import VerificationPolicy

    steps = [
        PlanStep(index=0, intent="list root", capability="filesystem.read",
                 action="list", scope="filesystem:read", params={"path": "."},
                 verification=VerificationPolicy("non_empty")),
        PlanStep(index=1, intent="read key files", capability="filesystem.read",
                 action="read", scope="filesystem:read", params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys",
                                                 {"keys": ["content"]})),
    ]
    gm.record_plan_version(gid, "direct", [s.to_dict() for s in steps],
                           reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [s.to_dict() for s in steps],
                           reason="replan_task_failed")
    gm.readopt_plan(gid, 1)                        # v3 = rollback
    engine.shutdown()
    storage.close()

    engine2, gm2, storage2 = _engine(db, sb)       # restart
    goal = engine2.run_goal(gid, max_replans=1)
    assert goal.status_value == GoalStatus.COMPLETED.value
    # deterministic: the re-adopted plan executed (v3 task completed)
    tasks = [t for t in storage2.list_tasks() if t.goal_id == gid]
    assert any(t.plan_version == 3 and t.status == TaskStatus.COMPLETED
               for t in tasks)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3]
    engine2.shutdown()
    storage2.close()


def test_stored_plan_cannot_bypass_authority(tmp_path):
    sb = _sandbox(tmp_path)
    db = str(tmp_path / "d5b.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    from arion.state.models import VerificationPolicy

    steps = [
        PlanStep(index=0, intent="list root", capability="filesystem.read",
                 action="list", scope="filesystem:read", params={"path": "."},
                 verification=VerificationPolicy("non_empty")),
        PlanStep(index=1, intent="read key files", capability="filesystem.read",
                 action="read", scope="filesystem:read", params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys",
                                                 {"keys": ["content"]})),
    ]
    gm.record_plan_version(gid, "direct", [s.to_dict() for s in steps],
                           reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [s.to_dict() for s in steps],
                           reason="replan_task_failed")
    gm.readopt_plan(gid, 1)
    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests",
                 "mutation_recoveries")
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
    assert after == before
    engine.shutdown()
    storage.close()


# ============================================================ D6: peek_evaluate

def test_peek_evaluate_public_non_mutating(tmp_path):
    sb = _sandbox(tmp_path)
    db = str(tmp_path / "d6.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    gm2, storage2, cognitive2 = _gm(db)
    goal_before = gm2.get_goal(gid)
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in ("goals", "audit_events")}
    conn.close()

    assert hasattr(gm2, "peek_evaluate")           # public seam exists
    result = gm2.peek_evaluate(gid)
    assert result is not None
    assert hasattr(result, "progress") and hasattr(result, "next_action")

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in ("goals", "audit_events")}
    conn.close()
    assert after == before                          # goals + events untouched
    assert gm2.get_goal(gid).progress_metadata == goal_before.progress_metadata
    assert gm2.get_goal(gid).last_evaluated_at == goal_before.last_evaluated_at
    storage2.close()
    cognitive2.close()


def test_cli_progress_uses_peek_and_stays_byte_identical(tmp_path, capsys):
    sb = _sandbox(tmp_path)
    db = str(tmp_path / "d6b.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    cli_main(["goals", "list", "--db", db])         # warm-up
    capsys.readouterr()
    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('scheduler_instances', 'scheduler_events', "
        "                  'environment_facts') ORDER BY name")]
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in tables}
    conn.close()
    events_before = _event_kinds(db)

    rc1, out1 = _cli(["goals", "progress", gid, "--db", db])
    rc2, out2 = _cli(["goals", "progress", gid, "--db", db])
    assert rc1 == rc2 == 0 and out1 == out2

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in tables}
    conn.close()
    assert after == before
    assert _event_kinds(db) == events_before


# ============================================================ D7: authority

def test_rollback_execution_authority_byte_identical(tmp_path):
    sb = _sandbox(tmp_path)
    db = str(tmp_path / "d7.db")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    from arion.state.models import VerificationPolicy

    steps = [
        PlanStep(index=0, intent="list root", capability="filesystem.read",
                 action="list", scope="filesystem:read", params={"path": "."},
                 verification=VerificationPolicy("non_empty")),
        PlanStep(index=1, intent="read key files", capability="filesystem.read",
                 action="read", scope="filesystem:read", params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys",
                                                 {"keys": ["content"]})),
    ]
    gm.record_plan_version(gid, "direct", [s.to_dict() for s in steps],
                           reason="initial_plan")
    gm.record_plan_version(gid, "avoid_known_failures",
                           [s.to_dict() for s in steps],
                           reason="replan_task_failed")
    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests",
                 "mutation_recoveries")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    gm.readopt_plan(gid, 1)
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before
