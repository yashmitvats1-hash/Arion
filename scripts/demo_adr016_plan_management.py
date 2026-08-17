#!/usr/bin/env python3
"""ADR-016 addendum demo: plan management (acceptance E).

Deterministic, self-contained, fully offline demonstration of Phases A-D:

  Plan history      - initial plan creation, immutable historical versions,
                      deterministic goals diff (incl. identical-version
                      empty diff), bounded content-safe diff output;
  Plan re-adoption  - rollback of an older version (reason
                      replan_rollback_v<N>), new immutable version with the
                      historical content, ADR-015 supersede coupling exactly
                      once, replay idempotent;
  Stored execution  - re-adopted plan executes without invoking the planner
                      (spy raises if called), deterministic from stored
                      plan_summary, live authorization still applies,
                      success creates no unnecessary new plan version;
  Restart/crash     - re-adopted plan survives close/reopen, remains
                      executable after restart, missing strategy outcome
                      repaired from authoritative plan history, repeated
                      repair idempotent;
  Pruning           - historical plan + coupled outcome prunable, diff and
                      rollback fail closed against pruned versions, repair
                      never resurrects pruned history, surviving historical
                      versions remain repairable;
  Read-only progress- GoalManager.peek_evaluate() non-mutating, goals
                      progress byte-identical across calls, no progress/
                      audit/strategy-outcome mutation;
  Authority         - forged telemetry cannot manufacture plan history or
                      rollback, scheduler configuration/weights/
                      reservations/ceilings/ownership/leases/approvals/
                      recoveries unchanged, execution-created scheduler/
                      checkpoint rows treated separately from authority
                      configuration;
  CLI observability - goals diff --json / rollback --json / progress --json,
                      deterministic across runs, no free-text/secret leakage.

No wall clock is used for any assertion (fixed timestamps / fixed ids where
practical; verified by byte-identical re-runs).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from arion.state.models import GoalStatus, PlanStep, TaskStatus
from arion.state.store import SQLiteStorage

CHECKS = 0
T0 = "2026-01-01T00:00:00+00:00"
FS = "filesystem:path"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def _tmp(name: str) -> str:
    return str(Path(tempfile.mkdtemp(prefix=f"arion-adr016-{name}-")) / "a.db")


def _gm(db):
    storage = SQLiteStorage(db)
    cognitive = SQLiteCognitiveStore(db)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive,
        events=EventLogger(sinks=[storage]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _engine(db, sandbox, planner=None):
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
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


class _PlannerSpy:
    """Raises if invoked - proves stored-plan execution bypasses the planner."""

    def __init__(self):
        self.calls = 0

    def plan(self, goal_description, task_id, registry, context=None):
        self.calls += 1
        raise AssertionError("planner must not be invoked for stored plans")

    def required_capabilities(self, goal_description):
        return set()


def _step(index, action="read", path="x.md"):
    from arion.state.models import VerificationPolicy

    return PlanStep(index=index, intent=f"step {index}", capability="filesystem.read",
                    action=action, scope="filesystem:read", params={"path": path},
                    verification=VerificationPolicy("non_empty"))


def _cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


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


def _seed_versions(gm, gid):
    """v1 [read a.md], v2 [list ., read b.md], v3 [read a.md, list .]."""
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


# ------------------------------------------------------------------ 1

def section_1_plan_history() -> None:
    print("\n[1] plan history: creation, immutability, deterministic diff")
    db = _tmp("hist")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    _seed_versions(gm, gid)
    history = gm.plan_history(gid)
    check([p["plan_version"] for p in history] == [1, 2, 3]
          and [p["reason"] for p in history] ==
          ["initial_plan", "replan_task_failed", "replan_world_changed"],
          "initial plan + 2 replans create immutable versions v1-v3")
    before = _dump(db, ("goal_plans",))["goal_plans"]
    gm.readopt_plan(gid, 1)                            # v4 rollback
    after = _dump(db, ("goal_plans",))["goal_plans"]
    check(after[:3] == before and len(after) == 4,
          "historical versions stay byte-identical; only a new row is appended")

    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
    d = json.loads(out)
    check(rc == 0 and d["strategy_a"] == "direct"
          and d["strategy_b"] == "avoid_known_failures"
          and d["added"] == [0, 1] and d["removed"] == [0],
          "goals diff reports deterministic structural differences")
    rc, out = _cli(["goals", "diff", gid, "1", "1", "--db", db, "--json"])
    d1 = json.loads(out)
    check(rc == 0 and d1["identical"] is True
          and d1["added"] == [] and d1["removed"] == [],
          "identical-version diff is an explicit empty diff")
    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
    check("a.md" not in out and "intent" not in out and "params" not in out,
          "diff output is bounded and content-safe (no free-text)")
    storage.close()
    cognitive.close()


# ------------------------------------------------------------------ 2

def section_2_re_adoption() -> None:
    print("\n[2] plan re-adoption: rollback, content, supersede, idempotent")
    db = _tmp("readopt")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    _seed_versions(gm, gid)
    v1 = gm.plan_history(gid)[0]

    rec = gm.readopt_plan(gid, 1)
    history = gm.plan_history(gid)
    check(rec["plan_version"] == 4
          and [p["plan_version"] for p in history] == [1, 2, 3, 4],
          "rollback creates exactly one new immutable version (v4)")
    check(rec["strategy"] == v1["strategy"] == "direct"
          and rec["plan_summary"] == v1["plan_summary"],
          "the new version contains the historical plan content")
    check(rec["reason"] == "replan_rollback_v1",
          "reason is exactly replan_rollback_v1")

    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    check(rows[3]["outcome"] == "superseded"
          and rows[3]["reason"] == "replan_rollback_v1",
          "ADR-015 supersede coupling occurs on the previously active version")
    events = [e for e in _outcome_events(db)
              if e["reason"] == "replan_rollback_v1"]
    check(len(events) == 1, "supersede coupling emits exactly ONE outcome event")

    again = gm.readopt_plan(gid, 1)
    check(again["plan_version"] == 4
          and len(gm.plan_history(gid)) == 4
          and len([e for e in _outcome_events(db)
                   if e["reason"] == "replan_rollback_v1"]) == 1,
          "replay is idempotent: same version, no duplicate outcome/event")
    storage.close()
    cognitive.close()


# ------------------------------------------------------------------ 3

def section_3_stored_plan_execution() -> None:
    print("\n[3] stored-plan execution: planner bypass, live authz, no new version")
    db = _tmp("stored")
    sb = Path(tempfile.mkdtemp()) / "repo"
    sb.mkdir()
    (sb / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sb, planner=DeterministicPlanner())
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
    gm.readopt_plan(gid, 1)                            # v3 = rollback

    spy = _PlannerSpy()
    engine.planner = spy                               # the bypass proof
    goal = engine.run_goal(gid, max_replans=1)
    check(goal.status_value == GoalStatus.COMPLETED.value,
          "re-adopted plan executes deterministically from stored plan_summary")
    check(spy.calls == 0,
          "stored-plan execution does NOT invoke the planner")
    tasks = [t for t in storage.list_tasks()
             if t.goal_id == gid and t.plan_version == 3]
    check(len(tasks) == 1 and tasks[0].status == TaskStatus.COMPLETED,
          "the executed task carries the re-adopted plan_version (v3)")
    check([p["plan_version"] for p in gm.plan_history(gid)] == [1, 2, 3],
          "successful execution creates NO unnecessary new plan version")

    # live authorization still applies: tighten the ActionSpec -> DENY
    spec = engine.registry._caps["filesystem.read"].actions[0]
    original = spec.required_scope
    spec.required_scope = "filesystem:write"
    try:
        gid2 = engine.submit_goal("inspect this repository").id
        gm2 = gm
        gm2.record_plan_version(gid2, "direct", [s.to_dict() for s in steps],
                                reason="initial_plan")
        gm2.record_plan_version(gid2, "avoid_known_failures",
                                [s.to_dict() for s in steps],
                                reason="replan_task_failed")
        gm2.readopt_plan(gid2, 1)
        goal2 = engine.run_goal(gid2, max_replans=1)
        denied = [e for e in storage.list_events()
                  if e.kind == "permission.denied"]
        check(goal2.status_value != GoalStatus.COMPLETED.value and len(denied) >= 1,
              "live capability/authorization checks still apply to stored plans")
    finally:
        spec.required_scope = original
    engine.shutdown()
    storage.close()


# ------------------------------------------------------------------ 4

def section_4_restart_crash() -> None:
    print("\n[4] restart/crash: reopen, re-execute, repair, idempotent")
    db = _tmp("restart")
    sb = Path(tempfile.mkdtemp()) / "repo"
    sb.mkdir()
    (sb / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sb, planner=DeterministicPlanner())
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
    gm.readopt_plan(gid, 1)                            # v3 = rollback
    engine.shutdown()
    storage.close()

    engine2, gm2, storage2 = _engine(db, sb, planner=DeterministicPlanner())
    check([p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3]
          and gm2.latest_plan(gid)["reason"] == "replan_rollback_v1",
          "re-adopted plan survives close/reopen with its reason intact")
    engine2.planner = _PlannerSpy()
    goal = engine2.run_goal(gid, max_replans=1)
    check(goal.status_value == GoalStatus.COMPLETED.value,
          "stored-plan execution remains executable after restart")
    engine2.shutdown()
    storage2.close()

    # crash window: outcome rows wiped -> repair from authoritative history
    db2 = _tmp("crash")
    gm3, storage3, cognitive3 = _gm(db2)
    gid3 = gm3.create_goal("inspect repository").id
    _seed_versions(gm3, gid3)
    gm3.readopt_plan(gid3, 1)
    storage3.close()
    cognitive3.close()
    conn = sqlite3.connect(db2)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.execute("DELETE FROM audit_events")
    conn.commit()
    conn.close()

    gm4, storage4, cognitive4 = _gm(db2)
    written = gm4.repair_strategy_outcomes()
    rows = {r["plan_version"]: r for r in gm4.strategy_outcomes(gid3)}
    check(written == 3
          and rows[3]["outcome"] == "superseded"
          and rows[3]["reason"] == "replan_rollback_v1",
          "missing strategy outcome is repaired from authoritative plan history")
    check(gm4.repair_strategy_outcomes() == 0
          and len([e for e in _outcome_events(db2)
                   if e["reason"] == "replan_rollback_v1"]) == 1,
          "repeated repair is idempotent (no duplicate events)")
    storage4.close()
    cognitive4.close()


# ------------------------------------------------------------------ 5

def section_5_pruning() -> None:
    print("\n[5] pruning: coupled removal, fail-closed diff/rollback, no resurrection")
    db = _tmp("prune")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    _seed_versions(gm, gid)
    storage.close()
    cognitive.close()

    c = SQLiteCognitiveStore(db)
    removed = c.prune_goal_plans(goal_id=gid, keep_latest=2)
    c.close()
    check(removed == 1, "historical plan (v1) can be pruned")
    gm2, storage2, cognitive2 = _gm(db)
    rows = gm2.strategy_outcomes(gid)
    check([r["plan_version"] for r in rows] == [2]
          and [p["plan_version"] for p in gm2.plan_history(gid)] == [2, 3],
          "coupled strategy outcome (v1) is pruned with its plan; v2 remains")

    rc, _ = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    check(rc == 1, "goals diff fails closed against pruned versions")
    rc, _ = _cli(["goals", "rollback", gid, "1", "--db", db])
    check(rc == 1, "goals rollback fails closed against pruned versions")
    check(gm2.repair_strategy_outcomes() == 0,
          "repair never resurrects pruned history")

    # surviving historical version (v2) remains repairable
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=2", (gid,))
    conn.commit()
    conn.close()
    check(gm2.repair_strategy_outcomes() == 1,
          "surviving historical versions remain repairable")
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------------ 6

def section_6_read_only_progress() -> None:
    print("\n[6] read-only progress: peek_evaluate non-mutating, byte-identical")
    db = _tmp("progress")
    sb = Path(tempfile.mkdtemp()) / "repo"
    sb.mkdir()
    (sb / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sb)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    gm2, storage2, cognitive2 = _gm(db)
    goal_before = gm2.get_goal(gid)
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in ("goals", "goal_plans", "strategy_outcomes", "audit_events")}
    conn.close()

    result = gm2.peek_evaluate(gid)
    check(result is not None and hasattr(result, "progress")
          and hasattr(result, "next_action"),
          "GoalManager.peek_evaluate() is a public non-mutating seam")

    rc1, out1 = _cli(["goals", "progress", gid, "--db", db])
    rc2, out2 = _cli(["goals", "progress", gid, "--db", db])
    check(rc1 == rc2 == 0 and out1 == out2,
          "goals progress is byte-identical across repeated calls")

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in ("goals", "goal_plans", "strategy_outcomes", "audit_events")}
    conn.close()
    # Each CLI invocation boots a fresh engine. The first boot registers the
    # full capability set once (deterministic observational event); the second
    # boot finds it already registered and mutates nothing. Same observational
    # exclusion as scheduler_instances/scheduler_events in earlier phases.
    new_audit = [r for r in after["audit_events"]
                 if r not in set(before["audit_events"])]
    check(all(after[t] == before[t]
              for t in ("goals", "goal_plans", "strategy_outcomes"))
          and len(new_audit) == 1
          and new_audit[0][4] == "world.state.changed"
          and "registered_capabilities" in new_audit[0][7]
          and gm2.get_goal(gid).progress_metadata == goal_before.progress_metadata
          and gm2.get_goal(gid).last_evaluated_at == goal_before.last_evaluated_at,
          "no progress/audit/strategy-outcome mutation (only one-time capability registration)")
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------------ 7

def section_7_authority_boundary() -> None:
    print("\n[7] authority boundary: forged telemetry, byte-identical scheduler")
    db = _tmp("auth")
    _st = SQLiteStorage(db)
    _st.close()
    _c = SQLiteCognitiveStore(db)
    _c.close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
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
        INSERT INTO approval_requests (approval_id, task_id, step_index, goal_id,
                                       capability, action, scope, risk,
                                       side_effects, resource_kind, resource,
                                       summary, status, requester_actor,
                                       actor_chain, params_keys, fingerprint,
                                       created_at, updated_at)
        VALUES ('ap-1', 't-1', 1, 'g1', 'filesystem.write', 'write',
                'filesystem:write', 'medium', 'mutating', 'filesystem:path',
                'x.txt', 'write x', 'pending', 'agent:arion',
                '["user:alice","agent:arion"]', '["path"]', 'fp-1', '{T0}', '{T0}');
        INSERT INTO mutation_recoveries (recovery_id, task_id, goal_id,
                                         step_index, capability, action, resource,
                                         reason, status, created_at)
        VALUES ('rec-1', 't-1', 'g1', 1, 'filesystem.write', 'write', 'x.txt',
                'interrupted', 'required', '{T0}');
        INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor,
                                  success, detail)
        VALUES ('evt-pv', '{T0}', NULL, NULL, 'plan.versioned', 'system', 1,
                '{{"goal_id": "g1", "plan_version": 99, "strategy": "direct",
                     "reason": "replan_rollback_v1", "steps": 1}}'),
               ('evt-so', '{T0}', NULL, NULL, 'strategy.outcome', 'system', 1,
                '{{"goal_id": "g1", "plan_version": 3, "strategy": "direct",
                     "outcome": "superseded", "reason": "replan_rollback_v1"}}');
    """)
    conn.commit()
    conn.close()
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    _seed_versions(gm, gid)
    check([p["plan_version"] for p in gm.plan_history(gid)] == [1, 2, 3],
          "forged plan.versioned/strategy.outcome telemetry cannot manufacture "
          "plan history")

    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests",
                 "mutation_recoveries")
    before = _dump(db, authority)
    rec = gm.readopt_plan(gid, 1)
    check(rec["plan_version"] == 4,
          "forged telemetry cannot block a real rollback (authority intact)")
    after = _dump(db, authority)
    check(after == before,
          "scheduler config/weights/reservations/ceilings/ownership/leases/"
          "approvals/recoveries unchanged by rollback")
    storage.close()
    cognitive.close()


