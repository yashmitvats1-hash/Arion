"""ADR-016 addendum Phase C: CLI + observability - tests first.

Three surfaces:

1. `arion goals diff <goal_id> <va> <vb> [--json]` - read-only structural
   diff of two immutable plan versions. Deterministic; bounded; stable
   JSON schema; no free-text step content; exit 1 fail-closed on invalid
   input; identical versions -> explicit empty diff; NO DB mutation, NO
   events, NO planner invocation, NO execution.
2. `arion goals rollback <goal_id> <version>` - thin CLI wrapper around
   GoalManager.readopt_plan() (no second mechanism); normal immutable new
   version; reason replan_rollback_v<N>; ADR-015 supersede intact; normal
   plan.versioned + strategy.outcome events; replay-safe; fail-closed for
   invalid/pruned/latest/cross-goal/terminal; deterministic bounded output.
3. `arion goals progress` - genuinely READ-ONLY: no progress_metadata /
   last_evaluated_at / goal / plan / task / audit changes; repeated
   invocation byte-identical; deterministic JSON+human; no planner; no
   strategy-outcome changes; no scheduler changes.

All assertions deterministic; no wall clock.
"""

from __future__ import annotations

import json
import sqlite3

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


def _seed_goal(db, with_events=True, three_versions=True):
    """Goal with 3 versions: v1 [read a.md], v2 [list ., read b.md],
    v3 [read a.md, list .]. Returns (gm, storage, cognitive, gid)."""
    gm, storage, cognitive = _gm(db, with_events=with_events)
    gid = gm.create_goal("inspect repository").id
    gm.record_plan_version(gid, "direct", [_step(0, path="a.md").to_dict()],
                           reason="initial_plan")
    if three_versions:
        gm.record_plan_version(
            gid, "avoid_known_failures",
            [_step(0, action="list", path=".").to_dict(),
             _step(1, path="b.md").to_dict()],
            reason="replan_task_failed")
        gm.record_plan_version(
            gid, "capability_verified",
            [_step(0, path="a.md").to_dict(), _step(1, action="list", path=".").to_dict()],
            reason="replan_world_changed")
    return gm, storage, cognitive, gid


def _dump_tables(db, exclude=()):
    conn = sqlite3.connect(db)
    if exclude:
        marks = ", ".join("?" * len(exclude))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT IN (" + marks + ") "
            "ORDER BY name", list(exclude))]
    else:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
           for t in tables}
    conn.close()
    return out


