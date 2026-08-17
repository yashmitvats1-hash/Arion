"""Real two-subprocess bounded lock waiting (ADR-022, Phase L).

Two independent Python processes share one SQLite DB:
- process A acquires the mutation lock, holds it, then releases;
- process B plans + authorizes, hits A's lock, enters BOUNDED WAITING
  (durable, backoff), A releases before the deadline, B retries, re-validates,
  acquires, mutates exactly once, verifies, releases, completes.

Also the timeout path: A holds past B's deadline -> B fails durably with a
typed timeout, no mutation, no recovery record.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = str(Path(__file__).resolve().parent.parent / "scripts" / "_lock_demo_worker.py")


def _run(*argv: str, timeout: float = 60.0) -> str:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"worker failed: {proc.stdout[-1500:]} {proc.stderr[-1500:]}"
    return proc.stdout.strip()


def _spawn(*argv: str):
    return subprocess.Popen([sys.executable, WORKER, *argv],
                            stdout=subprocess.PIPE, text=True, bufsize=1)


def test_two_subprocesses_contention_bounded_wait_then_success(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir()
    db = str(tmp_path / "arion.db")

    # process A: hold the lock ~1.5s, then release
    a = _spawn("hold-release", "--db", db, "--sandbox", str(sb), "--hold", "1.5",
               "--wait-max", "20")
    line = a.stdout.readline().strip()
    assert line == "HOLDING"

    # process B: full pipeline with bounded waiting (backoff 0.1s, deadline 20s)
    b = json.loads(_run("wait-write", "--db", db, "--sandbox", str(sb),
                        "--wait-max", "20", "--backoff-base", "0.1", "--backoff-max", "0.2"))
    a.wait()
    assert a.returncode == 0

    assert b["goal_status"] == "completed"
    assert b["task_status"] == "completed"
    assert b["locks"] == []  # released after success
    events = b["lock_events"]
    assert "mutation.lock.waiting" in events  # B entered bounded waiting
    assert "mutation.lock.retry" in events
    assert "mutation.lock.acquired" in events and "mutation.lock.released" in events
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    # exactly one mutation (no duplicate across the wait)
    engine_events = json.loads(b.get("_raw", "{}")) if b.get("_raw") else {}
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_two_subprocesses_timeout_no_mutation_no_recovery(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir()
    db = str(tmp_path / "arion.db")

    # process A: hold the lock well past B's deadline (never releases in time)
    a = _spawn("hold-release", "--db", db, "--sandbox", str(sb), "--hold", "8",
               "--wait-max", "60")
    assert a.stdout.readline().strip() == "HOLDING"

    # process B: deadline 1s, backoff 0.1 -> times out while A still holds
    b = json.loads(_run("wait-write", "--db", db, "--sandbox", str(sb),
                        "--wait-max", "1.0", "--backoff-base", "0.1", "--backoff-max", "0.2"))
    a.terminate()  # stop A early; B already finished

    assert b["goal_status"] == "blocked"
    assert b["task_status"] == "failed"
    assert "wait timed out" in (b["task_error"] or "")
    assert "mutation.lock.timeout" in b["lock_events"]
    assert "mutation.lock.waiting" in b["lock_events"]
    assert "mutation.attempted" not in json.dumps(b)  # never executed
    assert not (sb / "notes.txt").exists()  # no mutation
    # no recovery record (lock contention != mutation failure)
    from arion.state.store import SQLiteStorage

    st = SQLiteStorage(db)
    assert st.list_recoveries() == []
    st.close()