# ------------------------------------------------------------------ 8

def section_8_cli_observability() -> None:
    print("\n[8] CLI observability: --json surfaces, deterministic, secret-free")
    db = _tmp("cli")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository with secret notes").id
    _seed_versions(gm, gid)
    storage.close()
    cognitive.close()

    outs = []
    for _ in range(2):
        rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
        assert rc == 0
        outs.append(out)
    check(outs[0] == outs[1]
          and "secret notes" not in outs[0] and "a.md" not in outs[0],
          "goals diff --json is deterministic and free of free-text/secrets")

    rcs = []
    for _ in range(2):
        rc, out = _cli(["goals", "rollback", gid, "1", "--db", db, "--json"])
        assert rc == 0
        rcs.append((rc, out))
    d = json.loads(rcs[0][1])
    check(rcs[0] == rcs[1] and d["plan_version"] == 4
          and d["reason"] == "replan_rollback_v1"
          and set(d) == {"goal_id", "plan_version", "strategy", "reason"},
          "goals rollback --json is deterministic with a stable schema")

    rc1, out1 = _cli(["goals", "progress", gid, "--db", db, "--json"])
    rc2, out2 = _cli(["goals", "progress", gid, "--db", db, "--json"])
    p = json.loads(out1)
    check(rc1 == rc2 == 0 and out1 == out2
          and set(p) == {"goal_id", "evaluation", "status",
                         "progress_metadata"},
          "goals progress --json is deterministic with a stable schema")


def main() -> int:
    print("ADR-016 addendum demo: plan management")
    print(f"  fixed timeline T0 = {T0} (no wall clock in any assertion)")
    section_1_plan_history()
    section_2_re_adoption()
    section_3_stored_plan_execution()
    section_4_restart_crash()
    section_5_pruning()
    section_6_read_only_progress()
    section_7_authority_boundary()
    section_8_cli_observability()
    print("\n" + "=" * 78)
    print(f"ADR-016 demo PASSED ({CHECKS} checks) - plan management verified")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
