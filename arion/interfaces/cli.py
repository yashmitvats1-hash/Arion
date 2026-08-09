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
        print(f"  [{mark}] step {step.index}: {step.intent} ({step.capability}/{step.action})")
        if step.error:
            print(f"        error: {step.error}")


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent.parent
    db_path = args.db or args.db_global or str(root / "arion_data" / "arion.db")

    engine = build_engine(
        db_path=db_path,
        sandbox_root=str(root),
        jsonl_log=str(root / "arion_data" / "events.jsonl") if args.command in ("run", "resume", "capabilities") else None,
    )
    storage = engine.storage

    if args.command == "run":
        task = engine.execute_goal(args.goal, source=args.source)
        print("goal executed")
        print(f"task_id: {task.id}")
        print(f"status: {task.status.value}")
        _print_task(engine, task.id)
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

    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
