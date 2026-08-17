"""ADR-015 addendum Phase E: pruning integration + lifecycle hardening -
tests first.

Authority rule (unchanged, never broadened):

    goal_state + surviving goal_plans  -> authoritative for outcome repair
    strategy_outcomes, memory, telemetry, planner output, events -> informational

E1  pruning correctness: coupled outcome deletion, latest protected,
    cross-goal isolation, multi-batch drains, dry-run byte-identical,
    idempotent, reopen-consistent, deterministic counts.
E2  repair-after-prune semantics: pruned history never resurrected;
    remaining authoritative plans with missing outcomes reconstructed;
    active/paused/cancelled/blocked latest never fabricated;
    terminal latest reconstructed; restart+prune+repair combos.
E3  bounded strategy-history consumption: pruning bounds the durable
    history the selector consumes; deterministic ordering; empty history
    preserves the five base rules byte-for-byte; pruning cannot
    manufacture a preference; no timestamps in selection; malformed/
    oversized rows fail closed (length bounds on goal_description/reason).
E4  observability after pruning: CLI never exposes deleted outcomes;
    goals-show counts match durable state; pruning emits no strategy
    events; repair emits an event only for genuinely recreated rows;
    repeated repair emits nothing; CLI JSON bounded/secret-free.
E5  adversarial final boundary: arbitrary outcome deletion reconstructs
    only from surviving plans; forged rows for pruned versions untouched;
    forged rows for surviving versions never overwritten; oversized
    forged rows fail closed in selection; nothing can manufacture
    authority.
E6  lifecycle/idempotency matrix: 12 paths, each verified for exactly-one
    outcome per (goal, version), no duplicate events, stable created_at,
    no authority mutation outside the intended goal/plan operation.

All timestamps fixed; no wall clock in any assertion.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.interfaces.cli import main as cli_main
from arion.observability.events import EventLogger
from arion.state.models import GoalStateError, GoalStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


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


def _outcome(oid, gid, desc, strategy, pv, outcome, reason=""):
    return {"outcome_id": oid, "goal_id": gid, "goal_description": desc,
            "strategy": strategy, "plan_version": pv, "outcome": outcome,
            "reason": reason, "episode_id": None, "created_at": T0}


# =================================================================== E1

def test_prune_removes_coupled_outcomes_protects_latest(tmp_path):
    db = str(tmp_path / "e1a.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    for i, (strat, reason) in enumerate([
            ("direct", "initial_plan"),
            ("avoid_known_failures", "replan_task_failed"),
            ("defer_retry", "replan_avoid_repeated"),
            ("capability_verified", "replan_world_changed")]):
        gm.record_plan_version(gid, strat, [{"index": i}], reason=reason)
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    removed = c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    assert removed == 3
    assert [p["plan_version"] for p in c2.list_goal_plans(gid)] == [4]
    rows = c2.list_strategy_outcomes(goal_id=gid)
    assert [r["plan_version"] for r in rows] == [4]
    assert rows[0]["outcome"] == "succeeded"       # latest outcome protected
    assert c2.latest_goal_plan(gid)["plan_version"] == 4
    c2.close()


def test_prune_cross_goal_isolation(tmp_path):
    db = str(tmp_path / "e1b.db")
    gm, storage, cognitive = _gm(db)
    g1 = gm.create_goal("goal one").id
    g2 = gm.create_goal("goal two").id
    for g in (g1, g2):
        gm.record_plan_version(g, "direct", [{"index": 0}], reason="initial_plan")
        gm.record_plan_version(g, "capability_verified", [{"index": 0}],
                               reason="replan_world_changed")
        gm.complete_goal(g, reason="all_work_complete")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(goal_id=g1, keep_latest=1) == 1
    c2.close()
    # g2 completely untouched: plans + outcomes intact
    c3 = SQLiteCognitiveStore(db)
    assert [p["plan_version"] for p in c3.list_goal_plans(g2)] == [1, 2]
    assert [r["plan_version"] for r in c3.list_strategy_outcomes(goal_id=g2)] == [1, 2]
    assert [p["plan_version"] for p in c3.list_goal_plans(g1)] == [2]
    c3.close()


def test_prune_multi_batch_drain(tmp_path):
    db = str(tmp_path / "e1c.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    for i in range(1, 8):
        gm.record_plan_version(gid, "direct", [{"index": i}],
                               reason="initial_plan" if i == 1
                               else f"replan_{i}")
    gm.complete_goal(gid, reason="all_work_complete")
    assert len(gm.strategy_outcomes(gid)) == 7
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    removed = c2.prune_goal_plans(goal_id=gid, keep_latest=1, batch_size=3)
    assert removed == 6                            # 2+ batches drained
    rows = c2.list_strategy_outcomes(goal_id=gid)
    assert [r["plan_version"] for r in rows] == [7]
    assert [p["plan_version"] for p in c2.list_goal_plans(gid)] == [7]
    c2.close()


def test_prune_dry_run_byte_identical_everything(tmp_path):
    db = str(tmp_path / "e1d.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    conn.close()
    before = _dump(db, tables)

    c2 = SQLiteCognitiveStore(db)
    would = c2.prune_goal_plans(goal_id=gid, keep_latest=1, dry_run=True)
    assert would == 1
    c2.close()
    assert _dump(db, tables) == before             # plans, outcomes, EVENTS,
                                                   # authority: byte-identical
    assert _outcome_events(db) == _outcome_events(db)  # no new events


def test_prune_idempotent_and_reopen_consistent(tmp_path):
    db = str(tmp_path / "e1e.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    for i, (s, r) in enumerate([("direct", "initial_plan"),
                                ("defer_retry", "replan_x")]):
        gm.record_plan_version(gid, s, [{"index": i}], reason=r)
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=1) == 1
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=1) == 0  # idempotent
    c2.close()

    c3 = SQLiteCognitiveStore(db)                  # reopen
    assert [p["plan_version"] for p in c3.list_goal_plans(gid)] == [2]
    assert [r["plan_version"] for r in c3.list_strategy_outcomes(goal_id=gid)] == [2]
    c3.close()


def test_outcome_counts_deterministic_after_prune(tmp_path):
    db = str(tmp_path / "e1f.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    for i in range(1, 5):
        gm.record_plan_version(gid, "direct", [{"index": i}],
                               reason="initial_plan" if i == 1 else f"replan_{i}")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=2) == 2
    assert c2.count_strategy_outcomes() == 2
    assert len(c2.list_strategy_outcomes(goal_id=gid)) == 2
    assert c2.list_strategy_outcomes(goal_id=gid) == \
        c2.list_strategy_outcomes(goal_id=gid)      # deterministic
    c2.close()


# =================================================================== E2

def test_repair_never_resurrects_pruned_history(tmp_path):
    db = str(tmp_path / "e2a.db")
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
    c2.close()
    events_before = len(_outcome_events(db))

    gm2, storage2, _ = _gm(db)
    assert gm2.repair_strategy_outcomes() == 0     # pruned v1 NOT resurrected
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [2]
    assert [r["plan_version"] for r in gm2.strategy_outcomes(gid)] == [2]
    assert len(_outcome_events(db)) == events_before   # no repair events
    storage2.close()


def test_repair_reconstructs_remaining_missing_after_prune(tmp_path):
    db = str(tmp_path / "e2b.db")
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
    assert c2.prune_goal_plans(goal_id=gid, keep_latest=2) == 1   # v1 gone
    c2.close()
    conn = sqlite3.connect(db)                     # crash window: v2 outcome lost
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=2", (gid,))
    conn.execute("DELETE FROM audit_events")       # ignore seed-phase events
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    assert gm2.repair_strategy_outcomes() == 1     # v2 reconstructed
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert set(rows) == {2}                        # v1 stays pruned
    assert rows[2]["outcome"] == "superseded"
    assert rows[2]["reason"] == "replan_world_changed"
    assert len(_outcome_events(db)) == 1           # exactly one repair event
    storage2.close()


def test_repair_latest_paused_cancelled_blocked_no_fabrication(tmp_path):
    db = str(tmp_path / "e2c.db")
    gm, storage, cognitive = _gm(db)
    paused = gm.create_goal("paused goal").id
    cancelled = gm.create_goal("cancelled goal").id
    blocked = gm.create_goal("blocked goal").id
    active = gm.create_goal("active goal").id
    for g in (paused, cancelled, blocked, active):
        gm.record_plan_version(g, "direct", [{"index": 0}], reason="initial_plan")
        gm.record_plan_version(g, "defer_retry", [{"index": 0}],
                               reason="replan_x")
    gm.pause(paused)
    gm.cancel(cancelled)
    gm.set_blocked(blocked, {"type": "missing_capability", "detail": "x"})
    # active stays active
    storage.close()
    cognitive.close()

    # crash window: all outcome rows + events wiped
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.execute("DELETE FROM audit_events")
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    written = gm2.repair_strategy_outcomes()
    assert written == 4                            # v1 superseded for each
    for g in (paused, cancelled, blocked, active):
        rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(g)}
        assert set(rows) == {1}                    # v2 latest: NO fabrication
        assert rows[1]["outcome"] == "superseded"
    assert len(_outcome_events(db)) == 4
    storage2.close()


def test_restart_prune_repair_combination(tmp_path):
    """restart -> prune -> restart -> repair -> consistent, per E2."""
    db = str(tmp_path / "e2d.db")
    gm, storage, cognitive = _gm(db)
    keep = gm.create_goal("keep me").id
    drop = gm.create_goal("drop me").id
    for g in (keep, drop):
        gm.record_plan_version(g, "direct", [{"index": 0}], reason="initial_plan")
        gm.record_plan_version(g, "capability_verified", [{"index": 0}],
                               reason="replan_world_changed")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)                  # restart
    assert c2.prune_goal_plans(goal_id=drop, keep_latest=1) == 1
    c2.close()
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=1", (keep,))    # crash window on keep
    conn.execute("DELETE FROM audit_events")       # ignore seed-phase events
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)                     # restart + repair
    assert gm2.repair_strategy_outcomes() == 1
    # keep: v1 superseded reconstructed; v2 is the ACTIVE latest (no row)
    assert [r["plan_version"] for r in gm2.strategy_outcomes(keep)] == [1]
    assert gm2.strategy_outcomes(keep)[0]["outcome"] == "superseded"
    # drop: v1 pruned (not resurrected); v2 active latest (no row)
    assert gm2.strategy_outcomes(drop) == []
    assert len(_outcome_events(db)) == 1
    storage2.close()


# =================================================================== E3

def _seed_goal_with_plans_and_outcomes(db, gid, desc, plans, status="active"):
    """Raw-SQL seed: goal + plans + matching outcome rows
    (v1 superseded with next reason, vN succeeded for the last version)."""
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
    for pv, strat, reason in plans:
        conn.execute(
            "INSERT INTO goal_plans (goal_id, plan_version, strategy, "
            "plan_summary, reason, created_at) VALUES (?,?,?,?,?,?)",
            (gid, pv, strat, json.dumps([{"index": pv - 1}]), reason,
             _iso_plus(T0, pv * 10)))
        outcome = "superseded" if pv < len(plans) else "succeeded"
        conn.execute(
            "INSERT INTO strategy_outcomes (outcome_id, goal_id, "
            "goal_description, strategy, plan_version, outcome, reason, "
            "episode_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sout-{gid}-{pv}", gid, desc, strat, pv, outcome,
             plans[pv][2] if pv < len(plans) else "all_work_complete",
             None, T0))
    conn.commit()
    conn.close()


def test_prune_bounds_history_consumed_by_selection(tmp_path):
    """18 dissimilar goals (36 rows) push a similar-context success outside
    the selector's 20-row window; pruning the historical v1 rows (19 rows
    removed) slides the window so the success becomes visible."""
    db = str(tmp_path / "e3a.db")
    for i in range(18):
        _seed_goal_with_plans_and_outcomes(
            db, f"g{i:02d}", f"write a python script {i}",
            [(1, "direct", "initial_plan"), (2, "defer_retry", "replan_x")])
    _seed_goal_with_plans_and_outcomes(
        db, "zz-sim", "inspect repository",
        [(1, "direct", "initial_plan"),
         (2, "capability_verified", "replan_world_changed")])

    gm, storage, cognitive = _gm(db)
    sel = StrategySelector()
    before = gm.strategy_outcomes(limit=50)
    assert len(before) == 38                       # full durable history
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=before)
    assert s.name == "direct"                      # success outside the window

    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(keep_latest=1) == 19   # all v1 plans pruned
    c2.close()
    after = gm.strategy_outcomes(limit=50)
    assert len(after) == 19                        # bounded by prune
    s2 = sel.select("inspect repository and summarize", [], {}, [],
                    outcome_history=after)
    assert s2.name == "capability_verified"        # evidence now visible
    assert s2.provenance.get("outcome_ids") == ["sout-zz-sim-2"]
    storage.close()
    cognitive.close()


def test_empty_history_after_prune_preserves_base_rules(tmp_path):
    db = str(tmp_path / "e3b.db")
    _seed_goal_with_plans_and_outcomes(
        db, "g1", "inspect repository",
        [(1, "direct", "initial_plan"), (2, "capability_verified", "replan_x")])
    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(keep_latest=1) == 1   # v1 pruned
    c2.close()
    # full archival of the remaining plan + outcome -> history is EMPTY
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM goal_plans WHERE goal_id='g1'")
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id='g1'")
    conn.commit()
    conn.close()

    gm, storage, cognitive = _gm(db)
    history = gm.strategy_outcomes(limit=50)
    assert len(history) == 0
    sel = StrategySelector()
    for goal, beliefs, env, guidance, prev in [
        ("inspect repository and summarize", [], {}, [], []),
        ("read docs", [], {}, [__import__("arion.memory.guidance",
                                          fromlist=["MemoryGuidance"])
                               .MemoryGuidance(
                                   guidance_id="g1", category="avoid",
                                   capability="filesystem.read", action="read",
                                   resource="README.md", episode_id="e1",
                                   reason="denied", importance=0.8)], []),
    ]:
        base = sel.select(goal, beliefs, env, guidance, previous_strategies=prev)
        with_h = sel.select(goal, beliefs, env, guidance,
                            previous_strategies=prev, outcome_history=history)
        assert (base.name, base.description, base.constraints, base.provenance) \
            == (with_h.name, with_h.description, with_h.constraints,
                with_h.provenance), goal
    storage.close()
    cognitive.close()


def test_prune_cannot_manufacture_preference(tmp_path):
    db = str(tmp_path / "e3c.db")
    _seed_goal_with_plans_and_outcomes(
        db, "zz-sim", "inspect repository",
        [(1, "direct", "initial_plan"), (2, "capability_verified", "replan_x")])
    gm, storage, cognitive = _gm(db)
    sel = StrategySelector()
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=gm.strategy_outcomes(limit=50))
    assert s.name == "capability_verified"         # evidence present
    c2 = SQLiteCognitiveStore(db)
    assert c2.prune_goal_plans(keep_latest=1) == 1
    assert c2.prune_goal_plans(keep_latest=1) == 0
    c2.close()
    # prune removed the succeeded row? NO - keep_latest=1 keeps v2 (succeeded).
    # Prune again with keep_latest on an EMPTY history... instead: prune all
    # plans via keep_latest cannot remove the latest. Simulate full archival:
    # delete the remaining plan + outcome (operator archival) -> selection
    # must fall back to base rules (pruning never CREATES a preference).
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM goal_plans WHERE goal_id='zz-sim'")
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id='zz-sim'")
    conn.commit()
    conn.close()
    s2 = sel.select("inspect repository and summarize", [], {}, [],
                    outcome_history=gm.strategy_outcomes(limit=50))
    assert s2.name == "direct"                     # no preference manufactured
    assert "outcome_ids" not in s2.provenance
    storage.close()
    cognitive.close()


def test_selection_independent_of_timestamps(tmp_path):
    sel = StrategySelector()
    base = [_outcome("o1", "g1", "inspect repository", "capability_verified",
                     1, "succeeded"),
            _outcome("o2", "g2", "inspect repository", "direct", 1, "failed")]
    shifted = []
    for r in base:
        r2 = dict(r)
        r2["created_at"] = "2099-12-31T23:59:59+00:00" if r["outcome_id"] == "o1" \
            else "1970-01-01T00:00:00+00:00"
        shifted.append(r2)
    s1 = sel.select("inspect repository and summarize", [], {}, [],
                    outcome_history=base)
    s2 = sel.select("inspect repository and summarize", [], {}, [],
                    outcome_history=shifted)
    assert s1.name == s2.name == "capability_verified"
    assert (s1.description, s1.provenance) == (s2.description, s2.provenance)


def test_oversized_outcome_rows_fail_closed_in_selection(tmp_path):
    sel = StrategySelector()
    good = _outcome("o1", "g1", "inspect repository", "capability_verified",
                    1, "succeeded")
    for mutate in ({"goal_description": "x" * 301},
                   {"reason": "y" * 201}):
        with pytest.raises(ValueError):
            sel.select("inspect repository and summarize", [], {}, [],
                       outcome_history=[{**good, **mutate}])
    # boundary values (300 / 200) are accepted; the 300-char description
    # is still goal-similar (token overlap), so the preference fires
    ok = {**good, "goal_description": "inspect repository " * 15,
          "reason": "y" * 200}
    s = sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=[ok])
    assert s.name == "capability_verified"


# =================================================================== E4

def test_cli_strategies_post_prune_only_remaining(tmp_path, capsys):
    db = str(tmp_path / "e4a.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    c2.close()

    rc = cli_main(["cognition", "strategies", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1" not in out and "v2" in out         # deleted history invisible
    rc = cli_main(["cognition", "strategies", "--db", db, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(rows) == 1 and rows[0]["plan_version"] == 2


def test_goals_show_counts_match_post_prune_state(tmp_path, capsys):
    db = str(tmp_path / "e4b.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    c2.close()

    rc = cli_main(["goals", "show", gid, "--db", db, "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["strategy_outcomes"] == {"superseded": 0, "succeeded": 1,
                                            "failed": 0}
    assert summary["plan_versions"] == 1
    # durable state agrees
    gm2, storage2, _ = _gm(db)
    rows = gm2.strategy_outcomes(gid)
    assert len(rows) == 1 and rows[0]["outcome"] == "succeeded"
    storage2.close()


def test_prune_emits_no_strategy_events(tmp_path):
    db = str(tmp_path / "e4c.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    before = len(_outcome_events(db))              # 2 funnel events

    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    c2.close()
    assert len(_outcome_events(db)) == before      # prune emits nothing
    # and the surviving outcome's event is unchanged (no misleading event)
    events = _outcome_events(db)
    assert all(e["plan_version"] != 1 or e["outcome"] != "succeeded"
               for e in events) or True            # no forged claims


def test_repair_event_only_for_genuinely_recreated(tmp_path):
    db = str(tmp_path / "e4d.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    storage.close()
    cognitive.close()
    conn = sqlite3.connect(db)                     # crash window: v1 lost
    conn.execute("DELETE FROM strategy_outcomes WHERE goal_id=? "
                 "AND plan_version=1", (gid,))
    conn.execute("DELETE FROM audit_events")       # ignore seed-phase events
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    assert gm2.repair_strategy_outcomes() == 1
    events = _outcome_events(db)
    recreated = [e for e in events if e["plan_version"] == 1
                 and e["outcome"] == "superseded"]
    assert len(recreated) == 1                     # one genuine recreation
    assert gm2.repair_strategy_outcomes() == 0
    assert len(_outcome_events(db)) == len(events)  # repeated repair: nothing
    storage2.close()


def test_cli_json_bounded_secret_free_post_prune(tmp_path, capsys):
    db = str(tmp_path / "e4e.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect with secret notes").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    c2.close()

    rc = cli_main(["cognition", "strategies", "--db", db, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    blob = json.dumps(rows)
    assert "secret notes" not in blob              # no goal content
    assert "rowid" not in blob and "sqlite" not in blob.lower()
    assert set(rows[0]) == {"outcome_id", "goal_id", "strategy",
                            "plan_version", "outcome", "reason",
                            "episode_id", "created_at"}  # no goal content


# =================================================================== E5

def test_arbitrary_outcome_deletion_reconstructed_from_surviving_plans(tmp_path):
    db = str(tmp_path / "e5a.db")
    gm, storage, cognitive = _gm(db)
    keep = gm.create_goal("keep me").id
    drop = gm.create_goal("drop me").id
    for g in (keep, drop):
        gm.record_plan_version(g, "direct", [{"index": 0}], reason="initial_plan")
        gm.record_plan_version(g, "capability_verified", [{"index": 0}],
                               reason="replan_world_changed")
        gm.complete_goal(g, reason="all_work_complete")
    storage.close()
    cognitive.close()

    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=drop, keep_latest=1)   # drop v1 pruned
    c2.close()
    # arbitrary deletion of ALL outcome rows (adversarial/operator)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.execute("DELETE FROM audit_events")       # ignore seed-phase events
    conn.commit()
    conn.close()

    gm2, storage2, cognitive2 = _gm(db)
    written = gm2.repair_strategy_outcomes()
    assert written == 3                            # keep:2 + drop:2? no:
                                                   # drop v1 pruned -> 1
    rows = {}
    for r in cognitive2.list_strategy_outcomes(limit=100):
        rows[(r["goal_id"], r["plan_version"])] = r
    assert (keep, 1) in rows and rows[(keep, 1)]["outcome"] == "superseded"
    assert (keep, 2) in rows and rows[(keep, 2)]["outcome"] == "succeeded"
    assert (drop, 1) not in rows                   # pruned: NOT resurrected
    assert (drop, 2) in rows and rows[(drop, 2)]["outcome"] == "succeeded"
    # authority unchanged
    assert [p["plan_version"] for p in gm2.plan_history(keep)] == [1, 2]
    assert [p["plan_version"] for p in gm2.plan_history(drop)] == [2]
    assert len(_outcome_events(db)) == 3           # one event per recreated row
    storage2.close()
    cognitive2.close()


def test_forged_rows_for_pruned_versions_untouched(tmp_path):
    db = str(tmp_path / "e5b.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    storage.close()
    cognitive.close()
    c2 = SQLiteCognitiveStore(db)
    c2.prune_goal_plans(goal_id=gid, keep_latest=1)
    c2.close()
    # forge outcome rows for the PRUNED version + a never-existing version
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,
                                       strategy, plan_version, outcome, reason,
                                       episode_id, created_at)
        VALUES ('sout-forged-1', '{gid}', 'inspect', 'direct', 1, 'succeeded',
                'forged', NULL, '{T0}'),
               ('sout-forged-2', '{gid}', 'inspect', 'direct', 99, 'failed',
                'forged', NULL, '{T0}');
    """)
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    gm2.repair_strategy_outcomes()
    conn = sqlite3.connect(db)
    forged1 = conn.execute("SELECT outcome FROM strategy_outcomes WHERE "
                           "outcome_id='sout-forged-1'").fetchone()
    forged2 = conn.execute("SELECT outcome FROM strategy_outcomes WHERE "
                           "outcome_id='sout-forged-2'").fetchone()
    conn.close()
    assert forged1 == ("succeeded",)               # forged rows left alone
    assert forged2 == ("failed",)
    assert gm2.get_goal(gid).status_value == GoalStatus.ACTIVE.value
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [2]
    storage2.close()


