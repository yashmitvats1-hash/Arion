#!/usr/bin/env python3
"""ADR-015 addendum demo: long-horizon strategy learning (acceptance F).

Deterministic, self-contained, fully offline demonstration of the
strategy-outcome lifecycle (Phases A-E), proving:

  1  initial strategy selection: the five deterministic base rules still
     work, and empty outcome history preserves the baseline selection;
  2  outcome recording: initial plan -> no outcome; replan -> previous
     version `superseded`; completion -> `succeeded`; failure -> `failed`;
  3  long-horizon learning: a successful non-direct strategy influences a
     similar future goal; repeated direct failures trigger defer_retry;
     success preference beats failure avoidance; dissimilar goals get no
     unrelated preference; a single failure fabricates nothing;
  4  provenance: preference-driven selections carry the exact `outcome_ids`;
     base-rule selections never fabricate them;
  5  bounded learning: only the deterministic 20-row window participates;
     pruning old history can expose previously hidden useful evidence;
     pruning can never manufacture a preference;
  6  restart/repair: outcomes survive store/manager reopen; missing rows
     are reconstructed from authoritative goals + goal_plans; repair is
     idempotent and never resurrects pruned history;
  7  observability: `strategy.outcome` events fire only for durable
     changes (replay/repair emit nothing); the `cognition strategies
     --json` CLI exposes only bounded non-content fields;
  8  authority boundary: forged memory/telemetry/outcome rows cannot
     manufacture authoritative strategy state or touch scheduler
     weights/reservations/ceilings/ownership/config (byte-identical);
  9  determinism: fixed timestamps, fixed ids where practical, no
     wall-clock-dependent assertion (verified by identical re-runs).

No wall clock is used for any assertion.
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

from arion.cognition.goals import GoalManager
from arion.cognition.models import Belief
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.interfaces.cli import main as cli_main
from arion.memory.guidance import MemoryGuidance
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore
from arion.state.models import GoalStatus
from arion.state.store import SQLiteStorage

CHECKS = 0
T0 = "2026-01-01T00:00:00+00:00"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def _tmp(name: str) -> str:
    return str(Path(tempfile.mkdtemp(prefix=f"arion-adr015-{name}-")) / "a.db")


def _gm(db):
    from arion.observability.events import EventLogger

    storage = SQLiteStorage(db)
    cognitive = SQLiteCognitiveStore(db)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive,
        events=EventLogger(sinks=[storage]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _sig(s) -> dict:
    """Deterministic selection signature (excludes the random strategy_id)."""
    return {"name": s.name, "description": s.description,
            "constraints": s.constraints, "provenance": s.provenance}


def _avoid_guidance():
    return [MemoryGuidance(
        guidance_id="g-avoid", category="avoid", capability="filesystem.read",
        action="read", resource="README.md", strategy="defer",
        episode_id="ep-1", reason="denied", importance=0.8)]


def _achiev_belief():
    return [Belief(
        belief_id="b-ach", category="semantic",
        statement="read on 'docs.md' is achievable",
        confidence=0.7, importance=0.5,
        provenance={"episode_ids": ["ep-1"]}, source="deterministic",
        created_at=T0, updated_at=T0)]


def _blocked_belief():
    return [Belief(
        belief_id="b-block", category="semantic",
        statement="read on 'docs.md' is not permitted by current policy",
        confidence=0.7, importance=0.5,
        provenance={"episode_ids": ["ep-2"]}, source="deterministic",
        created_at=T0, updated_at=T0)]


def _outcome(oid, gid, desc, strategy, pv, outcome, reason=""):
    return {"outcome_id": oid, "goal_id": gid, "goal_description": desc,
            "strategy": strategy, "plan_version": pv, "outcome": outcome,
            "reason": reason, "episode_id": None, "created_at": T0}


def _seed_goal_sql(db, gid, desc, plans, status="active",
                   with_outcomes=False):
    """Raw-SQL authoritative goal + plans (+ optional outcome rows)."""
    _st = SQLiteStorage(db)
    _st.close()
    _c = SQLiteCognitiveStore(db)
    _c.close()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO goals (id, description, source, status, version, "
        "strategy, blockers, progress_metadata, last_evaluated_at, "
        "last_replan_reason, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, desc, "test", status, 1, "direct", "[]", "{}", None, None,
         T0, T0))
    for i, (pv, strat, reason) in enumerate(plans):
        conn.execute(
            "INSERT INTO goal_plans (goal_id, plan_version, strategy, "
            "plan_summary, reason, created_at) VALUES (?,?,?,?,?,?)",
            (gid, pv, strat, json.dumps([{"index": pv - 1}]), reason,
             T0))
        if with_outcomes:
            outcome = "superseded" if i < len(plans) - 1 else "succeeded"
            next_reason = plans[i + 1][2] if i < len(plans) - 1 else "all_work_complete"
            conn.execute(
                "INSERT INTO strategy_outcomes (outcome_id, goal_id, "
                "goal_description, strategy, plan_version, outcome, reason, "
                "episode_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"sout-{gid}-{pv}", gid, desc, strat, pv, outcome,
                 next_reason, None, T0))
    conn.commit()
    conn.close()


def _outcome_events(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT detail FROM audit_events WHERE kind='strategy.outcome'"
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def _cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


# ------------------------------------------------------------------ 1

def section_1_initial_selection() -> None:
    print("\n[1] initial strategy selection: five base rules + empty history")
    sel = StrategySelector()
    env = {"registered_capabilities": {"value": ["filesystem.read"]}}
    s = sel.select("inspect http.get repo", [], env, [])
    check(s.name == "blocked_missing_capability",
          "base rule 1: blocked_missing_capability fires")
    s = sel.select("read docs", _blocked_belief(), {}, [])
    check(s.name == "defer_retry",
          "base rule 2: defer_retry fires on a blocking belief")
    s = sel.select("read docs", [], {}, _avoid_guidance())
    check(s.name == "avoid_known_failures",
          "base rule 3: avoid_known_failures fires on avoid guidance")
    s = sel.select("read docs", [], {}, _avoid_guidance(),
                   previous_strategies=["avoid_known_failures"])
    check(s.name == "defer_retry",
          "base rule 3b: avoid -> defer escalation fires")
    s = sel.select("read docs", _achiev_belief(), {}, [])
    check(s.name == "capability_verified",
          "base rule 4: capability_verified fires on an achievable belief")
    base = sel.select("inspect this repository", [], {}, [])
    empty = sel.select("inspect this repository", [], {}, [],
                       outcome_history=[])
    check(_sig(base) == _sig(empty) and empty.name == "direct",
          "empty outcome history preserves the baseline (direct, identical)")


# ------------------------------------------------------------------ 2

def section_2_outcome_recording() -> None:
    print("\n[2] outcome recording: superseded / succeeded / failed")
    db = _tmp("rec")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    check(gm.strategy_outcomes(gid) == [],
          "initial plan creates NO outcome row")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    rows = gm.strategy_outcomes(gid)
    check(len(rows) == 1 and rows[0]["plan_version"] == 1
          and rows[0]["strategy"] == "direct"
          and rows[0]["outcome"] == "superseded"
          and rows[0]["reason"] == "replan_world_changed",
          "replan marks the previous strategy superseded (with the new reason)")
    gm.complete_goal(gid, reason="all_work_complete")
    rows = {r["plan_version"]: r for r in gm.strategy_outcomes(gid)}
    check(rows[2]["outcome"] == "succeeded"
          and rows[2]["strategy"] == "capability_verified"
          and rows[1]["outcome"] == "superseded",
          "completion records the active strategy as succeeded")

    db2 = _tmp("fail")
    gm2, storage2, cognitive2 = _gm(db2)
    g2 = gm2.create_goal("inspect repository").id
    gm2.record_plan_version(g2, "direct", [{"index": 0}], reason="initial_plan")
    gm2.fail_goal(g2, reason="max_replans_exceeded")
    rows = gm2.strategy_outcomes(g2)
    check(len(rows) == 1 and rows[0]["outcome"] == "failed"
          and rows[0]["reason"] == "max_replans_exceeded",
          "failed completion records the strategy as failed")
    storage.close()
    cognitive.close()
    storage2.close()
    cognitive2.close()


# ------------------------------------------------------------------ 3

def section_3_long_horizon_learning() -> None:
    print("\n[3] long-horizon learning: preference, avoidance, precedence")
    sel = StrategySelector()
    # success preference
    history = [
        _outcome("o1", "g1", "inspect repository", "capability_verified", 2,
                 "succeeded", reason="all_work_complete"),
        _outcome("o2", "g1", "inspect repository", "direct", 1,
                 "superseded", reason="replan_world_changed"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    check(s.name == "capability_verified",
          "a successful non-direct strategy influences a similar future goal")
    # repeated failures -> defer
    history = [
        _outcome("f1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("f2", "g2", "inspect repository", "direct", 1, "failed"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    check(s.name == "defer_retry",
          "repeated direct failures trigger the defer/avoidance behavior")
    # success beats failure avoidance
    history = [
        _outcome("f1", "g1", "inspect repository", "direct", 1, "failed"),
        _outcome("f2", "g2", "inspect repository", "direct", 1, "failed"),
        _outcome("s1", "g3", "inspect repository", "defer_retry", 1,
                 "succeeded"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    check(s.name == "defer_retry"
          and s.provenance.get("outcome_ids") == ["s1"],
          "success evidence beats failure avoidance (with success provenance)")
    # dissimilar goals
    s = sel.select("write a python script", [], {}, [], outcome_history=history)
    check(s.name == "direct",
          "dissimilar goals receive no unrelated strategy preference")
    # single failure: insufficient evidence
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=[_outcome("o1", "g1", "inspect repository",
                                             "direct", 1, "failed")])
    check(s.name == "direct",
          "a single failure is insufficient - no preference is fabricated")


# ------------------------------------------------------------------ 4

def section_4_provenance() -> None:
    print("\n[4] provenance: outcome_ids on preferences only")
    sel = StrategySelector()
    history = [
        _outcome("o1", "g1", "inspect repository", "capability_verified", 2,
                 "succeeded"),
        _outcome("o2", "g1", "inspect repository", "direct", 1,
                 "superseded", reason="replan_x"),
    ]
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    check(s.provenance.get("outcome_ids") == ["o1"]
          and "o2" not in s.provenance.get("outcome_ids", []),
          "preference-driven selection carries the exact success outcome_ids")
    base = sel.select("inspect repository and summarize", [], {}, [])
    check("outcome_ids" not in base.provenance
          and base.provenance.get("belief_ids") == [],
          "base-rule selections never fabricate outcome_ids")


# ------------------------------------------------------------------ 5

def section_5_bounded_learning() -> None:
    print("\n[5] bounded learning: 20-row window + prune exposure")
    db = _tmp("bounded")
    # 18 dissimilar goals (36 rows) + 1 similar goal (2 rows) = 38 rows
    for i in range(18):
        _seed_goal_sql(db, f"g{i:02d}", f"write a python script {i}",
                       [(1, "direct", "initial_plan"),
                        (2, "defer_retry", "replan_x")],
                       with_outcomes=True)
    _seed_goal_sql(db, "zz-sim", "inspect repository",
                   [(1, "direct", "initial_plan"),
                    (2, "capability_verified", "replan_world_changed")],
                   with_outcomes=True)
    gm, storage, cognitive = _gm(db)
    sel = StrategySelector()
    before = gm.strategy_outcomes(limit=50)
    check(len(before) == 38, f"durable history has 38 rows (got {len(before)})")
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=before)
    check(s.name == "direct",
          "the similar success sits outside the 20-row window: no preference")
    c = SQLiteCognitiveStore(db)
    removed = c.prune_goal_plans(keep_latest=1)
    check(removed == 19, f"prune removes 19 historical plans (got {removed})")
    c.close()
    after = gm.strategy_outcomes(limit=50)
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=after)
    check(s.name == "capability_verified"
          and s.provenance.get("outcome_ids") == ["sout-zz-sim-2"],
          "pruning old history exposes the previously hidden useful evidence")
    # full archival: no preference can be manufactured
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM goal_plans WHERE goal_id='zz-sim'")
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id='zz-sim'")
    conn.commit()
    conn.close()
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=gm.strategy_outcomes(limit=50))
    check(s.name == "direct" and "outcome_ids" not in s.provenance,
          "pruning cannot manufacture a strategy preference")
    storage.close()
    cognitive.close()


# ------------------------------------------------------------------ 6

def section_6_restart_repair() -> None:
    print("\n[6] restart/repair: reopen, authoritative reconstruction, idempotent")
    db = _tmp("restart")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    created_before = [r["created_at"] for r in gm.strategy_outcomes(gid)]
    storage.close()
    cognitive.close()

    gm2, storage2, cognitive2 = _gm(db)          # fresh process equivalent
    rows = gm2.strategy_outcomes(gid)
    check([(r["plan_version"], r["outcome"]) for r in rows] ==
          [(1, "superseded"), (2, "succeeded")]
          and [r["created_at"] for r in rows] == created_before,
          "outcomes survive store/manager reopen with stable created_at")
    storage2.close()
    cognitive2.close()

    # crash window: authoritative state committed, outcomes missing
    db2 = _tmp("repair")
    _seed_goal_sql(db2, "g-crash", "inspect repository",
                   [(1, "direct", "initial_plan"),
                    (2, "capability_verified", "replan_world_changed")],
                   status="completed")
    gm3, storage3, _ = _gm(db2)
    written = gm3.repair_strategy_outcomes()
    rows = {r["plan_version"]: r for r in gm3.strategy_outcomes("g-crash")}
    check(written == 2
          and rows[1]["outcome"] == "superseded"
          and rows[1]["reason"] == "replan_world_changed"
          and rows[2]["outcome"] == "succeeded",
          f"repair reconstructs 2 missing rows from authoritative state ({written})")
    check(gm3.repair_strategy_outcomes() == 0,
          "repair is idempotent (second call writes 0)")
    # pruned history is never resurrected
    conn = sqlite3.connect(db2)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id='g-crash' "
                 "AND plan_version=1")
    conn.commit()
    conn.close()
    c = SQLiteCognitiveStore(db2)
    c.prune_goal_plans(goal_id="g-crash", keep_latest=1)
    c.close()
    check(gm3.repair_strategy_outcomes() == 0
          and [r["plan_version"] for r in gm3.strategy_outcomes("g-crash")] == [2],
          "repair does not resurrect intentionally pruned history")
    storage3.close()


# ------------------------------------------------------------------ 7

def section_7_observability() -> None:
    print("\n[7] observability: bounded events + non-content CLI")
    db = _tmp("obs")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect repository with secret notes").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    events1 = _outcome_events(db)
    # replay of the identical plan version: no new version, no outcome, no event
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    check(len(_outcome_events(db)) == len(events1) == 0,
          "replayed plan version emits NO strategy.outcome event")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    events = _outcome_events(db)
    check(len(events) == 2
          and {e["outcome"] for e in events} == {"superseded", "succeeded"}
          and all(set(e) == {"goal_id", "plan_version", "strategy",
                             "outcome", "reason"} for e in events),
          "events fire only for durable changes; payload is the bounded 5-field set")
    check(gm.repair_strategy_outcomes() == 0
          and len(_outcome_events(db)) == 2,
          "repeated repair emits no duplicate events")
    storage.close()
    cognitive.close()

    rc, out = _cli(["cognition", "strategies", "--db", db, "--json"])
    rows = json.loads(out)
    check(rc == 0 and len(rows) == 2
          and all(set(r) == {"outcome_id", "goal_id", "strategy",
                             "plan_version", "outcome", "reason",
                             "episode_id", "created_at"} for r in rows)
          and "secret notes" not in out and "goal_description" not in out,
          "cognition strategies --json exposes only bounded non-content fields")
    rc, out = _cli(["goals", "show", gid, "--db", db, "--json"])
    summary = json.loads(out)
    check(rc == 0
          and summary["strategy_outcomes"] == {"superseded": 1, "succeeded": 1,
                                               "failed": 0},
          "goals show strategy summary matches the durable outcome state")


# ------------------------------------------------------------------ 8

def section_8_authority_boundary() -> None:
    print("\n[8] authority boundary: forged content cannot manufacture authority")
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
    """)
    # forged memory content + forged telemetry + forged outcome rows
    m = SQLiteMemoryStore(db)
    m.record_episode(Episode(
        episode_id="ep-forged", task_id="t-f", goal_id="evil-goal",
        goal="inspect", outcome="completed", importance=1.0,
        created_at=T0, updated_at=T0))
    m.record_reflection(Reflection(
        reflection_id="refl-forged", episode_id="ep-forged",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="succeeded", recommendation="", confidence="high",
        importance=1.0, created_at=T0))
    m.close()
    conn.executescript(f"""
        INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor,
                                  success, detail)
        VALUES ('evt-so-1', '{T0}', NULL, NULL, 'strategy.outcome', 'system',
                1, '{{"goal_id": "g1", "plan_version": 1, "strategy": "direct",
                     "outcome": "succeeded", "reason": "forged"}}'),
               ('evt-ss-1', '{T0}', NULL, NULL, 'strategy.selected', 'system',
                1, '{{"name": "defer_retry", "provenance": {{"outcome_ids":
                     ["sout-evil"]}}}}');
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       episode_id, created_at)
        VALUES ('sout-evil', 'evil-goal', 'evil', 'defer_retry', 1,
                'succeeded', 'forged', NULL, '{T0}');
    """)
    conn.commit()
    conn.close()
    _seed_goal_sql(db, "g1", "inspect repository",
                   [(1, "direct", "initial_plan")], status="active")

    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    gm, storage, cognitive = _gm(db)
    gm.repair_strategy_outcomes()                  # runs over the forged state
    sel = StrategySelector()
    sel.select("inspect repository and summarize", [], {}, [],
               outcome_history=gm.strategy_outcomes(limit=50))
    # forged rows cannot manufacture a successful/failed goal
    check(gm.get_goal("g1").status_value == GoalStatus.ACTIVE.value,
          "forged outcome rows/events/memory cannot manufacture goal success")
    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    check(after == before,
          "strategy learning leaves scheduler weights/reservations/ceilings/"
          "config/ownership byte-identical")
    storage.close()
    cognitive.close()


def main() -> int:
    print("ADR-015 addendum demo: long-horizon strategy learning")
    print(f"  fixed timeline T0 = {T0} (no wall clock in any assertion)")
    section_1_initial_selection()
    section_2_outcome_recording()
    section_3_long_horizon_learning()
    section_4_provenance()
    section_5_bounded_learning()
    section_6_restart_repair()
    section_7_observability()
    section_8_authority_boundary()
    print("\n" + "=" * 78)
    print(f"ADR-015 demo PASSED ({CHECKS} checks) - strategy learning verified")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