def _cli(argv):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _event_kinds(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT kind, detail FROM audit_events ORDER BY rowid").fetchall()
    conn.close()
    return [(k, json.loads(d)) for k, d in rows]


# ============================================================ goals diff

def test_diff_human_and_json(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    assert rc == 0
    assert "v1 (direct) vs v2 (avoid_known_failures)" in out
    assert "added:" in out and "removed:" in out and "kept:" in out
    # JSON stable schema
    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
    assert rc == 0
    d = json.loads(out)
    assert set(d) == {"goal_id", "version_a", "version_b", "strategy_a",
                      "strategy_b", "reason_a", "reason_b", "steps_a",
                      "steps_b", "added", "removed", "kept", "identical"}
    assert d["goal_id"] == gid
    assert d["version_a"] == 1 and d["version_b"] == 2
    assert d["strategy_a"] == "direct"
    assert d["strategy_b"] == "avoid_known_failures"
    assert d["added"] == [0, 1] and d["removed"] == [0]
    assert d["kept"] == []
    assert d["identical"] is False


def test_diff_identical_versions_empty_deterministic(tmp_path, capsys):
    db = str(tmp_path / "di.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    rc, out = _cli(["goals", "diff", gid, "1", "1", "--db", db, "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["identical"] is True
    assert d["added"] == [] and d["removed"] == [] and d["kept"] == [0]
    # deterministic across runs
    rc2, out2 = _cli(["goals", "diff", gid, "1", "1", "--db", db, "--json"])
    assert out == out2


def test_diff_kept_steps(tmp_path, capsys):
    """v2 [list ., read b.md] vs v3 [read a.md, list .]: step 1 (list .)
    appears in both at the same index? No - indexes differ; kept is empty.
    Use v1 [read a.md] vs v3 [read a.md, list .]: step 0 kept."""
    db = str(tmp_path / "dk.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    rc, out = _cli(["goals", "diff", gid, "1", "3", "--db", db, "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["kept"] == [0]                     # read a.md kept (same index)
    assert d["added"] == [1]                    # list . added
    assert d["removed"] == []


def test_diff_bounded_no_content(tmp_path, capsys):
    db = str(tmp_path / "db.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
    assert rc == 0
    blob = out
    assert "a.md" not in blob                    # no free-text params content
    assert "intent" not in blob                  # no step intents
    assert "params" not in blob
    # human output bounded lines
    rc, out = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    assert rc == 0
    for line in out.splitlines():
        assert len(line) <= 200


def test_diff_fail_closed(tmp_path, capsys):
    db = str(tmp_path / "df.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    other = gm.create_goal("other goal").id
    storage.close()
    cognitive.close()

    for argv in (
        ["goals", "diff", "nonexistent", "1", "2", "--db", db],   # no goal
        ["goals", "diff", gid, "99", "2", "--db", db],            # bad va
        ["goals", "diff", gid, "1", "99", "--db", db],            # bad vb
        ["goals", "diff", gid, "0", "2", "--db", db],
        ["goals", "diff", gid, "1", "-1", "--db", db],
        ["goals", "diff", gid, "abc", "2", "--db", db],
        ["goals", "diff", gid, "1", "2.5", "--db", db],
    ):
        rc, _ = _cli(argv)
        assert rc == 1, argv
    # cross-goal: version 2 belongs to gid, not `other`
    rc, _ = _cli(["goals", "diff", other, "1", "2", "--db", db])
    assert rc == 1


def test_diff_pruned_version_fail_closed(tmp_path, capsys):
    db = str(tmp_path / "dp.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()
    c = SQLiteCognitiveStore(db)
    assert c.prune_goal_plans(goal_id=gid, keep_latest=2) == 1   # v1 pruned
    c.close()

    rc, _ = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    assert rc == 1                             # pruned version: fail closed


def test_diff_read_only_no_events_no_mutation(tmp_path, capsys):
    db = str(tmp_path / "dr.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()
    cli_main(["goals", "list", "--db", db])    # warm-up (engine startup)
    capsys.readouterr()
    before = _dump_tables(db, exclude=("scheduler_instances", "scheduler_events",
                                       "environment_facts"))
    events_before = _event_kinds(db)

    rc, _ = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    assert rc == 0
    rc, _ = _cli(["goals", "diff", gid, "1", "2", "--db", db, "--json"])
    assert rc == 0
    capsys.readouterr()

    assert _dump_tables(db, exclude=("scheduler_instances", "scheduler_events",
                                     "environment_facts")) == before
    assert _event_kinds(db) == events_before   # no new events
    # no planner: nothing would invoke it anyway (diff is store-level)


def test_diff_no_planner_invocation(tmp_path, capsys, monkeypatch):
    """diff must not invoke the planner (spy raises if called)."""
    db = str(tmp_path / "dn.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    def _boom(*a, **k):
        raise AssertionError("planner invoked by diff")

    monkeypatch.setattr(DeterministicPlanner, "plan", _boom)
    rc, _ = _cli(["goals", "diff", gid, "1", "2", "--db", db])
    assert rc == 0


# ========================================================= goals rollback

def test_rollback_uses_readopt_plan(tmp_path, capsys, monkeypatch):
    """rollback delegates to GoalManager.readopt_plan (single mechanism)."""
    db = str(tmp_path / "r.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    called = {}

    def _spy(self, goal_id, from_version):
        called["args"] = (goal_id, from_version)
        return {"plan_version": 4, "strategy": "direct",
                "reason": "replan_rollback_v1"}

    monkeypatch.setattr(GoalManager, "readopt_plan", _spy)
    rc, out = _cli(["goals", "rollback", gid, "1", "--db", db])
    assert rc == 0
    assert called["args"] == (gid, 1)
    assert "v4" in out and "replan_rollback_v1" in out


def test_rollback_creates_normal_version_and_events(tmp_path, capsys):
    db = str(tmp_path / "rv.db")
    gm, storage, cognitive, gid = _seed_goal(db, with_events=True)
    events_before = len(_event_kinds(db))
    storage.close()
    cognitive.close()

    rc, out = _cli(["goals", "rollback", gid, "1", "--db", db])
    assert rc == 0
    assert "replan_rollback_v1" in out
    gm2, storage2, cognitive2 = _gm(db)
    history = gm2.plan_history(gid)
    assert [p["plan_version"] for p in history] == [1, 2, 3, 4]
    assert history[-1]["reason"] == "replan_rollback_v1"
    assert history[-1]["strategy"] == "direct"
    # ADR-015 supersede + events through the normal funnels
    rows = {r["plan_version"]: r for r in gm2.strategy_outcomes(gid)}
    assert rows[3]["outcome"] == "superseded"
    assert rows[3]["reason"] == "replan_rollback_v1"
    kinds = [k for k, _ in _event_kinds(db)]
    assert kinds.count("plan.versioned") == 4    # 3 seed + 1 rollback
    assert kinds.count("strategy.outcome") == 3  # 2 seed + 1 rollback supersede
    storage2.close()
    cognitive2.close()


def test_rollback_replay_safe(tmp_path, capsys):
    db = str(tmp_path / "rr.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()

    rc1, out1 = _cli(["goals", "rollback", gid, "1", "--db", db])
    rc2, out2 = _cli(["goals", "rollback", gid, "1", "--db", db])
    assert rc1 == rc2 == 0
    assert out1 == out2                          # deterministic
    gm2, storage2, cognitive2 = _gm(db)
    assert [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3, 4]
    assert len(gm2.strategy_outcomes(gid)) == 3  # no duplicate outcome
    storage2.close()
    cognitive2.close()


def test_rollback_fail_closed(tmp_path, capsys):
    db = str(tmp_path / "rf.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    done = gm.create_goal("done goal").id
    gm.record_plan_version(done, "direct", [_step(0).to_dict()],
                           reason="initial_plan")
    gm.complete_goal(done, reason="all_work_complete")
    storage.close()
    cognitive.close()

    for argv in (
        ["goals", "rollback", "nonexistent", "1", "--db", db],
        ["goals", "rollback", gid, "99", "--db", db],
        ["goals", "rollback", gid, "3", "--db", db],   # latest version
        ["goals", "rollback", gid, "0", "--db", db],
        ["goals", "rollback", done, "1", "--db", db],  # terminal completed
    ):
        rc, _ = _cli(argv)
        assert rc == 1, argv


def test_rollback_pruned_version_fail_closed(tmp_path, capsys):
    db = str(tmp_path / "rp.db")
    gm, storage, cognitive, gid = _seed_goal(db)
    storage.close()
    cognitive.close()
    c = SQLiteCognitiveStore(db)
    c.prune_goal_plans(goal_id=gid, keep_latest=2)
    c.close()

    rc, _ = _cli(["goals", "rollback", gid, "1", "--db", db])
    assert rc == 1


# ==================================================== progress read-only

def test_progress_read_only_no_mutation(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    sandbox = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "repo"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sandbox)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    cli_main(["goals", "list", "--db", db])      # warm-up
    capsys.readouterr()
    before = _dump_tables(db, exclude=("scheduler_instances", "scheduler_events",
                                       "environment_facts"))
    events_before = _event_kinds(db)

    rc1, out1 = _cli(["goals", "progress", gid, "--db", db])
    rc2, out2 = _cli(["goals", "progress", gid, "--db", db])
    assert rc1 == rc2 == 0
    assert out1 == out2                          # repeated byte-identical
    assert "progress=" in out1 and "status=" in out1

    rc3, out3 = _cli(["goals", "progress", gid, "--db", db, "--json"])
    rc4, out4 = _cli(["goals", "progress", gid, "--db", db, "--json"])
    assert rc3 == rc4 == 0 and out3 == out4

    assert _dump_tables(db, exclude=("scheduler_instances", "scheduler_events",
                                     "environment_facts")) == before
    assert _event_kinds(db) == events_before     # no progress.evaluated events


def test_progress_read_only_no_planner_no_outcomes(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "pn.db")
    sandbox = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "repo"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sandbox)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    def _boom(*a, **k):
        raise AssertionError("planner invoked by progress")

    monkeypatch.setattr(DeterministicPlanner, "plan", _boom)
    gm1, storage1, cognitive1 = _gm(db)
    outcomes_before = gm1.strategy_outcomes(gid)
    storage1.close()
    cognitive1.close()
    rc, out = _cli(["goals", "progress", gid, "--db", db])
    assert rc == 0
    gm2, storage2, cognitive2 = _gm(db)
    assert gm2.strategy_outcomes(gid) == outcomes_before  # unchanged
    storage2.close()
    cognitive2.close()


def test_progress_read_only_no_authority_change(tmp_path, capsys):
    db = str(tmp_path / "pa.db")
    sandbox = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "repo"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# R\n")
    engine, gm, storage = _engine(db, sandbox)
    gid = engine.submit_goal("summarize this repository").id
    engine.run_goal(gid, max_replans=1)
    engine.shutdown()
    storage.close()

    authority = ("scheduler_config", "scheduler_goal_weights",
                 "scheduler_goal_state", "scheduler_goal_reservations",
                 "scheduler_goal_ceilings", "mutation_locks",
                 "mutation_lock_waiters", "approval_requests",
                 "mutation_recoveries", "checkpoints")
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
              for t in authority}
    conn.close()

    rc, _ = _cli(["goals", "progress", gid, "--db", db])
    assert rc == 0
    conn = sqlite3.connect(db)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
             for t in authority}
    conn.close()
    assert after == before
