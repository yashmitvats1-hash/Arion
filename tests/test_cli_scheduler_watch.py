"""CLI `scheduler watch` (ADR-028, Phases E/F).

- human + stable JSON output;
- filters (--goal/--scheduler/--work/--type/--since) and bounded --limit;
- --follow polls (bounded) and is strictly read-only: no mutation, no
  registration, no heartbeat, no claims;
- oversized --limit fails closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from arion.observability.events import AuditEvent
from arion.interfaces.cli import main as cli_main

from tests.test_cross_goal_concurrency import _env, TwoStepPlanner, _read_step

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _seed(tmp_path, db_name="cli.db"):
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=1, db_name=db_name)
    return env


def test_watch_json_output(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    st = env.engine.storage
    st.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"scheduler_id": "s-1", "worker_id": "w-1", "goal_id": "g-1",
                "work_id": "sw-1", "step_index": 0}))
    rc = cli_main(["scheduler", "watch", "--json", "--work", "sw-1", "--db", db])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(rows) == 1
    assert rows[0]["kind"] == "work.claimed"
    assert rows[0]["detail"]["work_id"] == "sw-1"
    assert "ts" in rows[0] and "id" in rows[0]
    env.engine.storage.close()


def test_watch_human_output_and_filters(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    st = env.engine.storage
    st.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"scheduler_id": "s-1", "worker_id": "w-1", "goal_id": "g-1",
                "work_id": "sw-1"}))
    st.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=_iso_plus(T0, 1),
        detail={"scheduler_id": "s-2", "worker_id": "w-2", "goal_id": "g-2",
                "work_id": "sw-2"}))
    rc = cli_main(["scheduler", "watch", "--goal", "g-2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sw-2" in out and "sw-1" not in out
    rc = cli_main(["scheduler", "watch", "--type", "work.claimed", "--db", db])
    assert "work.claimed" in capsys.readouterr().out
    rc = cli_main(["scheduler", "watch", "--since", _iso_plus(T0, 1), "--db", db])
    out = capsys.readouterr().out
    assert "sw-2" in out and "sw-1" not in out
    env.engine.storage.close()


def test_watch_limit_bounded_fails_closed(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    for bad in ("0", "-1", "5000"):
        rc = cli_main(["scheduler", "watch", "--limit", bad, "--db", db])
        assert rc == 1, bad
        assert "bounded" in capsys.readouterr().out
    env.engine.storage.close()


def test_watch_no_events(tmp_path, capsys):
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    # the engine construction emitted scheduler.registered; with a filter
    # matching nothing the output is empty
    rc = cli_main(["scheduler", "watch", "--type", "work.claimed", "--db", db])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
    env.engine.storage.close()


def test_watch_follow_is_read_only(tmp_path):
    """--follow must not register/heartbeat/claim or mutate: run it briefly
    in a subprocess while an event is appended; the registry state is
    unchanged afterwards."""
    env = _seed(tmp_path)
    db = str(env.engine.storage.db_path)
    st = env.engine.storage
    st.append_scheduler_event(AuditEvent(
        kind="work.claimed", ts=T0,
        detail={"scheduler_id": "s-1", "worker_id": "w-1", "goal_id": "g-1",
                "work_id": "sw-1"}))
    before = {
        "events": st.scheduler_event_count(),
        "instances": st._conn.execute(
            "SELECT COUNT(*) FROM scheduler_instances").fetchone()[0],
        "running": len(st.list_work(status="running")) if False else
        len([r for r in st.list_work() if r.status.value == "running"]),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "arion.interfaces.cli", "scheduler", "watch",
         "--follow", "--db", db],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        cwd=str(Path(__file__).resolve().parent.parent))
    time.sleep(2.0)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
    after = {
        "events": st.scheduler_event_count(),
        "instances": st._conn.execute(
            "SELECT COUNT(*) FROM scheduler_instances").fetchone()[0],
        "running": len([r for r in st.list_work()
                        if r.status.value == "running"]),
    }
    assert after == before, (before, after)
    env.engine.storage.close()