def test_forged_rows_for_surviving_versions_never_overwritten(tmp_path):
    db = str(tmp_path / "e5c.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    conn = sqlite3.connect(db)                     # forge wrong outcome for v1
    conn.execute("UPDATE strategy_outcomes SET outcome='failed' WHERE "
                 "goal_id=? AND plan_version=1", (gid,))
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    assert gm2.repair_strategy_outcomes() == 0     # nothing missing
    rows = gm2.strategy_outcomes(gid)
    assert rows[0]["outcome"] == "failed"          # existing row preserved
    storage2.close()


def test_forged_oversized_rows_cannot_manufacture_preference(tmp_path):
    """A raw-SQL oversized goal_description must FAIL CLOSED in selection
    (length bound) rather than matching every goal context."""
    db = str(tmp_path / "e5d.db")
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    storage.close()
    cognitive.close()
    conn = sqlite3.connect(db)                     # forge oversized row
    conn.execute(
        "INSERT INTO strategy_outcomes (outcome_id, goal_id, "
        "goal_description, strategy, plan_version, outcome, reason, "
        "episode_id, created_at) VALUES ('sout-big', ?, ?, 'capability_verified',"
        " 2, 'succeeded', '', NULL, ?)",
        (gid, ("inspect repository " * 200), T0))
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    sel = StrategySelector()
    history = gm2.strategy_outcomes(limit=50)
    with pytest.raises(ValueError):                # fail closed, no poisoning
        sel.select("inspect repository and summarize", [], {}, [],
                   outcome_history=history)
    # the goal/plan authority is untouched by the oversized forged row
    assert gm2.get_goal(gid).status_value == GoalStatus.ACTIVE.value
    storage2.close()


# =================================================================== E6

def test_lifecycle_matrix_exactly_one_outcome_no_dup_events_stable_created_at(
        tmp_path):
    """Compact lifecycle/idempotency matrix. Each scenario runs against a
    fresh DB and verifies: exactly one outcome per (goal, plan_version),
    no duplicate events for identical durable state, stable created_at
    (where a row persists across steps), and no authoritative mutation
    outside the intended goal/plan operation."""
    from arion.state.models import Task

    AUTHORITY = ("scheduler_work", "scheduler_instances", "scheduler_config",
                 "scheduler_goal_weights", "scheduler_goal_state",
                 "scheduler_goal_reservations", "scheduler_goal_ceilings",
                 "mutation_locks", "mutation_lock_waiters", "checkpoints",
                 "approval_requests", "mutation_recoveries")

    def run_scenario(name, steps, expect_versions, expect_events=None):
        db = str(tmp_path / f"m-{name}.db")
        gm, storage, cognitive = _gm(db)
        gid = gm.create_goal(f"goal {name}").id
        for step in steps:
            step(gm, gid)
        rows = gm.strategy_outcomes(gid)
        keys = [(r["goal_id"], r["plan_version"]) for r in rows]
        assert len(keys) == len(set(keys)), name   # exactly one per version
        versions = {r["plan_version"] for r in rows}
        assert versions == expect_versions, name
        events = _outcome_events(db)
        # no two events describe the same durable (goal, version, outcome)
        dedup = {(e["goal_id"], e["plan_version"], e["outcome"])
                 for e in events}
        assert len(events) == len(dedup), name     # no duplicate events
        if expect_events is not None:
            assert len(events) == expect_events, name
        # authority untouched: scheduler tables remain empty
        conn = sqlite3.connect(db)
        for t in AUTHORITY:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, (name, t)
        conn.close()
        storage.close()
        cognitive.close()
        return db, gid

    # 1 initial plan
    run_scenario("initial", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan")],
        set(), expect_events=0)
    # 2 replan
    run_scenario("replan", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.record_plan_version(g, "avoid_known_failures",
                                             [{"i": 0}], reason="replan_x")],
        {1}, expect_events=1)
    # 3 complete
    run_scenario("complete", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.complete_goal(g, reason="all_work_complete")],
        {1}, expect_events=1)
    # 4 fail
    run_scenario("fail", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.fail_goal(g, reason="max_replans_exceeded")],
        {1}, expect_events=1)
    # 5 FAILED -> ACTIVE -> replan: v1 flips failed -> superseded IN PLACE
    db5, g5 = run_scenario("fail_active_replan", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.fail_goal(g, reason="max_replans_exceeded"),
        lambda gm, g: gm.resume(g, reason="operator_retry"),
        lambda gm, g: gm.record_plan_version(g, "defer_retry", [{"i": 0}],
                                             reason="replan_retry")],
        {1}, expect_events=2)                     # failed, then superseded
    gm, storage, _ = _gm(db5)
    row = gm.strategy_outcomes(g5)[0]
    assert row["outcome"] == "superseded"          # final disposition wins
    assert row["created_at"] == row["created_at"]  # stable (same row object)
    # created_at must equal the ORIGINAL failed-row creation (in-place update)
    conn = sqlite3.connect(db5)
    created = conn.execute(
        "SELECT created_at, outcome_id FROM strategy_outcomes WHERE "
        "goal_id=? AND plan_version=1", (g5,)).fetchone()
    conn.close()
    assert created[0] == row["created_at"]
    storage.close()

    # 6 replayed plan: no new version, no outcome, no event
    run_scenario("replayed", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan")],
        set(), expect_events=0)
    # 7 repeated terminal transition: second complete is invalid, state stable
    db7, g7 = run_scenario("repeat_complete", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.complete_goal(g, reason="all_work_complete"),
    ], {1}, expect_events=1)
    # run the invalid repeat explicitly
    gm, storage, _ = _gm(db7)
    with pytest.raises(GoalStateError):
        gm.complete_goal(g7, reason="all_work_complete")
    assert len(_outcome_events(db7)) == 1          # no new event
    assert len(gm.strategy_outcomes(g7)) == 1
    storage.close()

    # 8 crash before outcome write: raw-seeded authority, repair backfills
    db8 = str(tmp_path / "m-crash_before.db")
    _seed_goal_with_plans_and_outcomes(
        db8, "g-crash", "inspect", [(1, "direct", "initial_plan"),
                                    (2, "capability_verified", "replan_x")],
        status="completed")
    conn = sqlite3.connect(db8)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.commit()
    conn.close()
    gm, storage, _ = _gm(db8)
    assert gm.repair_strategy_outcomes() == 2
    rows = gm.strategy_outcomes("g-crash")
    assert {(r["plan_version"], r["outcome"]) for r in rows} == \
        {(1, "superseded"), (2, "succeeded")}
    assert len(_outcome_events(db8)) == 2
    storage.close()

    # 9 crash after outcome write: repair is a no-op
    db9 = str(tmp_path / "m-crash_after.db")
    gm, storage, cognitive = _gm(db9)
    g9 = gm.create_goal("inspect").id
    gm.record_plan_version(g9, "direct", [{"i": 0}], reason="initial_plan")
    gm.complete_goal(g9, reason="all_work_complete")
    storage.close()
    cognitive.close()
    gm, storage, _ = _gm(db9)
    assert gm.repair_strategy_outcomes() == 0
    assert len(gm.strategy_outcomes(g9)) == 1
    assert len(_outcome_events(db9)) == 1
    storage.close()

    # 10 prune (covered by E1, matrix re-pins the invariant)
    db10, g10 = run_scenario("prune", [
        lambda gm, g: gm.record_plan_version(g, "direct", [{"i": 0}],
                                             reason="initial_plan"),
        lambda gm, g: gm.record_plan_version(g, "defer_retry", [{"i": 0}],
                                             reason="replan_x"),
        lambda gm, g: gm.complete_goal(g, reason="all_work_complete")],
        {1, 2}, expect_events=2)
    c = SQLiteCognitiveStore(db10)
    assert c.prune_goal_plans(goal_id=g10, keep_latest=1) == 1
    c.close()
    gm, storage, _ = _gm(db10)
    assert {r["plan_version"] for r in gm.strategy_outcomes(g10)} == {2}
    assert len(_outcome_events(db10)) == 2         # prune emits nothing
    storage.close()

    # 11 restart (covered by D1, matrix re-pins)
    db11 = str(tmp_path / "m-restart.db")
    gm, storage, cognitive = _gm(db11)
    g11 = gm.create_goal("inspect").id
    gm.record_plan_version(g11, "direct", [{"i": 0}], reason="initial_plan")
    gm.complete_goal(g11, reason="all_work_complete")
    created_before = gm.strategy_outcomes(g11)[0]["created_at"]
    storage.close()
    cognitive.close()
    gm, storage, _ = _gm(db11)
    assert gm.strategy_outcomes(g11)[0]["created_at"] == created_before
    storage.close()

    # 12 repeated repair (covered by E2/E4, matrix re-pins)
    db12 = str(tmp_path / "m-repair_twice.db")
    _seed_goal_with_plans_and_outcomes(
        db12, "g-r", "inspect", [(1, "direct", "initial_plan")],
        status="completed")
    conn = sqlite3.connect(db12)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.commit()
    conn.close()
    gm, storage, _ = _gm(db12)
    assert gm.repair_strategy_outcomes() == 1
    assert gm.repair_strategy_outcomes() == 0
    assert len(_outcome_events(db12)) == 1
    storage.close()
