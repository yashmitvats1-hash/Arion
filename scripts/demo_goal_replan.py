#!/usr/bin/env python3
"""ADR-016 Definition-of-Done demo: durable goal management + replanning.

Runs a REAL multi-cycle goal ("inspect this repository and produce useful
notes") against a sandboxed repo with race conditions:

  Cycle 1  - plan v1 (strategy `direct`): the task fails on a known
             bad/binary resource (README.md); the failure is persisted.
  Cycle 2  - reevaluation: memory/reflection/cognition evidence is retrieved,
             the strategy changes to `avoid_known_failures`, plan v2 is
             produced; authorization independently approves every step; the
             task hits a RACE (docs/design.md turned binary mid-goal) and
             fails - the new failure is persisted too.
  Cycle 3  - an irrelevant world change (system_uptime) does NOT trigger a
             replan; a material world change (registered_capabilities) DOES:
             plan v3 (strategy `defer_retry` escalation) is produced, all
             previous plan versions stay immutable, and the goal completes.
  Restart  - a fresh process is simulated against the SAME database between
             Cycle 2 and Cycle 3: goal state / plan versions / progress /
             provenance survive, no plan version is duplicated, and
             authorization is re-evaluated from CURRENT live metadata (a
             tightened ActionSpec is honored - old successes are never reused).

Deterministic, offline, no LLM (ADR-008). Exits non-zero on any failed check.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _sandbox(root: Path) -> Path:
    """A repo with one KNOWN-BAD binary file, one text file, and docs/."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_bytes(b"\xff\xfe\x00binary payload")  # not text
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "design.md").write_text("# Design\n\nreadable\n", encoding="utf-8")
    return root


def _build_engine(db_path: Path, sandbox: Path, registry: CapabilityRegistry | None = None):
    """Wire a full deterministic spine over one SQLite DB (fresh process)."""
    storage = SQLiteStorage(db_path)
    registry = registry or CapabilityRegistry()
    if not registry.has("filesystem.read"):
        registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage, memory, world_monitor


def _snapshot_plans(gm, gid):
    return copy.deepcopy(gm.plan_history(gid))


def _plan_table(gm, gid):
    return [(p["plan_version"], p["reason"], p["strategy"]) for p in gm.plan_history(gid)]


def _event_kinds(storage, task_id=None):
    return [e.kind for e in storage.list_events(task_id)]


