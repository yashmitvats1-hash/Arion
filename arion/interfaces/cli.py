"""Interface layer: CLI as the first interface (ADR: CLI first).

The interface layer only translates between human input and the orchestration
API. Voice/vision/GUI adapters later implement the same role without touching
the engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arion.bootstrap import build_engine
from arion.orchestration.engine import ArionEngine


def _print_task(engine: ArionEngine, task_id: str) -> None:
    task = engine.storage.load_task(task_id)
    if task is None:
        print(f"task {task_id} not found")
        return
    print(f"task {task.id}: {task.status.value.upper()}")
    print(f"  goal: {task.description}")
    for step in task.steps:
        mark = {"pending": "-", "running": ">", "succeeded": "ok", "failed": "x"}[step.status.value]
        if task.status.value == "awaiting_approval" and step.status.value == "pending":
            mark = "A"
        print(f"  [{mark}] step {step.index}: {step.intent} ({step.capability}/{step.action})")
        if step.error:
            print(f"        error: {step.error}")
        if task.status.value == "awaiting_approval" and step.status.value == "pending":
            print("        awaiting approval: `arion approvals list` then `arion approvals approve <approval_id>`")


def build_parser() -> argparse.ArgumentParser:
    # --db is accepted both before the subcommand and after it.
    # NOTE: the two positions use DIFFERENT dests. Sharing one dest across the
    # main parser and subparsers (via a common parent) makes argparse drop the
    # pre-subcommand value (known argparse subparser shadowing gotcha).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, dest="db", help="path to the Arion state database (default: ./arion_data/arion.db)")

    parser = argparse.ArgumentParser(prog="arion", description="Arion - autonomous personal computing system")
    parser.add_argument("--db", default=None, dest="db_global", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="submit a goal and execute it end-to-end", parents=[common])
    run.add_argument("goal", help="the goal description, e.g. 'summarize this repository'")
    run.add_argument("--source", default="cli")

    resume = sub.add_parser("resume", help="resume a persisted task after a process restart", parents=[common])
    resume.add_argument("task_id", help="task id to resume")

    status = sub.add_parser("status", help="show status of a task", parents=[common])
    status.add_argument("task_id")

    tasks = sub.add_parser("tasks", help="list persisted tasks", parents=[common])
    tasks.add_argument("--status", default=None)

    events = sub.add_parser("events", help="show the audit event trail (optionally for one task)", parents=[common])
    events.add_argument("--task", default=None)

    caps = sub.add_parser("capabilities", help="list registered capabilities", parents=[common])

    mem = sub.add_parser("memory", help="inspect the persistent cognitive memory")
    mem.add_argument("--db", default=None, dest="db_mem", help=argparse.SUPPRESS)
    mem_sub = mem.add_subparsers(dest="memory_command", required=True)

    common_memory = argparse.ArgumentParser(add_help=False)
    common_memory.add_argument("--json", action="store_true", help="machine-readable output")

    mem_eps = mem_sub.add_parser("episodes", help="list recent episodes", parents=[common, common_memory])
    mem_eps.add_argument("--limit", type=int, default=10)
    mem_eps.add_argument("--outcome", default=None)

    mem_refs = mem_sub.add_parser("reflections", help="list recent reflections", parents=[common, common_memory])
    mem_refs.add_argument("--limit", type=int, default=10)

    mem_search = mem_sub.add_parser("search", help="search episodes by relevance", parents=[common, common_memory])
    mem_search.add_argument("query", help="search text / goal")
    mem_search.add_argument("--limit", type=int, default=5)

    mem_stats = mem_sub.add_parser("stats", help="memory statistics", parents=[common, common_memory])

    mem_consol = mem_sub.add_parser("consolidate", help="run deterministic consolidation", parents=[common, common_memory])
    mem_consol.add_argument("--limit", type=int, default=200)

    cog = sub.add_parser("cognition", help="inspect the cognitive state / world model (ADR-014)")
    cog.add_argument("--db", default=None, dest="db_cog", help=argparse.SUPPRESS)
    cog_sub = cog.add_subparsers(dest="cognition_command", required=True)

    cog_beliefs = cog_sub.add_parser("beliefs", help="list derived beliefs", parents=[common, common_memory])
    cog_beliefs.add_argument("--category", default=None)

    cog_prefs = cog_sub.add_parser("preferences", help="list user preferences", parents=[common, common_memory])

    cog_env = cog_sub.add_parser("environment", help="list environment facts", parents=[common, common_memory])

    cog_snap = cog_sub.add_parser("snapshot", help="full cognitive snapshot", parents=[common, common_memory])
    cog_snap.add_argument("--refresh", action="store_true", help="re-derive beliefs from memory first")

    cog_world = cog_sub.add_parser("world", help="current world state + stale facts", parents=[common, common_memory])

    cog_goals = cog_sub.add_parser("goals", help="long-horizon goal plan history", parents=[common, common_memory])
    cog_goals.add_argument("goal_id", help="goal id to inspect")

    goals = sub.add_parser("goals", help="durable goal management (ADR-016)")
    goals.add_argument("--db", default=None, dest="db_goals", help=argparse.SUPPRESS)
    goals_sub = goals.add_subparsers(dest="goals_command", required=True)

    goals_list = goals_sub.add_parser("list", help="list goals", parents=[common, common_memory])
    goals_list.add_argument("--status", default=None)

    goals_show = goals_sub.add_parser("show", help="show a goal", parents=[common, common_memory])
    goals_show.add_argument("goal_id")

    goals_prog = goals_sub.add_parser("progress", help="goal progress", parents=[common, common_memory])
    goals_prog.add_argument("goal_id")

    goals_pause = goals_sub.add_parser("pause", help="pause a goal", parents=[common, common_memory])
    goals_pause.add_argument("goal_id")

    goals_resume = goals_sub.add_parser("resume", help="resume a goal", parents=[common, common_memory])
    goals_resume.add_argument("goal_id")

    goals_cancel = goals_sub.add_parser("cancel", help="cancel a goal", parents=[common, common_memory])
    goals_cancel.add_argument("goal_id")

    goals_approve = goals_sub.add_parser("approve", help="approve the pending approval of a goal's task", parents=[common, common_memory])
    goals_approve.add_argument("goal_id")
    goals_approve.add_argument("--actor", default="cli-approver", help="who approved (audit only; never changes authorization identity)")

    goals_deny = goals_sub.add_parser("deny", help="deny the pending approval of a goal's task", parents=[common, common_memory])
    goals_deny.add_argument("goal_id")
    goals_deny.add_argument("--actor", default="cli-approver", help="who denied (audit only)")

    approvals = sub.add_parser("approvals", help="durable approval queue (ADR-018)")
    approvals.add_argument("--db", default=None, dest="db_approvals", help=argparse.SUPPRESS)
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)

    approvals_list = approvals_sub.add_parser("list", help="list approval requests", parents=[common, common_memory])
    approvals_list.add_argument("--status", default=None, choices=["pending", "approved", "denied", "expired"])

    approvals_show = approvals_sub.add_parser("show", help="show an approval request", parents=[common, common_memory])
    approvals_show.add_argument("approval_id")

    approvals_approve = approvals_sub.add_parser("approve", help="approve a pending approval request", parents=[common, common_memory])
    approvals_approve.add_argument("approval_id")
    approvals_approve.add_argument("--actor", default="cli-approver", help="who approved (audit only; never changes authorization identity)")

    approvals_deny = approvals_sub.add_parser("deny", help="deny a pending approval request", parents=[common, common_memory])
    approvals_deny.add_argument("approval_id")
    approvals_deny.add_argument("--actor", default="cli-approver", help="who denied (audit only)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent.parent
    db_path = (args.db or args.db_global or getattr(args, "db_mem", None)
               or getattr(args, "db_cog", None) or getattr(args, "db_goals", None)
               or getattr(args, "db_approvals", None)
               or str(root / "arion_data" / "arion.db"))

    engine = build_engine(
        db_path=db_path,
        sandbox_root=str(root),
        jsonl_log=str(root / "arion_data" / "events.jsonl") if args.command in ("run", "resume", "capabilities") else None,
    )
    storage = engine.storage

    if args.command == "run":
        # Durable goal loop (ADR-016/017): submit a goal and drive the
        # long-horizon lifecycle (plan -> execute -> observe -> verify ->
        # complete / replan / block / await approval).
        goal = engine.submit_goal(args.goal, source=args.source)
        goal = engine.run_goal(goal.id)
        print("goal executed")
        print(f"goal_id: {goal.id}")
        print(f"status: {goal.status_value}")
        if goal.blockers:
            print(f"blockers: {goal.blockers}")
        tasks = [t for t in storage.list_tasks() if t.goal_id == goal.id]
        if tasks:
            latest = max(tasks, key=lambda t: t.created_at)
            print(f"task_id: {latest.id}")
            _print_task(engine, latest.id)
        if goal.status_value == "failed":
            print(f"goal error: {goal.last_replan_reason or 'failed'}")
            storage.close()
            return 1
    elif args.command == "resume":
        task = engine.run_task(args.task_id)
        print("resumed")
        print(f"task_id: {task.id}")
        print(f"status: {task.status.value}")
        _print_task(engine, task.id)
    elif args.command == "status":
        _print_task(engine, args.task_id)
    elif args.command == "tasks":
        for task in storage.list_tasks(args.status):
            print(f"{task.id}  {task.status.value:<10} {task.description[:60]}")
    elif args.command == "events":
        for event in storage.list_events(args.task):
            print(f"{event.ts}  {event.kind:<22} task={event.task_id or '-':<24} step={event.step_id or '-':<8} success={int(event.success)} {str(event.detail)[:120]}")
    elif args.command == "capabilities":
        for cap in engine.registry.capabilities_summary():
            print(f"{cap['name']}: {cap['description']}")
            for action in cap["actions"]:
                print(f"  - {action['name']} (scope: {action['required_scope']})")
    elif args.command == "memory":
        return _memory_command(args, engine)
    elif args.command == "cognition":
        return _cognition_command(args, engine)
    elif args.command == "goals":
        return _goals_command(args, engine)
    elif args.command == "approvals":
        return _approvals_command(args, engine)

    storage.close()
    return 0


def _approvals_command(args, engine) -> int:
    """arion approvals list|show|approve|deny (durable queue, ADR-018)."""
    import json

    store = getattr(engine, "approval_store", None)
    if store is None:
        print("approval queue is not available on this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.approvals_command == "list":
        reqs = store.list_requests(status=args.status)
        if args.json:
            _emit([r.to_dict() for r in reqs])
            return 0
        for r in reqs:
            line = (f"{r.approval_id}  {r.status.value:<9} {r.capability}/{r.action}  "
                    f"{('on ' + str(r.resource)) if r.resource else ''}  task={r.task_id}"
                    + (f"  goal={r.goal_id}" if r.goal_id else ""))
            if r.status.value == "expired" and r.expired_at:
                line += f"  expired_at={r.expired_at}"
            print(line)
        return 0

    if args.approvals_command == "show":
        req = store.get_request(args.approval_id)
        if req is None:
            print(f"approval {args.approval_id} not found")
            return 1
        if args.json:
            _emit(req.to_dict())
            return 0
        print(f"approval {req.approval_id}: {req.status.value}")
        print(f"  {req.capability}/{req.action} {('on ' + str(req.resource)) if req.resource else ''}")
        print(f"  scope={req.scope} risk={req.risk} side_effects={req.side_effects}")
        print(f"  resource_kind={req.resource_kind or '-'}")
        print(f"  task={req.task_id} step={req.step_index} goal={req.goal_id or '-'}")
        print(f"  summary: {req.summary}")
        print(f"  requester={req.requester_actor} chain={req.actor_chain}")
        print(f"  created_at={req.created_at}")
        if req.decision_actor:
            print(f"  decided_by={req.decision_actor} at={req.decided_at}")
        return 0

    if args.approvals_command in ("approve", "deny"):
        from arion.orchestration.authz import ApprovalOutcome

        outcome = ApprovalOutcome.APPROVED if args.approvals_command == "approve" else ApprovalOutcome.DENIED
        try:
            resolved = engine.resolve_approval_request(args.approval_id, outcome, actor=args.actor)
        except Exception as exc:
            print(f"approval resolution rejected: {exc}")
            return 1
        if args.json:
            _emit(resolved.to_dict())
            return 0
        print(f"approval {resolved.approval_id}: {resolved.status.value} "
              f"({resolved.capability}/{resolved.action}) by {resolved.decision_actor}")
        return 0

    print(f"unknown approvals command: {args.approvals_command}")
    return 1


def _goals_command(args, engine) -> int:
    """arion goals list|show|progress|pause|resume|cancel"""
    import json

    gm = getattr(engine, "goal_manager", None)
    if gm is None:
        print("goal manager is disabled for this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.goals_command == "list":
        goals = gm.list_goals(status=args.status)
        if args.json:
            _emit([g.to_dict() for g in goals])
            return 0
        for g in goals:
            print(f"{g.id}  {g.status_value:<10} v{g.version} strategy={g.strategy or '-'}  {g.description[:50]!r}")
        return 0

    if args.goals_command == "show":
        summary = gm.summarize(args.goal_id)
        if not summary.get("exists"):
            print(f"goal {args.goal_id} not found")
            return 1
        if args.json:
            _emit(summary)
            return 0
        print(f"goal {args.goal_id}: status={summary['status']} v{summary['goal_version']}")
        print(f"  description: {summary.get('description', '')}")
        print(f"  strategy: {summary['strategy'] or '-'}")
        print(f"  plan versions: {summary['plan_versions']} (latest v{summary['latest_plan_version']} {summary['latest_strategy']})")
        print(f"  blockers: {len(summary['blockers'])}")
        for b in summary["blockers"]:
            print(f"    - {b.get('type', b.get('key'))}: {b.get('detail', b.get('reason', ''))}"
                  + (f" (task {b.get('task_id')} step {b.get('step_index')})" if b.get("task_id") else ""))
        print(f"  progress: {summary['progress']}")
        print(f"  tasks: {summary['tasks']}")
        return 0

    if args.goals_command == "progress":
        result, goal = gm.evaluate(args.goal_id)
        if args.json:
            _emit({"evaluation": result.to_dict(), "goal": goal.to_dict()})
            return 0
        print(f"goal {args.goal_id}: progress={result.progress:.2f} status={result.status} next_action={result.next_action}")
        print(f"  evidence: {result.evidence}")
        if result.blockers:
            print(f"  blockers: {result.blockers}")
        return 0

    from arion.state.models import GoalStateError

    def _transition(goal_id, fn, verb):
        try:
            goal = fn(goal_id)
        except GoalStateError as exc:
            print(f"goal {goal_id} transition rejected (fail closed): {exc}")
            return 1
        _emit(f"goal {goal_id} {verb} (v{goal.version})" if not args.json else goal.to_dict())
        return 0

    if args.goals_command == "pause":
        return _transition(args.goal_id, lambda g: gm.pause(g, reason="cli_pause"), "paused")
    if args.goals_command == "resume":
        return _transition(args.goal_id, lambda g: gm.resume(g, reason="cli_resume"), "resumed")
    if args.goals_command == "cancel":
        return _transition(args.goal_id, lambda g: gm.cancel(g, reason="cli_cancel"), "cancelled")

    if args.goals_command in ("approve", "deny"):
        from arion.orchestration.authz import ApprovalOutcome

        awaiting = [t for t in gm.task_history(args.goal_id)
                    if t.status.value == "awaiting_approval"]
        if not awaiting:
            print(f"goal {args.goal_id} has no approval-pending task")
            return 1
        task = awaiting[0]
        outcome = ApprovalOutcome.APPROVED if args.goals_command == "approve" else ApprovalOutcome.DENIED
        try:
            resolved = engine.resolve_approval(task.id, outcome, actor=args.actor)
        except Exception as exc:
            print(f"approval resolution rejected: {exc}")
            return 1
        if args.json:
            _emit(resolved.to_dict())
            return 0
        print(f"goal {args.goal_id}: approval {outcome.value} for task {task.id} step {task.current_step}")
        print(f"task {resolved.id}: {resolved.status.value.upper()}"
              + (f" ({resolved.error})" if resolved.error else ""))
        return 0

    print(f"unknown goals command: {args.goals_command}")
    return 1


def _cognition_command(args, engine) -> int:
    """arion cognition beliefs|preferences|environment|snapshot"""
    import json

    cognition = getattr(engine, "cognition", None)
    if cognition is None:
        print("cognitive state is disabled for this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.cognition_command == "beliefs":
        beliefs = cognition.cognition.list_beliefs(category=args.category, limit=200)
        if args.json:
            _emit([b.to_dict() for b in beliefs])
            return 0
        for b in beliefs:
            print(f"{b.belief_id}  [{b.category}] conf={b.confidence:.2f} src={b.source}  {b.statement[:100]}")
        return 0

    if args.cognition_command == "preferences":
        prefs = cognition.cognition.list_preferences(limit=200)
        if args.json:
            _emit([p.to_dict() for p in prefs])
            return 0
        for p in prefs:
            print(f"{p.preference_id}  {p.key}={p.value}  user={p.user} src={p.source}")
        return 0

    if args.cognition_command == "environment":
        facts = cognition.cognition.list_environment_facts(limit=200)
        if args.json:
            _emit([f.to_dict() for f in facts])
            return 0
        for f in facts:
            print(f"{f.key} = {json.dumps(f.value, default=str)[:120]}  src={f.source}")
        return 0

    if args.cognition_command == "snapshot":
        if getattr(args, "refresh", False):
            count = cognition.refresh_from_memory(limit=50)
            print(f"re-derived {count} new belief(s) from memory")
        snap = cognition.snapshot(limit_beliefs=50)
        _emit(
            f"beliefs: {snap.counts['beliefs']} | preferences: {snap.counts['preferences']} | "
            f"environment: {snap.counts['environment']}"
            if not args.json else snap.to_dict(limit_beliefs=50)
        )
        if args.json:
            return 0
        for b in snap.beliefs[:10]:
            print(f"  belief [{b.category}] conf={b.confidence:.2f}: {b.statement[:90]}")
        return 0

    if args.cognition_command == "world":
        monitor = getattr(engine, "world_monitor", None)
        if monitor is None:
            print("world monitor is disabled for this engine")
            return 1
        facts = monitor.current_state()
        stale = monitor.stale_facts(max_age_days=7.0)
        if args.json:
            _emit({
                "state": facts,
                "stale_facts": [f.to_dict() for f in stale],
            })
            return 0
        for key, info in sorted(facts.items()):
            print(f"{key} (v{info['version']}, observed {info['observed_at']}) = {json.dumps(info['value'], default=str)[:100]}")
        if stale:
            print(f"STALE facts ({len(stale)}):")
            for f in stale:
                print(f"  {f.key} last observed {f.observed_at}")
        else:
            print("no stale facts")
        return 0

    if args.cognition_command == "goals":
        gm = getattr(engine, "goal_manager", None)
        if gm is None:
            print("goal manager is disabled for this engine")
            return 1
        history = gm.plan_history(args.goal_id)
        if args.json:
            _emit(gm.summarize(args.goal_id))
            return 0
        print(f"goal {args.goal_id}: {len(history)} plan version(s)")
        for h in history:
            print(f"  v{h['plan_version']} strategy={h['strategy']} steps={len(json.loads(h['plan_summary']))} at {h['created_at']}")
        print(f"progress: {gm.progress(args.goal_id)}")
        return 0

    print(f"unknown cognition command: {args.cognition_command}")
    return 1


def _memory_command(args, engine) -> int:
    """arion memory episodes|reflections|search|stats|consolidate"""
    import json

    memory = getattr(engine, "memory", None)
    if memory is None:
        print("memory is disabled for this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.memory_command == "episodes":
        from arion.memory.models import EpisodeFilter

        episodes = memory.search_episodes(
            EpisodeFilter(outcome=args.outcome, limit=args.limit)
            if args.outcome else EpisodeFilter(limit=args.limit)
        )
        if args.json:
            _emit([e.to_dict() for e in episodes])
            return 0
        for ep in episodes:
            _emit(
                f"{ep.episode_id}  {ep.outcome:<10} importance={ep.importance:.2f}  {ep.goal[:60]!r}"
                f"  tags={ep.tags[:4]}"
            )
        return 0

    if args.memory_command == "reflections":
        reflections = memory.list_recent_reflections(limit=args.limit)
        if args.json:
            _emit([r.to_dict() for r in reflections])
            return 0
        for ref in reflections:
            _emit(
                f"{ref.reflection_id}  conf={ref.confidence}  episode={ref.episode_id}  lesson={ref.lesson[:80]!r}"
            )
        return 0

    if args.memory_command == "search":
        from arion.memory.retrieval import MemoryRetriever

        results = MemoryRetriever(memory).retrieve(args.query, top_k=args.limit)
        if args.json:
            _emit([e.to_dict() for e in results])
            return 0
        for ep in results:
            _emit(
                f"{ep.episode_id}  {ep.outcome:<10} importance={ep.importance:.2f}  {ep.goal[:60]!r}"
            )
        return 0

    if args.memory_command == "stats":
        episodes = memory.list_recent(limit=1000)
        reflections = memory.list_recent_reflections(limit=1000)
        from collections import Counter

        outcomes = Counter(e.outcome for e in episodes)
        consolidations = memory.list_consolidations(limit=1000)
        stats = {
            "episodes": len(episodes),
            "reflections": len(reflections),
            "consolidations": len(consolidations),
            "by_outcome": dict(outcomes),
        }
        _emit(
            f"episodes: {stats['episodes']} | reflections: {stats['reflections']} | "
            f"consolidations: {stats['consolidations']} | by_outcome: {stats['by_outcome']}"
            if not args.json else stats
        )
        return 0

    if args.memory_command == "consolidate":
        from arion.memory.consolidation import MemoryConsolidator

        records = MemoryConsolidator(memory).consolidate(limit=args.limit)
        if args.json:
            _emit([r.to_dict() for r in records])
            return 0
        for record in records:
            print(f"consolidated {record.count} episodes -> {record.consolidation_id} "
                  f"(category={record.category}, sources={record.source_episode_ids})")
            print(f"  lesson: {record.merged_lesson[:200]}")
        print(f"{len(records)} consolidation record(s) created")
        return 0

    print(f"unknown memory command: {args.memory_command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
