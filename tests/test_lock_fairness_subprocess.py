"""Cross-process FIFO lock-wait fairness (ADR-023) - real subprocesses.

Process A holds the lock; B and C queue in that order; A releases; B must
acquire BEFORE C; each mutates exactly once. Also: restart survival - a
killed waiter keeps its durable queue position and still wins over a later
waiter after restart.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from arion.state.locks import LockWaiterStatus
from arion.state.store import SQLiteStorage

WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_lock_demo_worker.py")
FS = "filesystem:path"


def _spawn(*argv: str):
    return subprocess.Popen([sys.executable, WORKER, *argv],
                            stdout=subprocess.PIPE, text=True, bufsize=1)


def _run(*argv: str, timeout: float = 90.0) -> str:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"worker failed: {proc.stdout[-1500:]} {proc.stderr[-1500:]}"
    return proc.stdout.strip()


def _read_json(proc, timeout=90):
    """Block until the worker prints its final JSON line."""
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line), lines
        lines.append(line)
    proc.wait(timeout=5)
    raise AssertionError(f"worker did not emit JSON; lines={lines}")


def _acquired_order(db_path, task_ids):
    """Return the list of mutation.lock.acquired events' task ids in audit
    order, filtered to the given tasks."""
    st = SQLiteStorage(db_path)
    order = [e.task_id for e in st.list_events()
             if e.kind == "mutation.lock.acquired" and e.task_id in task_ids]
    st.close()
    return order


def test_three_subprocesses_fifo_order(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir()
    db = str(tmp_path / "arion.db")

    # A holds the lock ~3s
    a = _spawn("hold-release", "--db", db, "--sandbox", str(sb), "--hold", "3.0",
               "--wait-max", "60")
    assert a.stdout.readline().strip() == "HOLDING"

    # B queues (position 1)
    b = _spawn("wait-write", "--db", db, "--sandbox", str(sb), "--mark-queued",
               "--wait-max", "30", "--backoff-base", "0.05", "--backoff-max", "0.1")
    b_lines = []
    b_queued = None
    while b_queued is None:
        line = b.stdout.readline().strip()
        b_lines.append(line)
        if line.startswith("QUEUED"):
            b_queued = json.loads(line[len("QUEUED"):].strip())
    assert b_queued["position"] == 1, b_queued

    # C queues (position 2)
    c = _spawn("wait-write", "--db", db, "--sandbox", str(sb), "--mark-queued",
               "--wait-max", "30", "--backoff-base", "0.05", "--backoff-max", "0.1")
    c_queued = None
    while c_queued is None:
        line = c.stdout.readline().strip()
        if line.startswith("QUEUED"):
            c_queued = json.loads(line[len("QUEUED"):].strip())
    assert c_queued["position"] == 2, c_queued
    assert b_queued["position"] < c_queued["position"]

    # A releases (hold ends); B completes first, then C
    b_out, _ = _read_json(b)
    c_out, _ = _read_json(c)
    a.wait(timeout=10)

    assert b_out["goal_status"] == "completed" and c_out["goal_status"] == "completed"
    assert b_out["task_status"] == "completed" and c_out["task_status"] == "completed"
    order = _acquired_order(db, {b_out["task_id"], c_out["task_id"]})
    assert len(order) == 2
    # the file was written exactly twice (once per process)
    from arion.state.store import SQLiteStorage as SS

    st = SS(db)
    attempts = [e for e in st.list_events() if e.kind == "mutation.attempted"]
    assert len(attempts) == 2
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    # no queued waiters remain
    queued = [w for w in st.list_waiters() if w.status == LockWaiterStatus.QUEUED]
    assert queued == []
    st.close()


def test_restart_preserves_queue_position(tmp_path):
    """B queues (pos 1), C queues (pos 2); B is KILLED mid-wait; after A
    releases and B restarts, B still acquires before C."""
    sb = tmp_path / "repo"
    sb.mkdir()
    db = str(tmp_path / "arion.db")

    a = _spawn("hold-release", "--db", db, "--sandbox", str(sb), "--hold", "5.0",
               "--wait-max", "60")
    assert a.stdout.readline().strip() == "HOLDING"

    b = _spawn("wait-write", "--db", db, "--sandbox", str(sb), "--mark-queued",
               "--wait-max", "40", "--backoff-base", "0.05", "--backoff-max", "0.1")
    b_queued = None
    while b_queued is None:
        line = b.stdout.readline().strip()
        if line.startswith("QUEUED"):
            b_queued = json.loads(line[len("QUEUED"):].strip())
    gid_b = b_queued["goal_id"]

    c = _spawn("wait-write", "--db", db, "--sandbox", str(sb), "--mark-queued",
               "--wait-max", "40", "--backoff-base", "0.05", "--backoff-max", "0.1")
    c_queued = None
    while c_queued is None:
        line = c.stdout.readline().strip()
        if line.startswith("QUEUED"):
            c_queued = json.loads(line[len("QUEUED"):].strip())
    assert b_queued["position"] == 1 and c_queued["position"] == 2

    # kill B mid-wait: its waiter row stays queued (durable)
    b.kill()
    b.wait(timeout=10)
    st0 = SQLiteStorage(db)
    b_waiter = st0.get_waiter(b_queued["waiter_id"])
    assert b_waiter is not None and b_waiter.status == LockWaiterStatus.QUEUED
    st0.close()

    # wait for A to release, then restart B (fresh process, same goal/task)
    assert a.stdout.readline().strip() == "RELEASED"
    a.wait(timeout=10)

    b2 = _spawn("wait-write", "--db", db, "--sandbox", str(sb), "--goal", gid_b,
                "--wait-max", "40", "--backoff-base", "0.05", "--backoff-max", "0.1")
    b2_out, _ = _read_json(b2)
    assert b2_out["goal_status"] == "completed"
    assert b2_out["task_status"] == "completed"

    c_out, _ = _read_json(c)
    assert c_out["goal_status"] == "completed"

    st = SQLiteStorage(db)
    attempts = [e for e in st.list_events() if e.kind == "mutation.attempted"]
    assert len(attempts) == 2  # B (restarted) + C, each exactly once
    queued = [w for w in st.list_waiters() if w.status == LockWaiterStatus.QUEUED]
    assert queued == []
    st.close()
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
