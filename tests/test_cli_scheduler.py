"""CLI scheduler inspection tests (ADR-025, Phase G).

`arion scheduler status|workers|queue|show <id>|reclaim <id>`:

- reads ONLY the durable registry through the domain store (never raw SQLite);
- output is bounded, metadata-only, secret-free, restart-safe;
- unknown work ids fail closed (exit 1);
- reclaim only moves a STALE RUNNING row (expired lease) to ABANDONED and
  never executes anything.
"""

from __future__ import annotations

import json

from arion.state.models import GoalStatus
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage
from arion.interfaces.cli import main as cli_main

from tests.test_cross_goal_concurrency import (
    _env,
    _submit,
    _task_for,
    SlowReadCapability,
    TwoStepPlanner,
    _read_step,
)


def _seed(tmp_path, db_name="cli.db"):
    """One completed goal run, producing durable registry rows."""
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=2, read_cap=SlowReadCapability(sleep=0.01),
               db_name=db_name)
    g1 = _submit(env, "goal one")
    g2 = _submit(env, "goal two")
    env.engine.run_goals([g1, g2])
    return env


def _run_cli(args, capsys=None, db=None):
    argv = args
    if db is not None:
        argv = [a.replace("__DB__", db) for a in argv]
    return cli_main(argv)


def test_cli_scheduler_status(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "status", "--json", "--db", db])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["total"] == 2
    assert out["completed"] == 2
    assert out["queued"] == 0 and out["running"] == 0
    assert out["stale_running_leases"] == 0
    assert "fn" not in json.dumps(out) and "content" not in json.dumps(out)
    env.engine.storage.close()


def test_cli_scheduler_queue_and_workers(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    # create a QUEUED + RUNNING row directly via the store (domain interface)
    task = _task_for(env, _submit(env, "goal three"))
    queued = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=task.goal_id, step_index=0,
        scheduler_id="sched-cli")
    running = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=task.goal_id, step_index=1,
        scheduler_id="sched-cli")
    env.engine.scheduler_registry.mark_running(
        running.work_id, worker_id="worker:cli:1", lease_seconds=60.0)

    rc = cli_main(["scheduler", "queue", "--json", "--db", db])
    queue = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [q["work_id"] for q in queue] == [queued.work_id]

    rc = cli_main(["scheduler", "workers", "--json", "--db", db])
    workers = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [w["work_id"] for w in workers] == [running.work_id]
    assert workers[0]["worker_id"] == "worker:cli:1"
    assert workers[0]["lease_expires_at"] is not None
    env.engine.storage.close()


def test_cli_scheduler_show(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    row = env.engine.scheduler_registry.list_work()[0]
    rc = cli_main(["scheduler", "show", row.work_id, "--json", "--db", db])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert d["work_id"] == row.work_id
    assert d["status"] == "completed"
    assert d["task_id"] and d["step_index"] == 0
    env.engine.storage.close()


def test_cli_scheduler_show_unknown_fails_closed(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "show", "sw_does_not_exist", "--db", db])
    err = capsys.readouterr().out
    assert rc == 1
    assert "unknown" in err.lower()
    env.engine.storage.close()


def test_cli_scheduler_reclaim_stale(tmp_path, capsys):
    """Reclaim works only on a RUNNING row whose lease expired; the row moves
    to ABANDONED (never executes, never authorizes)."""
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    task = _task_for(env, _submit(env, "goal three"))
    stale = env.engine.scheduler_registry.create(
        task_id=task.id, goal_id=task.goal_id, step_index=0,
        scheduler_id="sched-cli")
    env.engine.scheduler_registry.mark_running(
        stale.work_id, worker_id="worker:dead:9", lease_seconds=1.0)
    # active lease: reclaim fails closed
    rc = cli_main(["scheduler", "reclaim", stale.work_id, "--db", db])
    assert rc == 1
    assert "valid" in capsys.readouterr().out.lower()
    assert env.engine.scheduler_registry.get_work(stale.work_id).status == \
        SchedulerWorkStatus.RUNNING
    # expire the lease (injectable clock via future now on the row) -> reclaim ok
    import time as _time
    _time.sleep(1.1)
    rc = cli_main(["scheduler", "reclaim", stale.work_id, "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert env.engine.scheduler_registry.get_work(stale.work_id).status == \
        SchedulerWorkStatus.ABANDONED
    env.engine.storage.close()


def test_cli_scheduler_reclaim_unknown_and_non_running_fail_closed(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    rc = cli_main(["scheduler", "reclaim", "sw_nope", "--db", db])
    assert rc == 1
    assert "unknown" in capsys.readouterr().out.lower()
    row = env.engine.scheduler_registry.list_work()[0]  # completed
    rc = cli_main(["scheduler", "reclaim", row.work_id, "--db", db])
    assert rc == 1
    assert "only RUNNING" in capsys.readouterr().out
    env.engine.storage.close()
