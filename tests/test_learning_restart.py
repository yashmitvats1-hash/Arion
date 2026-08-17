"""Learning restart/crash recovery (ADR-013 addendum, Phase 9) - tests
first, using REAL subprocesses against one shared DB file.

- episode survives process restart;
- reflection can resume after restart (recorded-but-unreflected episode
  completes the pass on the next invocation);
- partially completed learning does not duplicate;
- crash after episode persistence does not lose experience (catch-up
  recovers it);
- crash during reflection leaves a recoverable state;
- retry completes the learning cycle;
- concurrent learning workers cannot double-apply the same episode;
- scheduler state remains unchanged by learning recovery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"

_HELPER = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, %(repo)r)
    from arion.capabilities.filesystem import FilesystemReadCapability
    from arion.capabilities.registry import CapabilityRegistry
    from arion.intelligence.planner import DeterministicPlanner
    from arion.intelligence.router import DeterministicRouter
    from arion.memory.store import SQLiteMemoryStore
    from arion.memory.lifecycle import build_episode_from_task
    from arion.observability.events import EventLogger
    from arion.orchestration.authz import Actor, PathPrefixBoundary, RelativePathBoundary, ResourcePolicy
    from arion.orchestration.engine import ArionEngine
    from arion.state.store import SQLiteStorage

    FS = "filesystem:path"
    db = sys.argv[1]
    sandbox = sys.argv[2]
    mode = sys.argv[3]
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db) if mode != "complete-without-learning" else None
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"), memory=memory)

    if mode == "complete":
        task = engine.execute_goal("summarize this repository")
        print(json.dumps({"task_id": task.id,
                          "status": task.status.value}), flush=True)
    elif mode == "complete-without-learning":
        # like a crash between the terminal save and _record_memory:
        # the task is durably COMPLETED but no memory store was wired
        task = engine.execute_goal("summarize this repository")
        print(json.dumps({"task_id": task.id,
                          "status": task.status.value}), flush=True)
    elif mode == "record-episode-only":
        # crash-after-episode-persistence: record the episode, then die
        task = storage.load_task(sys.argv[4])
        ep = build_episode_from_task(task)
        memory.record_episode(ep)
        print(json.dumps({"episode_id": ep.episode_id}), flush=True)
        os._exit(1)  # crash BEFORE reflection
    elif mode == "learn-catchup":
        n = engine.learn_from_terminal_tasks()
        print(json.dumps({"recorded": n}), flush=True)
    elif mode == "learn-catchup-race":
        n = engine.learn_from_terminal_tasks()
        print(json.dumps({"recorded": n}), flush=True)
    storage.close()
    if memory is not None:
        memory.close()
    """
)


def _run_helper(db, sandbox, *args) -> dict:
    code = _HELPER % {"repo": str(Path(__file__).resolve().parent.parent)}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db), str(sandbox), *args],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"helper failed: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_helper_crash(db, sandbox, *args) -> dict:
    code = _HELPER % {"repo": str(Path(__file__).resolve().parent.parent)}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db), str(sandbox), *args],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, f"expected crash, got {proc.returncode}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _episodes_for(db, task_id: str) -> list:
    m = SQLiteMemoryStore(db)
    eps = [e for e in m.list_recent(limit=1000) if e.task_id == task_id]
    m.close()
    return eps


def test_episode_survives_process_restart(tmp_path, sandbox):
    db = str(tmp_path / "restart.db")
    out = _run_helper(db, str(sandbox), "complete")
    # the episode + reflection are visible to a fresh process
    m = SQLiteMemoryStore(db)
    episodes = [e for e in m.list_recent(limit=10)
                if e.task_id == out["task_id"]]
    assert len(episodes) == 1
    assert episodes[0].lifecycle == "consolidated"
    assert m.get_reflection(episodes[0].reflection_id) is not None
    m.close()


