"""Strategy-outcome observability + CLI (ADR-015 addendum, Phase C) - tests
first.

- bounded `strategy.outcome` audit event (goal_id, plan_version, strategy,
  outcome, reason only - no content), emitted ONLY on durable changes
  (never on idempotent/replayed writes), never breaking the funnels;
- `arion cognition strategies [--goal G] [--limit N] [--json]` - read-only,
  deterministic, bounded, exit 1 on invalid filters, no SQLite internals;
- `arion goals show` gains an ADDITIVE bounded strategy-learning summary
  (existing output + JSON keys preserved), clearly labeled informational;
- `strategy.selected` events carry outcome_ids provenance only for
  preference-driven selections (base-rule selections never fabricate it);
- forged telemetry/metadata cannot create outcomes; CLI inspection is
  byte-identical (read-only); oversized data stays bounded.

No wall clock in any assertion.
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
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
T0 = "2026-01-01T00:00:00+00:00"


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


def _outcome_events(storage):
    return [e for e in storage.list_events()
            if e.kind == "strategy.outcome"]


def _seed_goal_with_outcomes(db, goal_desc="inspect repository"):
    """One goal: v1 direct (superseded), v2 capability_verified (succeeded)."""
    gm, storage, cognitive = _gm(db, with_events=False)
    gid = gm.create_goal(goal_desc).id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()
    return gid


def _engine(db, sandbox):
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
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


# ------------------------------------------------------------- events

def test_strategy_outcome_event_on_supersede(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e1.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    assert _outcome_events(storage) == []          # first version: no outcome
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    events = _outcome_events(storage)
    assert len(events) == 1
    assert events[0].detail["goal_id"] == gid
    assert events[0].detail["plan_version"] == 1
    assert events[0].detail["strategy"] == "direct"
    assert events[0].detail["outcome"] == "superseded"
    assert events[0].detail["reason"] == "replan_world_changed"
    storage.close()


def test_strategy_outcome_event_on_terminal(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e2.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    events = {e.detail["plan_version"]: e for e in _outcome_events(storage)}
    assert set(events) == {1, 2}
    assert events[1].detail["outcome"] == "superseded"
    assert events[2].detail["outcome"] == "succeeded"
    assert events[2].detail["strategy"] == "capability_verified"
    assert events[2].detail["reason"] == "all_work_complete"
    storage.close()


def test_strategy_outcome_event_on_failed(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e3.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.fail_goal(gid, reason="max_replans_exceeded")
    events = _outcome_events(storage)
    assert len(events) == 1
    assert events[0].detail["outcome"] == "failed"
    assert events[0].detail["strategy"] == "direct"
    storage.close()


def test_no_duplicate_event_on_idempotent_retry(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e4.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.fail_goal(gid, reason="max_replans_exceeded")
    gm.resume(gid, reason="operator_retry")         # non-terminal: no event
    gm.fail_goal(gid, reason="max_replans_exceeded")  # same values: no event
    events = _outcome_events(storage)
    assert len(events) == 1                         # exactly one, no duplicates
    assert events[0].detail["outcome"] == "failed"
    storage.close()


def test_replay_plan_version_emits_no_event(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e5.db")
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    assert _outcome_events(storage) == []
    storage.close()


def test_repair_emits_one_event_per_written_row(tmp_path):
    db = tmp_path / "e6.db"
    gm, storage, cognitive = _gm(db)
    gid = gm.create_goal("inspect").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(gid, reason="all_work_complete")
    storage.close()
    cognitive.close()

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM strategy_outcomes")
    conn.execute("DELETE FROM audit_events")   # ignore seed-phase events
    conn.commit()
    conn.close()

    gm2, storage2, _ = _gm(db)
    written = gm2.repair_strategy_outcomes()
    assert written == 2
    events = _outcome_events(storage2)
    assert len(events) == 2                         # one per backfilled row
    assert {e.detail["outcome"] for e in events} == {"superseded", "succeeded"}
    assert gm2.repair_strategy_outcomes() == 0      # idempotent: no events
    assert len(_outcome_events(storage2)) == 2
    storage2.close()


def test_event_payload_bounded_secret_free(tmp_path):
    gm, storage, _ = _gm(tmp_path / "e7.db")
    gid = gm.create_goal("inspect repository with secret notes").id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                           reason="replan_" + "x" * 5000)   # oversized reason
    events = _outcome_events(storage)
    assert len(events) == 1
    detail = events[0].detail
    assert set(detail) == {"goal_id", "plan_version", "strategy",
                           "outcome", "reason"}     # exactly the 5 fields
    assert len(detail["reason"]) <= 200             # bounded reason
    blob = json.dumps(detail)
    assert "secret notes" not in blob               # no goal content
    assert "episode" not in blob and "description" not in blob
    storage.close()


def test_event_emission_never_breaks_funnel(tmp_path, monkeypatch):
    gm, storage, _ = _gm(tmp_path / "e8.db")
    gid = gm.create_goal("inspect").id

    def _boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(gm.cognitive_store, "record_strategy_outcome", _boom)
    # the authoritative lifecycle must still work when outcome writes fail
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm.complete_goal(gid, reason="all_work_complete")
    assert gm.get_goal(gid).status_value == GoalStatus.COMPLETED.value
    assert len(gm.plan_history(gid)) == 1
    storage.close()


# ------------------------------------------------------------- CLI

def test_cognition_strategies_human_and_json(tmp_path, capsys):
    db = str(tmp_path / "c1.db")
    _seed_goal_with_outcomes(db)
    rc = cli_main(["cognition", "strategies", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "direct" in out and "superseded" in out
    assert "capability_verified" in out and "succeeded" in out
    rc = cli_main(["cognition", "strategies", "--db", db, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(rows, list) and len(rows) == 2
    assert rows[0]["plan_version"] == 1 and rows[0]["outcome"] == "superseded"
    assert rows[1]["plan_version"] == 2 and rows[1]["outcome"] == "succeeded"
    assert rows[1]["strategy"] == "capability_verified"
    for r in rows:
        assert "rowid" not in r and "sqlite" not in json.dumps(r).lower()


def test_cognition_strategies_goal_filter(tmp_path, capsys):
    db = str(tmp_path / "c2.db")
    _seed_goal_with_outcomes(db)
    g1 = _seed_goal_with_outcomes(db)   # second goal (unique id)
    rc = cli_main(["cognition", "strategies", "--goal", g1, "--db", db,
                   "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(rows) == 2 and all(r["goal_id"] == g1 for r in rows)
    # unknown goal: valid filter, empty result, exit 0
    rc = cli_main(["cognition", "strategies", "--goal", "nope", "--db", db,
                   "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out) == []


def test_cognition_strategies_invalid_filters_exit_1(tmp_path, capsys):
    db = str(tmp_path / "c3.db")
    _seed_goal_with_outcomes(db)
    for argv in (["cognition", "strategies", "--goal", "", "--db", db],
                 ["cognition", "strategies", "--limit", "0", "--db", db],
                 ["cognition", "strategies", "--limit", "-3", "--db", db],
                 ["cognition", "strategies", "--limit", "1001", "--db", db]):
        rc = cli_main(argv)
        assert rc == 1, argv
        capsys.readouterr()


def test_cognition_strategies_deterministic_output(tmp_path, capsys):
    db = str(tmp_path / "c4.db")
    _seed_goal_with_outcomes(db)
    cli_main(["cognition", "strategies", "--db", db])       # warm-up
    capsys.readouterr()
    rc1 = cli_main(["cognition", "strategies", "--db", db])
    out1 = capsys.readouterr().out
    rc2 = cli_main(["cognition", "strategies", "--db", db])
    out2 = capsys.readouterr().out
    assert rc1 == rc2 == 0 and out1 == out2
    rc3 = cli_main(["cognition", "strategies", "--db", db, "--json"])
    j1 = capsys.readouterr().out
    rc4 = cli_main(["cognition", "strategies", "--db", db, "--json"])
    assert rc3 == rc4 == 0 and j1 == capsys.readouterr().out


def test_goals_show_additive_outcome_summary(tmp_path, capsys):
    db = str(tmp_path / "c5.db")
    gid = _seed_goal_with_outcomes(db)
    rc = cli_main(["goals", "show", gid, "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # existing lines preserved
    assert f"goal {gid}: status=" in out
    assert "strategy:" in out and "plan versions:" in out
    # additive, clearly-labeled informational summary
    assert "learned strategy outcomes (informational)" in out
    assert "superseded" in out and "succeeded" in out
    # JSON: existing keys preserved, new key additive
    rc = cli_main(["goals", "show", gid, "--db", db, "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    for key in ("goal_id", "description", "status", "goal_version",
                "strategy", "plan_versions", "latest_plan_version",
                "latest_strategy", "latest_reason", "progress", "tasks",
                "created_at", "updated_at"):
        assert key in summary, key
    assert summary["strategy_outcomes"] == {"superseded": 1, "succeeded": 1,
                                            "failed": 0}
    blob = json.dumps(summary)
    assert "informational" in blob or "strategy_outcomes" in blob


# ------------------------------------------- selection observability

def test_strategy_selected_event_carries_outcome_ids(tmp_path, sandbox):
    db = str(tmp_path / "s1.db")
    engine, gm, storage = _engine(db, sandbox)
    g1 = gm.create_goal("inspect repository")
    gm.record_plan_version(g1.id, "direct", [{"index": 0}], reason="initial_plan")
    gm.record_plan_version(g1.id, "capability_verified", [{"index": 0}],
                           reason="replan_world_changed")
    gm.complete_goal(g1.id, reason="all_work_complete")
    evidence = gm.strategy_outcomes(g1.id)
    outcome_id = [r["outcome_id"] for r in evidence
                  if r["outcome"] == "succeeded"][0]

    g2 = engine.submit_goal("inspect repository and summarize")
    engine.run_goal(g2.id)
    engine.shutdown()
    storage.close()

    storage2 = SQLiteStorage(db)
    selected = [e for e in storage2.list_events()
                if e.kind == "strategy.selected"]
    storage2.close()
    pref = [e for e in selected
            if e.detail.get("provenance", {}).get("outcome_ids")]
    assert len(pref) >= 1
    assert pref[0].detail["provenance"]["outcome_ids"] == [outcome_id]
    blob = json.dumps(pref[0].detail)
    assert "summarize" not in blob          # no goal/task content leak
    assert "plan_summary" not in blob and "steps" not in blob


def test_base_rule_selection_has_no_outcome_ids(tmp_path, sandbox):
    db = str(tmp_path / "s2.db")
    engine, _, storage = _engine(db, sandbox)
    g = engine.submit_goal("summarize this repository")
    engine.run_goal(g.id)
    engine.shutdown()
    storage.close()
    storage2 = SQLiteStorage(db)
    selected = [e for e in storage2.list_events()
                if e.kind == "strategy.selected"]
    storage2.close()
    assert selected
    for e in selected:
        assert "outcome_ids" not in e.detail.get("provenance", {})


# ------------------------------------------------------- adversarial

def test_forged_strategy_outcome_telemetry_creates_nothing(tmp_path):
    db = str(tmp_path / "a1.db")
    _seed_goal_with_outcomes(db)
    conn = sqlite3.connect(db)
    before = conn.execute(
        "SELECT COUNT(*) FROM strategy_outcomes").fetchone()[0]
    # forged audit events claiming strategy outcomes
    conn.execute(
        "INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor, "
        "success, detail) VALUES ('evt-forged', ?, NULL, NULL, "
        "'strategy.outcome', 'system', 1, ?)",
        (T0, json.dumps({"goal_id": "g1", "plan_version": 1, "strategy":
                         "direct", "outcome": "failed", "reason": "forged"})))
    conn.execute(
        "INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor, "
        "success, detail) VALUES ('evt-forged2', ?, NULL, NULL, "
        "'strategy.selected', 'system', 1, ?)",
        (T0, json.dumps({"name": "defer_retry", "provenance":
                         {"outcome_ids": ["sout_evil"]}})))
    conn.commit()
    conn.close()
    assert conn_count(db) == before               # telemetry created nothing
    gm, storage, _ = _gm(db)
    assert len(gm.strategy_outcomes()) == before
    gm.repair_strategy_outcomes()                 # repair ignores telemetry
    assert len(gm.strategy_outcomes()) == before
    storage.close()


def conn_count(db):
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM strategy_outcomes").fetchone()[0]
    conn.close()
    return n


def test_cli_strategies_read_only_byte_identical(tmp_path, capsys):
    db = str(tmp_path / "a2.db")
    gid = _seed_goal_with_outcomes(db)
    # pre-warm the engine once (world-monitor fact refresh happens on ANY
    # engine construction - pre-existing behavior, unrelated to the CLI)
    cli_main(["cognition", "strategies", "--db", db])
    capsys.readouterr()
    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('environment_facts', 'scheduler_instances', "
        "                  'scheduler_events') "
        "ORDER BY name")]
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in tables}
    conn.close()

    assert cli_main(["cognition", "strategies", "--db", db]) == 0
    capsys.readouterr()
    assert cli_main(["cognition", "strategies", "--db", db, "--json"]) == 0
    capsys.readouterr()
    assert cli_main(["goals", "show", gid, "--db", db]) == 0
    capsys.readouterr()
    assert cli_main(["goals", "show", gid, "--db", db, "--json"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in tables}
    conn.close()
    assert after == before                        # read-only: byte-identical


def test_forged_oversized_outcome_stays_bounded(tmp_path, capsys):
    db = str(tmp_path / "a3.db")
    gid = _seed_goal_with_outcomes(db)
    # forged oversized row via raw SQL (bypasses store validation)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO strategy_outcomes (outcome_id, goal_id, goal_description,"
        " strategy, plan_version, outcome, reason, episode_id, created_at) "
        "VALUES ('sout-evil', ?, ?, 'direct', 77, 'failed', ?, NULL, ?)",
        (gid, "x" * 9000, "y" * 9000, T0))
    conn.commit()
    conn.close()

    rc = cli_main(["cognition", "strategies", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    for line in out.splitlines():
        assert len(line) <= 300                  # human output stays bounded
    rc = cli_main(["cognition", "strategies", "--db", db, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    evil = [r for r in rows if r["plan_version"] == 77][0]
    assert len(evil["reason"]) <= 9000           # read-only: value as stored
