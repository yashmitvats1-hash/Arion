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

    recovery = sub.add_parser("recovery", help="mutation recovery registry (ADR-020)")
    recovery.add_argument("--db", default=None, dest="db_recovery", help=argparse.SUPPRESS)
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)

    recovery_list = recovery_sub.add_parser("list", help="list mutation recovery records", parents=[common, common_memory])
    recovery_list.add_argument("--status", default=None, choices=["required", "acknowledged"])

    recovery_show = recovery_sub.add_parser("show", help="show a mutation recovery record", parents=[common, common_memory])
    recovery_show.add_argument("recovery_id")

    recovery_ack = recovery_sub.add_parser("acknowledge", help="acknowledge a REQUIRED mutation recovery (ADR-020)", parents=[common, common_memory])
    recovery_ack.add_argument("recovery_id")
    recovery_ack.add_argument("--actor", default="cli-operator", help="who acknowledged (audit only; never authorizes)")

    locks = sub.add_parser("locks", help="advisory mutation locks (ADR-021)")
    locks.add_argument("--db", default=None, dest="db_locks", help=argparse.SUPPRESS)
    locks_sub = locks.add_subparsers(dest="locks_command", required=True)

    locks_list = locks_sub.add_parser("list", help="list mutation locks", parents=[common, common_memory])
    locks_waiters = locks_sub.add_parser("waiters", help="list tasks waiting (bounded) on mutation locks (ADR-022/023)", parents=[common, common_memory])
    locks_queue = locks_sub.add_parser("queue", help="show the durable FIFO wait queue for a resource (ADR-023)", parents=[common, common_memory])
    locks_queue.add_argument("resource")
    locks_queue.add_argument("--kind", default="filesystem:path", help="resource kind (default filesystem:path)")
    locks_show = locks_sub.add_parser("show", help="show a mutation lock or waiter", parents=[common, common_memory])
    locks_show.add_argument("id")

    locks_reclaim = locks_sub.add_parser("reclaim", help="reclaim an EXPIRED mutation lock (ADR-021; never authorizes)", parents=[common, common_memory])
    locks_reclaim.add_argument("id")

    sched = sub.add_parser("scheduler", help="durable scheduler/work registry (ADR-025)")
    sched.add_argument("--db", default=None, dest="db_scheduler", help=argparse.SUPPRESS)
    sched_sub = sched.add_subparsers(dest="scheduler_command", required=True)

    sched_status = sched_sub.add_parser("status", help="scheduler status: capacity + work counts by state (bounded, metadata-only)", parents=[common, common_memory])
    sched_workers = sched_sub.add_parser("workers", help="list RUNNING work + worker leases (ADR-025)", parents=[common, common_memory])
    sched_queue = sched_sub.add_parser("queue", help="list QUEUED work in admission order (ADR-025)", parents=[common, common_memory])
    sched_show = sched_sub.add_parser("show", help="show one scheduler work row (bounded metadata; fails closed on unknown id)", parents=[common, common_memory])
    sched_show.add_argument("work_id")
    sched_reclaim = sched_sub.add_parser("reclaim", help="reclaim a STALE RUNNING work row whose lease expired (ADR-025; never executes)", parents=[common, common_memory])
    sched_reclaim.add_argument("work_id")

    sched_weights = sched_sub.add_parser("weights", help="list durable per-goal scheduling weights (ADR-027)", parents=[common, common_memory])
    sched_weight = sched_sub.add_parser("weight", help="manage a goal's durable scheduling weight (ADR-027)", parents=[common, common_memory])
    sched_weight_sub = sched_weight.add_subparsers(dest="scheduler_weight_command", required=True)
    sched_weight_set = sched_weight_sub.add_parser("set", help="set a goal's weight (>=1, bounded; scheduler POLICY, never authorization)", parents=[common, common_memory])
    sched_weight_set.add_argument("goal_id")
    sched_weight_set.add_argument("weight", type=int)
    sched_weight_set.add_argument("--disable", action="store_true", help="set the config disabled (goal never admitted)")
    sched_weight_set.add_argument("--by", default="cli-operator", help="who configured (audit only)")
    sched_weight_rm = sched_weight_sub.add_parser("remove", help="remove a goal's weight config (back to default weight 1)", parents=[common, common_memory])
    sched_weight_rm.add_argument("goal_id")
    sched_weight_en = sched_weight_sub.add_parser("enable", help="enable a goal's weight config", parents=[common, common_memory])
    sched_weight_en.add_argument("goal_id")
    sched_weight_dis = sched_weight_sub.add_parser("disable", help="disable a goal's weight config (never admitted)", parents=[common, common_memory])
    sched_weight_dis.add_argument("goal_id")

    sched_reservations = sched_sub.add_parser("reservations", help="list durable per-goal capacity reservations (ADR-029)", parents=[common, common_memory])
    sched_reservation = sched_sub.add_parser("reservation", help="manage a goal's durable capacity reservation (ADR-029)", parents=[common, common_memory])
    sched_reservation_sub = sched_reservation.add_subparsers(dest="scheduler_reservation_command", required=True)
    sched_reservation_set = sched_reservation_sub.add_parser("set", help="set a goal's reservation (>=0, bounded; scheduler POLICY, never authorization)", parents=[common, common_memory])
    sched_reservation_set.add_argument("goal_id")
    sched_reservation_set.add_argument("capacity", type=int)
    sched_reservation_set.add_argument("--disable", action="store_true", help="set the config disabled (no floor)")
    sched_reservation_set.add_argument("--by", default="cli-operator", help="who configured (audit only)")
    sched_reservation_rm = sched_reservation_sub.add_parser("remove", help="remove a goal's reservation config (back to 0)", parents=[common, common_memory])
    sched_reservation_rm.add_argument("goal_id")
    sched_reservation_en = sched_reservation_sub.add_parser("enable", help="enable a goal's reservation config", parents=[common, common_memory])
    sched_reservation_en.add_argument("goal_id")
    sched_reservation_dis = sched_reservation_sub.add_parser("disable", help="disable a goal's reservation config (no floor)", parents=[common, common_memory])
    sched_reservation_dis.add_argument("goal_id")

    sched_watch = sched_sub.add_parser("watch", help="show scheduler telemetry events (ADR-028; observational only)", parents=[common, common_memory])
    sched_watch.add_argument("--goal", default=None, help="filter by goal id")
    sched_watch.add_argument("--scheduler", default=None, help="filter by scheduler id")
    sched_watch.add_argument("--work", default=None, help="filter by work id")
    sched_watch.add_argument("--type", default=None, help="filter by event type")
    sched_watch.add_argument("--since", default=None, help="only events at/after this ISO timestamp")
    sched_watch.add_argument("--limit", type=int, default=50, help="max events to show (bounded; default 50)")
    sched_watch.add_argument("--follow", action="store_true", help="bounded polling mode (Ctrl-C to stop); read-only, no registration/heartbeat")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent.parent
    db_path = (args.db or args.db_global or getattr(args, "db_mem", None)
               or getattr(args, "db_cog", None) or getattr(args, "db_goals", None)
               or getattr(args, "db_approvals", None) or getattr(args, "db_recovery", None)
               or getattr(args, "db_locks", None) or getattr(args, "db_scheduler", None)
               or str(root / "arion_data" / "arion.db"))

    engine = build_engine(
        db_path=db_path,
        sandbox_root=str(root),
        jsonl_log=str(root / "arion_data" / "events.jsonl") if args.command in ("run", "resume", "capabilities") else None,
        # The `scheduler` command is a PASSIVE observer of the durable
        # registry: it must not abandon another (possibly live) scheduler's
        # QUEUED rows. Every other command performs the normal restart
        # reclamation (stale leases + dead schedulers' queues).
        scheduler_reclaim_on_start=args.command != "scheduler",
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
            engine.shutdown()  # ADR-024: join bounded workers, no orphans
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
    elif args.command == "recovery":
        return _recovery_command(args, engine)
    elif args.command == "locks":
        return _locks_command(args, engine)
    elif args.command == "scheduler":
        return _scheduler_command(args, engine)

    engine.shutdown()  # ADR-024/025: join bounded workers, no orphans
    storage.close()
    return 0


def _scheduler_command(args, engine) -> int:
    """arion scheduler status|workers|queue|show|reclaim (ADR-025).

    Reads the DURABLE scheduler/work registry through the domain store only -
    never raw SQLite. Output is bounded, metadata-only, secret-free and
    restart-safe (it reflects the durable rows, not live engine memory).
    Unknown work ids fail closed (non-zero exit). Reclaim only moves a STALE
    RUNNING row (lease expired) to ABANDONED; it never executes anything and
    never authorizes a mutation - abandoned work re-runs the full fresh
    authorization/recovery path on the next engine run.
    """
    import json

    store = getattr(engine, "scheduler_registry", None)
    if store is None or not hasattr(store, "list_work"):
        print("scheduler registry is not available on this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.scheduler_command == "status":
        from arion.state.scheduler_work import SchedulerWorkStatus

        rows = store.list_work()
        counts = {s.value: 0 for s in SchedulerWorkStatus}
        for r in rows:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        stale = [r for r in rows if r.status == SchedulerWorkStatus.RUNNING
                 and r.lease_expires_at is not None and r.lease_expires_at <= engine._lock_now()]
        out = {
            "total": len(rows),
            "queued": counts["queued"],
            "running": counts["running"],
            "completed": counts["completed"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "abandoned": counts["abandoned"],
            "stale_running_leases": len(stale),
        }
        if args.json:
            _emit(out)
        else:
            for k, v in out.items():
                print(f"{k:<22} {v}")
        return 0

    if args.scheduler_command == "workers":
        from arion.state.scheduler_work import SchedulerWorkStatus

        workers = [r for r in store.list_work(status=SchedulerWorkStatus.RUNNING)]
        if args.json:
            _emit([w.to_dict() for w in workers])
            return 0
        for w in workers:
            print(f"{w.work_id}  running  worker={w.worker_id}  "
                  f"task={w.task_id}  goal={w.goal_id or '-'}  step={w.step_index}  "
                  f"lease_expires={w.lease_expires_at}")
        return 0

    if args.scheduler_command == "queue":
        from arion.state.scheduler_work import SchedulerWorkStatus

        queued = store.list_work(status=SchedulerWorkStatus.QUEUED)
        if args.json:
            _emit([q.to_dict() for q in queued])
            return 0
        for pos, q in enumerate(queued, start=1):
            print(f"#{pos:<4} {q.work_id}  queued  task={q.task_id}  "
                  f"goal={q.goal_id or '-'}  step={q.step_index}  created={q.created_at}")
        return 0

    if args.scheduler_command == "show":
        work = store.get_work(args.work_id)
        if work is None:
            print(f"unknown scheduler work id: {args.work_id} (fail closed)")
            return 1
        _emit(work.to_dict())
        return 0

    if args.scheduler_command == "reclaim":
        from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus

        work = store.get_work(args.work_id)
        if work is None:
            print(f"unknown scheduler work id: {args.work_id} (fail closed)")
            return 1
        if work.status != SchedulerWorkStatus.RUNNING:
            print(f"work {args.work_id} is {work.status.value} (only RUNNING rows can be reclaimed)")
            return 1
        if work.lease_expires_at is None or work.lease_expires_at > engine._lock_now():
            print(f"work {args.work_id} lease is still valid (expires {work.lease_expires_at}); "
                  f"not reclaimed")
            return 1
        try:
            store.mark_terminal(args.work_id, SchedulerWorkStatus.ABANDONED,
                                now=engine._lock_now())
        except SchedulerStateError as exc:
            print(f"reclaim failed: {exc}")
            return 1
        print(f"reclaimed {args.work_id} -> abandoned (lease had expired)")
        return 0

    if args.scheduler_command == "weights":
        from arion.state.scheduler_work import SchedulerRegistryError

        rows = store.list_goal_weights() if hasattr(store, "list_goal_weights") else []
        if args.json:
            _emit(rows)
            return 0
        if not rows:
            print("(no goal weights configured - all goals use the default weight 1)")
            return 0
        for r in rows:
            state = "enabled" if r["enabled"] else "disabled"
            print(f"{r['goal_id']:<24} weight={r['weight']:<5} {state:<9} "
                  f"by={r['updated_by']}  at={r['updated_at']}")
        return 0

    if args.scheduler_command == "weight":
        from arion.state.scheduler_work import SchedulerRegistryError

        if args.scheduler_weight_command == "set":
            try:
                store.set_goal_weight(args.goal_id, args.weight,
                                      enabled=not args.disable,
                                      by=args.by, now=engine._lock_now())
            except SchedulerRegistryError as exc:
                print(f"invalid weight config: {exc}")
                return 1
            cfg = store.get_goal_weight_config(args.goal_id)
            if args.json:
                _emit(cfg)
            else:
                print(f"{args.goal_id}: weight={cfg['weight']} "
                      f"{'disabled' if not cfg['enabled'] else 'enabled'} "
                      f"(by {cfg['updated_by']})")
            return 0
        if args.scheduler_weight_command == "remove":
            removed = store.remove_goal_weight(args.goal_id)
            if not removed:
                print(f"{args.goal_id}: no weight config (already default 1)")
                return 1
            print(f"{args.goal_id}: weight config removed (default 1 restored)")
            return 0
        if args.scheduler_weight_command in ("enable", "disable"):
            enabled = args.scheduler_weight_command == "enable"
            cfg = store.set_goal_weight_enabled(args.goal_id, enabled)
            if cfg is None:
                print(f"{args.goal_id}: no weight config to "
                      f"{'enable' if enabled else 'disable'}")
                return 1
            print(f"{args.goal_id}: "
                  f"{'enabled' if enabled else 'disabled'} "
                  f"(weight={cfg['weight']})")
            return 0
        return 1

    if args.scheduler_command == "reservations":
        rows = (store.list_goal_reservations()
                if hasattr(store, "list_goal_reservations") else [])
        if args.json:
            _emit(rows)
            return 0
        if not rows:
            print("(no goal reservations configured - all goals have floor 0)")
            return 0
        total = sum(int(r["reservation"]) for r in rows if r["enabled"])
        for r in rows:
            state = "enabled" if r["enabled"] else "disabled"
            print(f"{r['goal_id']:<24} reservation={r['reservation']:<5} "
                  f"{state:<9} by {r['updated_by']} @ {r['updated_at']}")
        print(f"(reserved_capacity={total})")
        return 0

    if args.scheduler_command == "reservation":
        from arion.state.scheduler_work import SchedulerRegistryError

        if args.scheduler_reservation_command == "set":
            try:
                store.set_goal_reservation(args.goal_id, args.capacity,
                                           enabled=not args.disable,
                                           by=args.by, now=engine._lock_now())
            except SchedulerRegistryError as exc:
                print(f"invalid reservation config: {exc}")
                return 1
            cfg = store.get_goal_reservation_config(args.goal_id)
            if args.json:
                _emit(cfg)
            else:
                print(f"{args.goal_id}: reservation={cfg['reservation']} "
                      f"{'disabled' if not cfg['enabled'] else 'enabled'} "
                      f"(by {cfg['updated_by']})")
            return 0
        if args.scheduler_reservation_command == "remove":
            removed = store.remove_goal_reservation(args.goal_id)
            if not removed:
                print(f"{args.goal_id}: no reservation config (floor 0)")
                return 1
            print(f"{args.goal_id}: reservation removed (floor 0 restored)")
            return 0
        if args.scheduler_reservation_command in ("enable", "disable"):
            enabled = args.scheduler_reservation_command == "enable"
            cfg = store.set_goal_reservation_enabled(args.goal_id, enabled)
            if cfg is None:
                print(f"{args.goal_id}: no reservation config to "
                      f"{'enable' if enabled else 'disable'}")
                return 1
            print(f"{args.goal_id}: "
                  f"{'enabled' if enabled else 'disabled'} "
                  f"(reservation={cfg['reservation']})")
            return 0
        return 1

    if args.scheduler_command == "watch":
        if not hasattr(store, "scheduler_events"):
            print("scheduler telemetry is not available on this engine")
            return 1
        if args.limit < 1 or args.limit > 1000:
            print("watch --limit must be in [1, 1000] (bounded; fail closed)")
            return 1
        if args.follow:
            return _scheduler_watch_follow(args, store, engine)

        def _human(e):
            d = e.detail
            who = (d.get("worker_id") or d.get("scheduler_id") or "-")
            goal = d.get("goal_id") or "-"
            work = d.get("work_id") or "-"
            extra = ""
            if e.kind in ("work.claimed", "work.heartbeat", "work.reclaimed"):
                extra = f" lease={d.get('lease_expires_at', '-')}"
            if e.kind in ("work.claim_denied", "capacity.denied",
                          "scheduler_share.denied", "goal_weight.denied",
                          "reservation.denied"):
                extra = f" reason={d.get('reason', '-')}"
                if e.kind == "reservation.denied":
                    extra += (f" pressure={d.get('pressure', '-')} "
                              f"reserved={d.get('reserved_capacity', '-')}")
            if e.kind == "goal_weight.refill":
                extra = (f" weight={d.get('weight')} "
                         f"credit={d.get('credit_before')}->{d.get('credit_after')}")
            if e.kind == "scheduler.config_changed":
                extra = f" config={d.get('config')} ({d.get('reason', '-')})"
            if e.kind == "reservation.satisfied":
                extra = (f" reservation={d.get('reservation')} "
                         f"running={d.get('running')}")
            if e.kind == "goal_reservation_changed":
                extra = f" config={d.get('config')} ({d.get('outcome', '-')} {d.get('reason', '')})"
            print(f"{e.ts}  {e.kind:<24} who={who:<20} goal={goal:<12} "
                  f"work={work:<12}{extra}")

        events = store.scheduler_events(
            scheduler_id=args.scheduler, goal_id=args.goal, work_id=args.work,
            event_type=args.type, since=args.since, limit=args.limit)
        if args.json:
            _emit([{"id": e.id, "ts": e.ts, "kind": e.kind,
                    "detail": e.detail, "success": e.success}
                   for e in events])
        else:
            for e in events:
                _human(e)
        return 0

    return 1


def _scheduler_watch_follow(args, store, engine) -> int:
    """Bounded polling watch mode (ADR-028 Phase F): prints NEW events each
    poll. READ-ONLY: no mutation, no registration, no heartbeat, no claims.
    Ctrl-C (KeyboardInterrupt) exits cleanly; the in-memory cursor is the
    only state, so memory growth is bounded."""
    import time as _time

    if not hasattr(store, "oldest_scheduler_event"):
        print("scheduler telemetry is not available on this engine")
        return 1
    # start from the newest event so the first poll prints only new ones
    recent = store.recent_scheduler_events(limit=1)
    last_id = recent[0].id if recent else None
    interval = max(0.5, float(getattr(args, "follow_interval", 2.0)))
    print("watching scheduler events (Ctrl-C to stop)...", flush=True)
    try:
        while True:
            rows = store.scheduler_events(
                scheduler_id=args.scheduler, goal_id=args.goal,
                work_id=args.work, event_type=args.type, since=args.since,
                limit=args.limit)
            for e in rows:
                if last_id is None or e.id != last_id:
                    print(f"{e.ts}  {e.kind:<24} "
                          f"who={(e.detail.get('worker_id') or e.detail.get('scheduler_id') or '-'):<20} "
                          f"goal={e.detail.get('goal_id', '-'):<12} "
                          f"work={e.detail.get('work_id', '-'):<12}",
                          flush=True)
            if rows:
                last_id = rows[-1].id
            _time.sleep(interval)
    except KeyboardInterrupt:
        print("\nwatch stopped (no mutation performed)", flush=True)
        return 0


def _locks_command(args, engine) -> int:
    """arion locks list|waiters|show|reclaim (ADR-021/022).

    Uses the domain lock store / engine interfaces only - never raw SQLite.
    Output is bounded and secret-free (lock/resource/owner identifiers and
    timestamps). Reclaim only removes EXPIRED coordination records; it never
    authorizes a mutation. Waiters expose bounded ADR-022 wait metadata
    (task/goal/step, resource, attempts, deadline, next retry).
    """
    import json

    store = getattr(engine, "mutation_lock_store", None)
    if store is None:
        print("mutation lock store is not available on this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    def _waiters(status="queued"):
        """Bounded ADR-023 waiters: the durable FIFO queue rows (status
        queued) merged with the task's own persisted wait metadata."""
        waiters = store.list_waiters(status=status) if hasattr(store, "list_waiters") else []
        out = []
        for w in waiters:
            task = engine.storage.load_task(w.task_id) if engine.storage else None
            lw = (task.lock_wait or {}) if task else {}
            out.append({
                "status": "waiting",
                "task_id": w.task_id,
                "goal_id": w.goal_id,
                "step_index": w.step_index,
                "resource_kind": w.resource_kind,
                "resource": w.resource,
                "waiter_id": w.waiter_id,
                "position": w.seq,
                "attempts": lw.get("attempts", w.attempts),
                "deadline": lw.get("deadline", w.deadline),
                "next_retry": lw.get("next_retry", w.next_retry),
            })
        return out

    if args.locks_command == "list":
        locks = store.list()
        if args.json:
            _emit([l.to_dict() for l in locks])
            return 0
        for l in locks:
            print(f"{l.lock_id}  held  {l.resource_kind}/{l.resource}  "
                  f"{l.capability}/{l.action}  owner={l.owner_id}  expires={l.expires_at}")
        return 0

    if args.locks_command == "waiters":
        waiters = _waiters()
        if args.json:
            _emit(waiters)
            return 0
        if not waiters:
            print("no tasks waiting on mutation locks")
            return 0
        for w in waiters:
            print(f"task={w['task_id']}  waiting  pos={w['position']}  {w['resource_kind']}/{w['resource']}  "
                  f"waiter={w['waiter_id']}  attempts={w['attempts']}  deadline={w['deadline']}  "
                  f"next_retry={w['next_retry']}" + (f"  goal={w['goal_id']}" if w["goal_id"] else ""))
        return 0

    if args.locks_command == "queue":
        # safe inspection of the durable FIFO queue for one resource (all
        # statuses, oldest first); never grants or transfers anything.
        rows = store.list_waiters(resource_kind=args.kind, resource=args.resource) \
            if hasattr(store, "list_waiters") else []
        if args.json:
            _emit([r.to_dict() for r in rows])
            return 0
        if not rows:
            print(f"no waiters for {args.kind} {args.resource}")
            return 0
        for r in rows:
            print(f"pos={r.seq}  {r.status.value:<9} task={r.task_id}  waiter={r.waiter_id}  "
                  f"deadline={r.deadline}  attempts={r.attempts}"
                  + (f"  goal={r.goal_id}" if r.goal_id else ""))
        return 0

    if args.locks_command == "show":
        # show <id>: a lock id, a waiter id, or a task id with wait metadata
        lock = store.get(args.id)
        if lock is not None:
            if args.json:
                _emit(lock.to_dict())
                return 0
            print(f"lock {lock.lock_id}: held")
            print(f"  {lock.capability}/{lock.action} on {lock.resource_kind} {lock.resource}")
            print(f"  owner={lock.owner_id}")
            print(f"  acquired_at={lock.acquired_at}  expires_at={lock.expires_at}")
            return 0
        waiter = store.get_waiter(args.id) if hasattr(store, "get_waiter") else None
        if waiter is not None:
            if args.json:
                _emit(waiter.to_dict())
                return 0
            print(f"waiter {waiter.waiter_id}: {waiter.status.value} (pos {waiter.seq})")
            print(f"  {waiter.resource_kind}/{waiter.resource}  task={waiter.task_id}"
                  + (f"  goal={waiter.goal_id}" if waiter.goal_id else ""))
            print(f"  enqueued_at={waiter.enqueued_at}  deadline={waiter.deadline}  "
                  f"attempts={waiter.attempts}")
            return 0
        waiter = next((w for w in _waiters() if w["task_id"] == args.id), None)
        if waiter is not None:
            if args.json:
                _emit(waiter)
                return 0
            print(f"task {waiter['task_id']}: waiting for mutation lock")
            print(f"  {waiter['resource_kind']}/{waiter['resource']}")
            print(f"  pos={waiter['position']}  waiter={waiter['waiter_id']}")
            print(f"  attempts={waiter['attempts']}  deadline={waiter['deadline']}  "
                  f"next_retry={waiter['next_retry']}"
                  + (f"  goal={waiter['goal_id']}" if waiter["goal_id"] else ""))
            return 0
        print(f"no lock or waiter found for id: {args.id}")
        return 1

    if args.locks_command == "reclaim":
        try:
            reclaimed = engine.reclaim_lock(args.id)
        except Exception as exc:
            print(f"lock reclaim rejected: {exc}")
            return 1
        if args.json:
            _emit(reclaimed)
            return 0
        print(f"lock {reclaimed['lock_id']}: reclaimed "
              f"({reclaimed['capability']}/{reclaimed['action']} on {reclaimed['resource']})")
        return 0

    print(f"unknown locks command: {args.locks_command}")
    return 1


def _recovery_command(args, engine) -> int:
    """arion recovery list|show|acknowledge (durable registry, ADR-020).

    Talks to the recovery store / engine domain interfaces only - never raw
    SQLite. Output is bounded and secret-free (identifiers + reasons only).
    """
    import json

    store = getattr(engine, "recovery_store", None)
    if store is None:
        print("mutation recovery registry is not available on this engine")
        return 1

    def _emit(obj):
        if getattr(args, "json", False):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)

    if args.recovery_command == "list":
        recs = store.list_recoveries(status=args.status)
        if args.json:
            _emit([r.to_dict() for r in recs])
            return 0
        for r in recs:
            line = (f"{r.recovery_id}  {r.status.value:<12} {r.capability}/{r.action}  "
                    f"{('on ' + str(r.resource)) if r.resource else ''}  task={r.task_id}"
                    + (f"  goal={r.goal_id}" if r.goal_id else ""))
            if r.acknowledged_by:
                line += f"  acknowledged_by={r.acknowledged_by}"
            print(line)
        return 0

    if args.recovery_command == "show":
        rec = store.get_recovery(args.recovery_id)
        if rec is None:
            print(f"recovery {args.recovery_id} not found")
            return 1
        if args.json:
            _emit(rec.to_dict())
            return 0
        print(f"recovery {rec.recovery_id}: {rec.status.value}")
        print(f"  {rec.capability}/{rec.action} {('on ' + str(rec.resource)) if rec.resource else ''}")
        print(f"  task={rec.task_id} step={rec.step_index} goal={rec.goal_id or '-'}")
        print(f"  reason: {rec.reason}")
        print(f"  created_at={rec.created_at}")
        if rec.acknowledged_by:
            print(f"  acknowledged_by={rec.acknowledged_by} at={rec.acknowledged_at}")
        return 0

    if args.recovery_command == "acknowledge":
        try:
            rec = engine.acknowledge_recovery(args.recovery_id, actor=args.actor)
        except Exception as exc:
            print(f"recovery acknowledgement rejected: {exc}")
            return 1
        if args.json:
            _emit(rec.to_dict())
            return 0
        print(f"recovery {rec.recovery_id}: {rec.status.value} "
              f"({rec.capability}/{rec.action}) acknowledged by {rec.acknowledged_by}")
        return 0

    print(f"unknown recovery command: {args.recovery_command}")
    return 1


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
