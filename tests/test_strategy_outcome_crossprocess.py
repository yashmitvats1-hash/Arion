"""Strategy-outcome cross-process repair (ADR-015 addendum, Phase D5) -
tests first, using REAL subprocesses against one shared DB file.

- two repair workers running concurrently cannot create duplicate
  (goal_id, plan_version) outcome rows (UNIQUE + conflict-safe insert);
- the final outcome set is deterministic and complete;
- created_at remains stable (first writer wins; later repairs change
  nothing);
- no duplicate strategy.outcome events from the racing workers;
- no scheduler ownership/lease/config/DWRR/reservation/ceiling mutation.

The helper seeds N goals (2 plan versions each, completed) via the
authoritative funnels, then wipes ALL outcome rows (the crash window), then
runs repair in fresh processes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

T0 = "2026-01-01T00:00:00+00:00"

_HELPER = textwrap.dedent(
    """
    import json, sqlite3, sys
    sys.path.insert(0, %(repo)r)
    from arion.cognition.goals import GoalManager
    from arion.cognition.progress import DeterministicProgressEvaluator
    from arion.cognition.store import SQLiteCognitiveStore
    from arion.cognition.strategy import StrategySelector
    from arion.observability.events import EventLogger
    from arion.state.store import SQLiteStorage

    T0 = %(t0)r

    def _gm(db):
        storage = SQLiteStorage(db)
        cognitive = SQLiteCognitiveStore(db)
        gm = GoalManager(
            storage=storage, cognitive_store=cognitive,
            events=EventLogger(sinks=[storage]),
            strategy_selector=StrategySelector(),
            progress_evaluator=DeterministicProgressEvaluator(),
        )
        return gm, storage, cognitive

    def _authority(db):
        conn = sqlite3.connect(db)
        out = {}
        for t in ("scheduler_config", "scheduler_goal_weights",
                  "scheduler_goal_state", "scheduler_goal_reservations",
                  "scheduler_goal_ceilings", "mutation_locks"):
            out[t] = conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
        conn.close()
        return out

    def _verify(db):
        gm, storage, cognitive = _gm(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT goal_id, plan_version, strategy, outcome, reason, "
            "created_at FROM strategy_outcomes ORDER BY goal_id, plan_version"
        ).fetchall()
        events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE kind='strategy.outcome'"
        ).fetchone()[0]
        authority = _authority(db)
        conn.close()
        storage.close()
        cognitive.close()
        return {"rows": rows, "event_count": events, "authority": authority}

    db = sys.argv[1]
    mode = sys.argv[2]
    if mode == "seed":
        gm, storage, cognitive = _gm(db)
        for i in range(%(n_goals)d):
            gid = gm.create_goal(f"goal number {i}").id
            gm.record_plan_version(gid, "direct", [{"index": 0}],
                                   reason="initial_plan")
            gm.record_plan_version(gid, "capability_verified", [{"index": 0}],
                                   reason="replan_world_changed")
            gm.complete_goal(gid, reason="all_work_complete")
        storage.close()
        cognitive.close()
        print(json.dumps(_verify(db), default=str), flush=True)
    elif mode == "wipe":
        # crash window: authoritative state intact, ALL outcome rows gone
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM strategy_outcomes")
        conn.execute("DELETE FROM audit_events")  # drop seed-phase events
        conn.commit()
        conn.close()
        print(json.dumps(_verify(db), default=str), flush=True)
    elif mode == "repair":
        gm, storage, cognitive = _gm(db)
        written = gm.repair_strategy_outcomes()
        storage.close()
        cognitive.close()
        out = _verify(db)
        out["written"] = written
        print(json.dumps(out, default=str), flush=True)
    elif mode == "verify":
        print(json.dumps(_verify(db), default=str), flush=True)
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    """
) % {
    "repo": str(Path(__file__).resolve().parent.parent),
    "t0": T0,
    "n_goals": 30,   # 60 outcome rows: wide enough for real overlap
}


def _run(db, *args, expect=0, timeout=180) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _HELPER, str(db), *args],
        capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == expect, \
        f"helper {args!r} rc={proc.returncode}: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_async(db, *args):
    return subprocess.Popen(
        [sys.executable, "-c", _HELPER, str(db), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_concurrent_repair_workers_no_duplicates_deterministic(tmp_path):
    db = str(tmp_path / "x1.db")
    _run(db, "seed")
    _run(db, "wipe")

    a = _run_async(db, "repair")
    time.sleep(0.15)          # let A get ahead mid-repair (test-side only)
    b = _run_async(db, "repair")
    out_a, err_a = a.communicate(timeout=180)
    out_b, err_b = b.communicate(timeout=180)
    assert a.returncode == 0, err_a[-800:]
    assert b.returncode == 0, err_b[-800:]
    res_a = json.loads(out_a.strip().splitlines()[-1])
    res_b = json.loads(out_b.strip().splitlines()[-1])

    final = _run(db, "verify")
    rows = final["rows"]
    keys = [(r[0], r[1]) for r in rows]
    # exactly one row per (goal, plan_version) - no duplicates from the race
    assert len(keys) == len(set(keys)) == 60
    # deterministic, complete final set: every goal has v1 superseded + v2 succeeded
    by_goal: dict[str, dict] = {}
    for gid, pv, strategy, outcome, reason, created in rows:
        by_goal.setdefault(gid, {})[pv] = (strategy, outcome, reason)
    assert len(by_goal) == 30
    for gid, versions in by_goal.items():
        assert versions[1] == ("direct", "superseded", "replan_world_changed")
        assert versions[2] == ("capability_verified", "succeeded",
                               "all_work_complete")
    # at least one worker wrote (both may have split the work)
    assert res_a["written"] + res_b["written"] == 60
    # the LOSER of each race emitted no duplicate event: exactly 60 events
    assert final["event_count"] == 60


def test_create_race_first_writer_wins_deterministic(tmp_path):
    """Deterministic in-process simulation of the cross-process create race.

    Two store connections (== two processes sharing one DB). B reads the row
    as MISSING, pauses; A creates the row and commits; B resumes and writes.
    The first writer must win: B's write is a no-op (returns False), the
    winner's outcome_id + created_at survive, and no duplicate row exists.
    """
    import threading
    import types

    from arion.cognition.store import SQLiteCognitiveStore as Store

    db = str(tmp_path / "race.db")
    a = Store(db)
    b = Store(db)
    orig_get = Store.get_strategy_outcome.__wrapped__   # raw fn (no wrapper)

    gate = threading.Event()
    b_read_missing = threading.Event()

    def paused_get(self, goal_id, plan_version):
        row = orig_get(self, goal_id, plan_version)
        if row is None:
            b_read_missing.set()      # B's stale read happened
            assert gate.wait(10)      # hold B between read and insert
        return row

    b.get_strategy_outcome = types.MethodType(paused_get, b)
    results = {}

    def b_write():
        results["b"] = b.record_strategy_outcome(
            "g1", "goal one", "direct", 1, "superseded", reason="r")

    t = threading.Thread(target=b_write)
    t.start()
    assert b_read_missing.wait(10)    # B is now blocked with a stale "missing"
    assert a.record_strategy_outcome(
        "g1", "goal one", "direct", 1, "superseded", reason="r") is True
    winner = a.get_strategy_outcome("g1", 1)
    gate.set()                        # release B; B writes after A committed
    t.join(timeout=10)

    assert results["b"] is False      # the loser's write was a no-op
    row = a.get_strategy_outcome("g1", 1)
    assert row["outcome_id"] == winner["outcome_id"]     # winner preserved
    assert row["created_at"] == winner["created_at"]     # created_at stable
    assert a.count_strategy_outcomes() == 1              # no duplicate row
    a.close()
    b.close()


def test_created_at_stable_across_repeated_repair(tmp_path):
    db = str(tmp_path / "x2.db")
    _run(db, "seed")
    _run(db, "wipe")
    _run(db, "repair")
    first = _run(db, "verify")
    created1 = [(r[0], r[1], r[5]) for r in first["rows"]]

    # a third repair (fresh process) writes nothing and changes nothing
    third = _run(db, "repair")
    assert third["written"] == 0
    final = _run(db, "verify")
    assert final["rows"] == first["rows"]          # byte-identical rows
    created2 = [(r[0], r[1], r[5]) for r in final["rows"]]
    assert created2 == created1                     # created_at stable
    assert final["event_count"] == first["event_count"]  # no dup events


def test_concurrent_repair_no_authority_mutation(tmp_path):
    db = str(tmp_path / "x3.db")
    _run(db, "seed")
    authority_before = _run(db, "verify")["authority"]
    _run(db, "wipe")
    # wipe must not touch authority either
    assert _run(db, "verify")["authority"] == authority_before

    a = _run_async(db, "repair")
    time.sleep(0.1)
    b = _run_async(db, "repair")
    a.communicate(timeout=180)
    b.communicate(timeout=180)
    assert a.returncode == 0 and b.returncode == 0

    final = _run(db, "verify")
    assert final["authority"] == authority_before   # byte-identical
    # scheduler tables completely untouched by cross-process repair
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM scheduler_work").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduler_goal_weights").fetchone()[0] == 0
    conn.close()