def test_crash_after_episode_persistence_is_recoverable(tmp_path, sandbox):
    """A worker records the episode then dies BEFORE reflection: the
    episode survives; a later catch-up pass resumes and completes the
    cycle (reflection + consolidation) exactly once."""
    db = str(tmp_path / "crash-ep.db")
    out = _run_helper(db, str(sandbox), "complete-without-learning")
    task_id = out["task_id"]
    # record the episode only, then crash (simulated in-process here via
    # the helper's record-episode-only mode)
    crash = _run_helper_crash(db, str(sandbox), "record-episode-only", task_id)
    ep_id = crash["episode_id"]
    eps = _episodes_for(db, task_id)
    assert len(eps) == 1 and eps[0].episode_id == ep_id
    assert eps[0].lifecycle == "recorded"  # reflection never ran
    assert eps[0].reflection_id is None
    # catch-up resumes the cycle and completes it exactly once
    out2 = _run_helper(db, str(sandbox), "learn-catchup")
    assert out2["recorded"] == 1
    eps = _episodes_for(db, task_id)
    assert len(eps) == 1  # still one episode - no duplicate
    assert eps[0].lifecycle == "consolidated"
    assert eps[0].reflection_id is not None
    # a second catch-up pass is a no-op
    out3 = _run_helper(db, str(sandbox), "learn-catchup")
    assert out3["recorded"] == 0
    assert len(_episodes_for(db, task_id)) == 1
    m = SQLiteMemoryStore(db)
    refs = m.list_recent_reflections(limit=100)
    assert len([r for r in refs if r.episode_id == ep_id]) == 1
    m.close()


def test_crash_before_episode_persistence_is_recovered_by_catch_up(tmp_path, sandbox):
    """The task is terminal but no episode exists (crash between the
    durable task save and the episode write): catch-up recovers it."""
    db = str(tmp_path / "crash-task.db")
    out = _run_helper(db, str(sandbox), "complete-without-learning")
    assert _episodes_for(db, out["task_id"]) == []
    # scheduler state before the catch-up pass
    storage = SQLiteStorage(db)
    work_before = [(w.work_id, w.status.value, w.worker_id)
                   for w in storage.list_work()]
    cap_before = storage.get_scheduler_global_max()
    storage.close()
    n = _run_helper(db, str(sandbox), "learn-catchup")
    assert n["recorded"] == 1
    eps = _episodes_for(db, out["task_id"])
    assert len(eps) == 1 and eps[0].lifecycle == "consolidated"
    # scheduler state is untouched by learning recovery
    storage = SQLiteStorage(db)
    assert storage.get_scheduler_global_max() == cap_before
    assert [(w.work_id, w.status.value, w.worker_id)
            for w in storage.list_work()] == work_before
    storage.close()


def test_concurrent_learning_workers_do_not_double_apply(tmp_path, sandbox):
    """Two processes run the catch-up pass concurrently: exactly one
    episode is created for the task (task-keyed uniqueness + idempotent
    pass)."""
    db = str(tmp_path / "race.db")
    out = _run_helper(db, str(sandbox), "complete-without-learning")
    task_id = out["task_id"]
    procs = []
    code = _HELPER % {"repo": str(Path(__file__).resolve().parent.parent)}
    for _ in range(2):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", code, db, str(sandbox), "learn-catchup"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        assert p.returncode == 0, stderr[-500:]
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    assert sum(r["recorded"] for r in results) >= 1  # at least one applied
    eps = _episodes_for(db, task_id)
    assert len(eps) == 1, f"concurrent workers created {len(eps)} episodes"
    assert eps[0].lifecycle == "consolidated"
    # exactly one reflection
    m = SQLiteMemoryStore(db)
    refs = [r for r in m.list_recent_reflections(limit=100)
            if r.episode_id == eps[0].episode_id]
    assert len(refs) == 1
    m.close()


def test_learning_recovery_leaves_scheduler_authority_untouched(tmp_path, sandbox):
    """A full engine run + catch-up with scheduler activity: learning
    recovery changes no scheduler rows, config, or ownership."""
    db = str(tmp_path / "sched.db")
    # complete a task with learning disabled to create a catch-up need
    out = _run_helper(db, str(sandbox), "complete-without-learning")
    task_id = out["task_id"]
    # add scheduler state directly
    storage = SQLiteStorage(db)
    storage.set_scheduler_global_max(4)
    row = storage.create(task_id=task_id, goal_id=None, step_index=0,
                         scheduler_id="sched-1")
    storage.claim(row.work_id, "w-1", 60.0, None, 600.0,
                  scheduler_id="sched-1")
    storage.close()
    # run catch-up learning
    n = _run_helper(db, str(sandbox), "learn-catchup")
    assert n["recorded"] == 1
    storage = SQLiteStorage(db)
    assert storage.get_scheduler_global_max() == 4
    work = storage.list_work()
    mine = [w for w in work if w.work_id == row.work_id]
    assert len(mine) == 1
    assert mine[0].status.value == "running"
    assert mine[0].worker_id == "w-1"
    assert storage.reclaim_stale() == []  # nothing was reclaimed by learning
    storage.close()