def _checks(failures, label, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(f"{label}: {detail}")


# --------------------------------------------------------------------------- #
# demo
# --------------------------------------------------------------------------- #

def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr016-demo-"))
    sandbox = _sandbox(tmp / "repo")
    db = tmp / "arion.db"

    print("=" * 78)
    print("ADR-016 DEMO: durable goals, progress evaluation, replanning, restart")
    print(f"db={db}  sandbox={sandbox}")
    print("=" * 78)

    # ------------------------------------------------------------------ #
    # Phase 0: fresh process, goal submission (restart-safe boundary #1)
    # ------------------------------------------------------------------ #
    print("\n[Phase 0] fresh process; submit goal")
    engine, gm, storage, memory, world_monitor = _build_engine(db, sandbox)
    goal = engine.submit_goal("inspect this repository and produce useful notes")
    gid = goal.id
    print(f"  goal {gid} status={goal.status_value} version={goal.version}")
    _checks(failures, "goal starts ACTIVE", goal.status == GoalStatus.ACTIVE)
    _checks(failures, "goal.created event emitted", "goal.created" in _event_kinds(storage))

    # ------------------------------------------------------------------ #
    # Phase 1: Cycle 1 - plan v1 (direct) fails on known-bad binary file
    # ------------------------------------------------------------------ #
    print("\n[Phase 1] Cycle 1: plan v1 `direct`; task fails on binary README.md")
    g1 = engine.run_goal(gid)
    print(f"  goal status after cycle 1: {g1.status_value} (run_goal returned; task failed)")
    history1 = gm.plan_history(gid)
    _checks(failures, "plan v1 recorded (initial_plan)", _plan_table(gm, gid) == [(1, "initial_plan", "direct")],
            f"history={_plan_table(gm, gid)}")
    tasks1 = gm.task_history(gid)
    _checks(failures, "one task, FAILED, persisted", len(tasks1) == 1 and tasks1[0].status == TaskStatus.FAILED)
    t1 = tasks1[0]
    _checks(failures, "failure persists on the task", "not a text file" in (t1.error or ""), t1.error or "")
    steps1 = {s.index: (s.action, s.status.value, s.params.get("path")) for s in t1.steps}
    print(f"  v1 task steps: {steps1}")
    _checks(failures, "v1 read step failed on README.md",
            steps1.get(1) == ("read", "failed", "README.md"), str(steps1))
    # the goal must NOT be inferred complete from any single task
    _checks(failures, "goal NOT completed from failed task", g1.status == GoalStatus.ACTIVE)

    # ------------------------------------------------------------------ #
    # Phase 2: Cycle 2 - memory-driven strategy change + race condition
    # ------------------------------------------------------------------ #
    print("\n[Phase 2] Cycle 2: reevaluate -> avoid_known_failures; race hits")
    # prior experience: a previous (successful) goal read docs/design.md
    prelude = engine.submit_goal("read docs/design.md", source="demo-prelude")
    prelude_task = engine.run_goal(prelude.id)
    _checks(failures, "prelude completes (prefer docs/design.md seeded)",
            prelude_task.status_value == "completed", prelude_task.status_value)

    # RACE: docs/design.md was readable during the prelude, now turns binary
    # (invalid UTF-8, like README.md - a genuine mid-goal file change)
    (sandbox / "docs" / "design.md").write_bytes(b"\xff\xfe\x00binary now")
    print("  RACE: docs/design.md turned binary after the prelude")

    g2 = engine.run_goal(gid)
    print(f"  goal status after cycle 2: {g2.status_value} (race made v2 fail too)")
    history2 = gm.plan_history(gid)
    _checks(failures, "plan v2 recorded (replan_task_failed, avoid_known_failures)",
            _plan_table(gm, gid) == [(1, "initial_plan", "direct"),
                                     (2, "replan_task_failed", "avoid_known_failures")],
            f"history={_plan_table(gm, gid)}")
    v2 = history2[1]["plan_summary"]
    read2 = next(s for s in v2 if s.get("action") == "read")
    _checks(failures, "v2 read step re-targeted to docs/design.md (resource_substitution)",
            read2.get("params", {}).get("path") == "docs/design.md" and
            any(d.get("category") == "resource_substitution" for d in read2.get("guidance", [])),
            str(read2))
    tasks2 = gm.task_history(gid)
    _checks(failures, "two tasks; v2 task FAILED on the race",
            len(tasks2) == 2 and tasks2[-1].status == TaskStatus.FAILED,
            [t.status.value for t in tasks2])
    _checks(failures, "v2 failure persisted (docs/design.md binary)",
            "not a text file" in (tasks2[-1].error or ""), tasks2[-1].error or "")
    # memory/reflection/cognition evidence drove the strategy change: the
    # planning context retrieved for cycle 2 carries bounded, provenanced
    # episodes/reflections/guidance (never raw prompts).
    from arion.memory.models import ContextBudget
    from arion.memory.retrieval import MemoryRetriever, build_planning_context
    ctx2 = build_planning_context(MemoryRetriever(memory), "inspect this repository and produce useful notes",
                                  ContextBudget())
    ctx_guidance = [(g.category, g.capability, g.resource, bool(g.episode_id)) for g in ctx2.guidance]
    print(f"  cycle-2 planning context: {len(ctx2.episodes)} episode(s), "
          f"{len(ctx2.reflections)} reflection(s), guidance={ctx_guidance}")
    _checks(failures, "memory/reflection evidence retrieved with provenance",
            len(ctx2.episodes) >= 2 and len(ctx2.reflections) >= 2 and ctx2.provenance.get("episode_ids"),
            f"provenance={ctx2.provenance}")
    _checks(failures, "avoid(README.md) + prefer(docs/design.md) guidance",
            any(g.category == "avoid" and g.resource == "README.md" for g in ctx2.guidance) and
            any(g.category == "prefer" and g.resource == "docs/design.md" for g in ctx2.guidance),
            f"guidance={ctx_guidance}")
    _checks(failures, "plan v1 immutable (unchanged snapshot)",
            history1[0]["plan_summary"] == history2[0]["plan_summary"])

    # ------------------------------------------------------------------ #
    # Phase 3: RESTART between Cycle 2 and Cycle 3 (restart-safe boundary #2)
    # ------------------------------------------------------------------ #
    print("\n[Phase 3] RESTART: fresh process on the SAME database")
    engine2, gm2, storage2, memory2, wm2 = _build_engine(db, sandbox)
    _ = world_monitor, memory  # engine1 objects go out of scope (process died)
    g_rest = gm2.get_goal(gid)
    _checks(failures, "goal state survived restart",
            g_rest is not None and g_rest.status == GoalStatus.ACTIVE,
            f"status={g_rest.status_value} version={g_rest.version}")
    _checks(failures, "plan versions [1,2] survived; NO duplicate on reload",
            _plan_table(gm2, gid) == [(1, "initial_plan", "direct"),
                                      (2, "replan_task_failed", "avoid_known_failures")],
            f"history={_plan_table(gm2, gid)}")
    _checks(failures, "task history survived (2 tasks)",
            len(gm2.task_history(gid)) == 2, str(len(gm2.task_history(gid))))
    _checks(failures, "progress metadata survived",
            isinstance(g_rest.progress_metadata, dict) and g_rest.progress_metadata.get("next_action"),
            str(g_rest.progress_metadata)[:120])
    _checks(failures, "strategy + provenance survived",
            g_rest.strategy == "avoid_known_failures", str(g_rest.strategy))
    _checks(failures, "plan summaries byte-identical across restart",
            _snapshot_plans(gm, gid) == _snapshot_plans(gm2, gid))

    # ------------------------------------------------------------------ #
    # Phase 4: Cycle 3 - world-state change; replan ONLY if materially affected
    # ------------------------------------------------------------------ #
    print("\n[Phase 4] Cycle 3: world-state change -> evaluate -> plan v3")
    # irrelevant change: system_uptime must NOT trigger a world replan
    wm2.observe("system_uptime", 3600.0, source="system")
    res_irrel, _ = gm2.evaluate(gid)
    _checks(failures, "irrelevant change filtered out (no world replan)",
            res_irrel.evidence.get("world_change_keys", []) == [] and
            res_irrel.evidence.get("reason") == "task_failed",
            f"reason={res_irrel.evidence.get('reason')} keys={res_irrel.evidence.get('world_change_keys')}")

    # material change: a new capability becomes registered (world fact)
    class GitLogCapability:
        name = "git.log"
        description = "Read git history (demo)"
        actions = [ActionSpec(name="log", description="log", required_scope="git:read",
                              resource_kind="git:repo", resource_param="repo")]

        def execute(self, action, params):
            return {"commits": []}

    engine2.registry.register(GitLogCapability())
    wm2.observe("registered_capabilities", sorted(engine2.registry.list()), source="system")
    print(f"  material change observed: registered_capabilities={sorted(engine2.registry.list())}")

    g3 = engine2.run_goal(gid)
    print(f"  goal status after cycle 3: {g3.status_value}")
    history3 = gm2.plan_history(gid)
    _checks(failures, "plan v3 recorded (replan_world_changed, defer_retry escalation)",
            _plan_table(gm2, gid) == [(1, "initial_plan", "direct"),
                                      (2, "replan_task_failed", "avoid_known_failures"),
                                      (3, "replan_world_changed", "defer_retry")],
            f"history={_plan_table(gm2, gid)}")
    _checks(failures, "goal COMPLETED only after all work handled",
            g3.status == GoalStatus.COMPLETED, g3.status_value)
    v3 = history3[2]["plan_summary"]
    read3 = next(s for s in v3 if s.get("action") == "read")
    _checks(failures, "v3 read step explicitly SKIPPED with guidance provenance",
            read3.get("status") == "skipped" and read3.get("guidance"),
            str(read3))
    _checks(failures, "all previous plans intact after v3",
            history2 == history3[:2])
    _checks(failures, "no duplicate plan version across restart+cycles",
            [p["plan_version"] for p in history3] == [1, 2, 3])
    _checks(failures, "goal.replanned event emitted for v3",
            "goal.replanned" in _event_kinds(storage2))
    _checks(failures, "world.state.changed event emitted",
            "world.state.changed" in _event_kinds(storage2))

    # completed goal is terminal: another run_goal creates NO new version
    engine2.run_goal(gid)
    _checks(failures, "terminal goal: no further plan versions",
            [p["plan_version"] for p in gm2.plan_history(gid)] == [1, 2, 3])

    # ------------------------------------------------------------------ #
    # Phase 5: authorization is re-evaluated from CURRENT live metadata
    # ------------------------------------------------------------------ #
    print("\n[Phase 5] authz re-evaluated from live metadata (no stale approvals)")
    class LockedFilesystemRead(FilesystemReadCapability):
        name = "filesystem.read"
        actions = [
            ActionSpec(
                name="read", description="read (tightened)",
                required_scope="filesystem:write",  # metadata changed AFTER old success
                risk="low", side_effects="read_only", reversible=True,
                idempotent=True, retry_safe=True,
                resource_kind="filesystem:path", resource_param="path",
                param_schema={"path": {"type": "string", "required": True}},
                default_verification={"policy": "schema_keys", "args": {"keys": ["content"]}},
            ),
            ActionSpec(
                name="list", description="list", required_scope="filesystem:write",
                risk="low", side_effects="read_only", reversible=True,
                idempotent=True, retry_safe=True,
                resource_kind="filesystem:path", resource_param="path",
                param_schema={"path": {"type": "string", "required": True}},
                default_verification={"policy": "non_empty"},
            ),
        ]

    engine2.registry.register(LockedFilesystemRead(sandbox))
    print("  filesystem.read ActionSpec tightened to required_scope=filesystem:write")
    g2b = engine2.submit_goal("read notes.txt", source="demo-authz")
    g2b_after = engine2.run_goal(g2b.id)
    denied_tasks = gm2.task_history(g2b.id)
    denied = denied_tasks and denied_tasks[-1].status == TaskStatus.FAILED and \
        any(k in (denied_tasks[-1].error or "").lower() for k in ("denied", "not permitted"))
    _checks(failures, "previously-successful read is DENIED under NEW metadata",
            g2b_after.status == GoalStatus.ACTIVE and denied,
            f"status={g2b_after.status_value} error={denied_tasks[-1].error if denied_tasks else None}")
    _checks(failures, "permission.denied event recorded",
            "permission.denied" in _event_kinds(storage2))
    print("  old successful authorization decisions are NEVER reused after "
          "resource/action metadata changes")

    # restore live metadata; the goal replans and completes (denial -> learning)
    engine2.registry.register(FilesystemReadCapability(sandbox))
    wm2.observe("registered_capabilities", sorted(engine2.registry.list()), source="system")
    g2c = engine2.run_goal(g2b.id)
    _checks(failures, "goal recovers after metadata restored (replan, not stale retry)",
            g2c.status == GoalStatus.COMPLETED, g2c.status_value)

    # ------------------------------------------------------------------ #
    # Phase 6: strategy selection demonstrates blocked_missing_capability
    # ------------------------------------------------------------------ #
    print("\n[Phase 6] strategy selection: blocked_missing_capability (explainable)")
    strat = gm2.strategy_for(
        "goal_demo_strategy", "inspect the git history with git.audit", [],
        {"registered_capabilities": {"value": sorted(engine2.registry.list())}},
        [],
    )
    _checks(failures, "missing capability -> blocked_missing_capability strategy",
            strat.name == "blocked_missing_capability",
            f"strategy={strat.name} constraints={strat.constraints}")
    print(f"  strategy provenance: {strat.provenance}")
    _checks(failures, "strategy carries provenance + constraints",
            bool(strat.constraints.get("missing_capabilities")))

    # ------------------------------------------------------------------ #
    # summary
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 78)
    print(f"goal {gid} final state:  {gm2.get_goal(gid).status_value} (version {gm2.get_goal(gid).version})")
    print("plan versions (immutable, monotonic):")
    for p in gm2.plan_history(gid):
        actions = [s.get("action") for s in p["plan_summary"]]
        print(f"  v{p['plan_version']}  reason={p['reason']:<24} strategy={p['strategy']:<22} steps={actions}")
    print(f"tasks for goal: {[(t.status.value, t.plan_version) for t in gm2.task_history(gid)]}")
    print("=" * 78)
    if failures:
        print(f"DEMO FAILED ({len(failures)} check(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED: all checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
