#!/usr/bin/env python3
"""ADR-016 DoD demo: durable goal management + replanning under races.

A real multi-cycle goal ("inspect this repository and produce useful notes")
run against a sandbox that contains a KNOWN-BAD binary resource and then
RACES the goal (a previously-good file turns binary mid-goal, and a new
capability appears mid-goal):

  Cycle 1  plan v1 (direct)          -> task fails on the binary README.md;
                                        failure persisted to memory.
  Cycle 2  reevaluate with memory/reflection/cognition evidence -> strategy
           avoid_known_failures -> plan v2 substitutes the read to the
           previously-good docs/design.md -> the RACE: docs/design.md is now
           binary -> task fails again; a second avoid entry is learned.
  Cycle 3  world-state change (new capability registered) -> reevaluate ->
           plan v3 (defer_retry) only because the change is MATERIAL
           (an unrelated system_uptime change is ignored) -> v3 skips the
           known-bad reads -> task succeeds -> goal COMPLETED. All previous
           plan versions intact.

Then: a simulated process restart proves goal state / plan versions / progress
/ provenance survive, no duplicate plan version is produced, and authorization
is re-evaluated against CURRENT live capability metadata (a tightened
ActionSpec scope is denied even though an earlier read succeeded).

Deterministic and fully offline: no LLM, no shell, no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.cognition.deriver import DeterministicBeliefDeriver
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.memory.models import ContextBudget, EpisodeFilter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, StepStatus, TaskStatus
from arion.state.store import SQLiteStorage

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def build_engine(db_path, sandbox_root):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox_root))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    cognition = CognitiveState(memory, cognitive, DeterministicBeliefDeriver())
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    strategy_selector = StrategySelector()
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=strategy_selector,
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read"},
                              boundaries={"filesystem:path": RelativePathBoundary()}),
        memory=memory, cognition=cognition, world_monitor=wm,
        strategy_selector=strategy_selector, goal_manager=gm,
        reflector=DeterministicReflector(),
    )
    return engine, gm, storage, registry, wm, memory


def context_evidence(engine, description):
    """Bounded memory/reflection/cognition evidence for a goal description."""
    from arion.memory.retrieval import MemoryRetriever, build_planning_context

    ctx = build_planning_context(MemoryRetriever(engine.memory), description, ContextBudget())
    return ctx


def plan_table(gm, gid):
    rows = []
    for p in gm.plan_history(gid):
        rows.append(
            f"    v{p['plan_version']}  {p.get('reason', ''):<20} "
            f"strategy={p.get('strategy', '-')}  steps={len(p.get('plan_summary', []))}"
        )
    return "\n".join(rows)


def main() -> int:
    work = Path(tempfile_dir())
    (work / "repo" / "docs").mkdir(parents=True, exist_ok=True)
    sb = work / "repo"
    (sb / "README.md").write_bytes(b"\xff\xfe\x00binary")  # known bad (invalid UTF-8)
    (sb / "notes.txt").write_text("hello arion\n", encoding="utf-8")
    (sb / "docs" / "design.md").write_text("# Design\n\nintended for humans\n", encoding="utf-8")
    db = work / "arion.db"

    print("=" * 78)
    print("ADR-016 demo: durable goal management & replanning under races")
    print("=" * 78)

    # ---------------------------------------------------------------- cycle 1
    print("\n[Cycle 1] plan v1 (direct); task fails on a known-bad binary resource")
    engine, gm, storage, registry, wm, memory = build_engine(db, sb)
    goal = engine.submit_goal("inspect this repository and produce useful notes")
    gid = goal.id
    check(gm.get_goal(gid).status == GoalStatus.ACTIVE, "goal starts ACTIVE")

    final = engine.run_goal(gid)
    check(final.status == GoalStatus.ACTIVE, "run_goal returns ACTIVE (task failed, failure persisted)")
    check(gm.get_goal(gid).strategy == "direct", f"cycle 1 strategy 'direct' (got {gm.get_goal(gid).strategy})")
    task1 = gm.task_history(gid)[-1]
    check(task1.status == TaskStatus.FAILED, "v1 task FAILED (binary resource)")
    check("text" in (task1.error or ""), f"failure persisted with explainable error: {task1.error!r}")
    hist = gm.plan_history(gid)
    check([p["plan_version"] for p in hist] == [1], "exactly one plan version (v1)")
    check(hist[0]["reason"] == "initial_plan" and hist[0]["strategy"] == "direct", "v1 reason=initial_plan strategy=direct")
    check(len(memory.search_episodes(EpisodeFilter())) == 1, "failure episode persisted to memory")
    ctx1 = context_evidence(engine, goal.description)
    check(any(g.category == "avoid" and g.resource == "README.md" for g in ctx1.guidance),
          "memory guidance now AVOIDS README.md")
    v1_summary = hist[0]["plan_summary"]

    # ---------------------------------------------------------------- cycle 2
    print("\n[Cycle 2] prelude + race; reevaluate; avoid_known_failures; plan v2")
    prelude = engine.submit_goal("read docs/design.md")
    engine.run_goal(prelude.id)
    check(gm.get_goal(prelude.id).status == GoalStatus.COMPLETED,
          "prelude goal completed -> prefer(docs/design.md) guidance in memory")
    ctx_pre = context_evidence(engine, goal.description)
    check(any(g.category == "prefer" and g.resource == "docs/design.md" for g in ctx_pre.guidance),
          "memory now PREFERS docs/design.md as a safe read target")

    # the race: a previously-good file turns binary mid-goal
    (sb / "docs" / "design.md").write_bytes(b"\xff\xde\xad race: now binary")

    final = engine.run_goal(gid)
    check(final.status == GoalStatus.ACTIVE, "cycle 2 run_goal returns ACTIVE (v2 task failed on the race)")
    hist = gm.plan_history(gid)
    check([p["plan_version"] for p in hist] == [1, 2], "plan versions [1, 2]")
    v2 = hist[1]
    check(v2["reason"] == "replan_task_failed", f"v2 reason=replan_task_failed (got {v2['reason']})")
    check(v2["strategy"] == "avoid_known_failures", f"v2 strategy=avoid_known_failures (got {v2['strategy']})")
    task2 = gm.task_history(gid)[-1]
    read_steps = [s for s in task2.steps if s.action == "read"]
    check(read_steps and read_steps[0].params.get("path") == "docs/design.md",
          "v2 substituted the read onto the preferred docs/design.md (guidance-driven)")
    check(any(c["category"] == "resource_substitution" for c in read_steps[0].guidance),
          "v2 read step carries resource_substitution provenance")
    check("text" in (task2.error or ""), "v2 task failed on the RACE (docs/design.md now binary)")
    check(v1_summary == hist[0]["plan_summary"], "v1 plan summary unchanged (plans are immutable)")
    check(len(memory.search_episodes(EpisodeFilter())) >= 2, "second failure episode persisted (avoid docs/design.md)")

    # ---------------------------------------------------------------- cycle 3
    print("\n[Cycle 3] world-state change -> replan ONLY if materially affected")
    wm.observe("system_uptime", {"seconds": 42.0}, source="demo")
    result_irrelevant, _ = gm.evaluate(gid)
    check(result_irrelevant.evidence.get("world_change_keys") is None
          and result_irrelevant.evidence.get("reason") == "task_failed",
          "irrelevant world change (system_uptime) is NOT material -> no replan trigger")

    registry.register(GitLogCapability(sb))  # new capability appears mid-goal
    wm.observe("registered_capabilities", sorted(registry.list()), source="demo")

    # the engine's own reevaluation (inside run_goal) sees the MATERIAL change
    # and replans: v3 reason=replan_world_changed (system_uptime above was
    # filtered out - world-change filtering demonstrated end to end)
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "cycle 3: goal COMPLETED")
    hist = gm.plan_history(gid)
    check([p["plan_version"] for p in hist] == [1, 2, 3], "plan versions [1, 2, 3] - all previous plans intact")
    v3 = hist[2]
    check(v3["reason"] == "replan_world_changed", f"v3 reason=replan_world_changed (got {v3['reason']})")
    check(v3["strategy"] == "defer_retry", f"v3 strategy=defer_retry (escalation; got {v3['strategy']})")
    task3 = gm.task_history(gid)[-1]
    read3 = [s for s in task3.steps if s.action == "read"]
    check(read3 and read3[0].status == StepStatus.SKIPPED,
          "v3 skips the known-bad read (both README.md and docs/design.md avoided)")
    check(v2["plan_summary"] == hist[1]["plan_summary"], "v2 plan summary unchanged after v3")
    check(engine.run_goal(gid).status == GoalStatus.COMPLETED,
          "re-running a COMPLETED goal stays completed (no new plan version)")
    check(len(gm.plan_history(gid)) == 3, "no duplicate plan version on re-run")
    check(gm.get_goal(gid).last_replan_reason == "replan_world_changed", "goal records last replan reason")
    check(len(memory.search_episodes(EpisodeFilter())) >= 3, "completed v3 episode persisted")

    print("\n  plan history:")
    print(plan_table(gm, gid))
    print("\n  progress:", gm.get_goal(gid).progress_metadata)

    # ------------------------------------------------------- restart + authz
    print("\n[Restart] simulate a process restart; prove durable goal state + live authz")
    engine2, gm2, storage2, registry2, wm2, memory2 = build_engine(db, sb)
    g2 = gm2.get_goal(gid)
    check(g2.status == GoalStatus.COMPLETED, "goal state survives restart (COMPLETED)")
    check(g2.version == gm.get_goal(gid).version and g2.strategy == "defer_retry",
          "goal version + strategy survive restart")
    hist2 = gm2.plan_history(gid)
    check([p["plan_version"] for p in hist2] == [1, 2, 3], "plan versions survive restart")
    check(hist2[1]["plan_summary"] == v2["plan_summary"] and hist2[2]["plan_summary"] == v3["plan_summary"],
          "plan provenance survives restart (identical summaries)")
    check(len(gm2.task_history(gid)) == len(gm.task_history(gid)), "task history survives restart")
    engine2.run_goal(gid)
    check(len(gm2.plan_history(gid)) == 3, "restart does NOT duplicate a plan version")

    # authorization is re-evaluated against CURRENT live metadata: tighten the
    # read capability's scope in the registry; an old successful decision is
    # never reused, so the new read is DENIED.
    class TightenedRead(FilesystemReadCapability):
        name = "filesystem.read"
        actions = [
            ActionSpec(
                name="read", description="read (tightened)", required_scope="filesystem:write",
                risk="low", side_effects="read_only", reversible=True, idempotent=True,
                retry_safe=True, resource_kind="filesystem:path", resource_param="path",
                param_schema={"path": {"type": "string", "required": True}},
                default_verification={"policy": "schema_keys", "args": {"keys": ["content"]}},
            )
        ]

    registry2.register(TightenedRead(sb))
    g_new = engine2.submit_goal("read notes.txt")
    final2 = engine2.run_goal(g_new.id)
    check(final2.status == GoalStatus.ACTIVE, "new read goal does NOT complete (authz re-evaluated)")
    tnew = gm2.task_history(g_new.id)[-1]
    check(tnew.status == TaskStatus.FAILED and "not permitted" in (tnew.error or ""),
          f"live ActionSpec scope change is denied: {tnew.error!r}")
    kinds = [e.kind for e in storage2.list_events()]
    check("permission.denied" in kinds, "authorization.denied audited for the live-metadata rejection")

    print("\n" + "=" * 78)
    print(f"ADR-016 demo PASSED ({CHECKS} checks) - 3 cycles, restart, live authz")
    print("=" * 78)
    return 0


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="arion-adr016-demo-")


if __name__ == "__main__":
    raise SystemExit(main())
