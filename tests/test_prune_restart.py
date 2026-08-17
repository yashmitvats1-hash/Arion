"""Prune restart/crash recovery (ADR-014 addendum, Phase F) - tests first,
using REAL subprocesses against one shared DB file.

- prune -> process restart -> reopen consistency (fresh process sees the
  pruned state, consolidations intact);
- crash DURING a bounded prune (uncommitted deletes, hard process death):
  SQLite recovery rolls back -> nothing lost, byte-identical to pre-crash;
  a repeated prune after recovery is deterministic (same result as a clean
  prune);
- consolidation + belief consistency after restart (consolidation-fed
  refresh works in a fresh process after episodes were pruned);
- scheduler authority state byte-identical across prune + restart.

All timestamps fixed; helper output is deterministic JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

T0 = "2026-01-01T00:00:00+00:00"

_HELPER = textwrap.dedent(
    """
    import json, os, sqlite3, sys
    sys.path.insert(0, %(repo)r)
    from arion.cognition.models import Belief
    from arion.cognition.state import CognitiveState
    from arion.cognition.store import SQLiteCognitiveStore
    from arion.memory.models import Episode, Reflection
    from arion.memory.store import ConsolidationRecord, SQLiteMemoryStore
    from arion.state.store import SQLiteStorage

    T0 = %(t0)r

    def _iso_plus(iso, seconds):
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(seconds=seconds)).isoformat()

    def _seed(db):
        SQLiteStorage(db).close()  # create state schema
        m = SQLiteMemoryStore(db)
        for i in range(3):
            created = _iso_plus(T0, i * 10)
            m.record_episode(Episode(
                episode_id=f"ep-{i}", task_id=f"t-{i}", goal_id="g1",
                goal=f"goal {i}", outcome="completed", importance=0.5,
                created_at=created, updated_at=created,
                reflection_id="refl-1" if i == 1 else None))
        m.record_reflection(Reflection(
            reflection_id="refl-1", episode_id="ep-1",
            what_happened="x", what_worked="", what_failed="", why="",
            lesson="lesson one", recommendation="", confidence="medium",
            importance=0.5, created_at=_iso_plus(T0, 10)))
        m.record_consolidation(ConsolidationRecord(
            consolidation_id="consol-1",
            source_episode_ids=["ep-0", "ep-1"],
            category="lesson", merged_lesson="merged lesson from restart",
            count=2, importance=0.6, created_at=_iso_plus(T0, 60)))
        m.close()
        c = SQLiteCognitiveStore(db)
        c.record_belief(Belief(
            belief_id="b-0", category="semantic", statement="stmt",
            confidence=0.4, importance=0.5, version=1,
            provenance={"episode_ids": ["ep-0"]}, source="deterministic",
            created_at=_iso_plus(T0, 0), updated_at=_iso_plus(T0, 2),
            superseded_at=_iso_plus(T0, 2)))
        c.record_belief(Belief(
            belief_id="b-1", category="semantic", statement="stmt",
            confidence=0.6, importance=0.5, version=2,
            provenance={"episode_ids": ["ep-0"]}, source="deterministic",
            created_at=_iso_plus(T0, 5), updated_at=_iso_plus(T0, 5),
            superseded_at=_iso_plus(T0, 5)))
        c.record_belief(Belief(
            belief_id="b-2", category="semantic", statement="stmt",
            confidence=0.8, importance=0.5, version=3,
            provenance={"episode_ids": ["ep-1"]}, source="deterministic",
            created_at=_iso_plus(T0, 10), updated_at=_iso_plus(T0, 10)))
        c.record_goal_plan("g1", 1, "direct", [{"v": 1}], reason="r1")
        c.record_goal_plan("g1", 2, "direct", [{"v": 2}], reason="r2")
        c.close()
        conn = sqlite3.connect(db)
        conn.executescript(\"\"\"
            INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                        scheduler_id, worker_id, status, attempts,
                                        error, created_at, started_at, completed_at,
                                        lease_expires_at)
            VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', 'worker-1', 'running',
                    1, NULL, '%(t0)s', '%(t0)s', NULL, '%(t0_60)s');
            INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                             heartbeat_at, lease_expires_at)
            VALUES ('sched-1', 42, '%(t0)s', '%(t0)s', '%(t0_60)s');
            INSERT INTO scheduler_goal_weights (goal_id, weight, enabled,
                                                updated_at, updated_by)
            VALUES ('g1', 3, 1, '%(t0)s', 'operator');
            INSERT INTO scheduler_goal_state (goal_id, deficit, updated_at)
            VALUES ('g1', 1, '%(t0)s');
        \"\"\")
        conn.commit()
        conn.close()

    def _verify(db):
        m = SQLiteMemoryStore(db)
        c = SQLiteCognitiveStore(db)
        conn = sqlite3.connect(db)
        out = {
            "episodes": [e.episode_id for e in m.list_recent(limit=100)],
            "reflections": len(m.list_recent_reflections(limit=100)),
            "consolidations": [r.consolidation_id for r in m.list_consolidations()],
            "beliefs": sorted(b.belief_id for b in
                              c.list_beliefs(limit=100, include_superseded=True)),
            "plans": [p["plan_version"] for p in c.list_goal_plans("g1")],
            "scheduler_work": conn.execute(
                "SELECT work_id, worker_id, status, lease_expires_at "
                "FROM scheduler_work ORDER BY work_id").fetchall(),
            "scheduler_instances": conn.execute(
                "SELECT scheduler_id, pid, heartbeat_at, lease_expires_at "
                "FROM scheduler_instances").fetchall(),
            "scheduler_weights": conn.execute(
                "SELECT goal_id, weight, enabled FROM scheduler_goal_weights"
            ).fetchall(),
            "scheduler_deficit": conn.execute(
                "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall(),
        }
        conn.close()
        m.close()
        c.close()
        return out

    db = sys.argv[1]
    mode = sys.argv[2]
    if mode == "seed":
        _seed(db)
        print(json.dumps(_verify(db), default=str), flush=True)
    elif mode == "prune":
        m = SQLiteMemoryStore(db)
        removed = m.prune(older_than=_iso_plus(T0, 15))
        m.close()
        print(json.dumps({"removed": removed}, default=str), flush=True)
    elif mode == "prune-beliefs":
        c = SQLiteCognitiveStore(db)
        removed = c.prune_superseded_beliefs(older_than=_iso_plus(T0, 100000))
        c.close()
        print(json.dumps({"removed": removed}, default=str), flush=True)
    elif mode == "prune-plans":
        c = SQLiteCognitiveStore(db)
        removed = c.prune_goal_plans(goal_id="g1", keep_latest=1)
        c.close()
        print(json.dumps({"removed": removed}, default=str), flush=True)
    elif mode == "refresh-consolidations":
        m = SQLiteMemoryStore(db)
        c = SQLiteCognitiveStore(db)
        facade = CognitiveState(memory=m, cognition=c)
        new_count = facade.refresh_from_memory(limit=20,
                                               include_consolidations=True)
        lifted = [b.statement for b in c.list_beliefs(limit=1000)
                  if "merged lesson from restart" in b.statement]
        prov = None
        for b in c.list_beliefs(limit=1000):
            if "merged lesson from restart" in b.statement:
                prov = b.provenance
                break
        c.close()
        m.close()
        print(json.dumps({"new": new_count, "lifted": lifted,
                          "provenance": prov}, default=str), flush=True)
    elif mode == "crash-mid-prune":
        # Deterministic crash DURING a bounded prune: select doomed rows,
        # delete reflections + the first episode batch, then HARD-DIE before
        # commit (like a killed process mid-loop).
        m = SQLiteMemoryStore(db)
        rows = m._conn.execute(
            "SELECT episode_id, created_at, importance FROM episodic_memories"
        ).fetchall()
        rows.sort(key=lambda r: r[1])
        doomed = [r[0] for r in rows if r[1] < _iso_plus(T0, 15)]
        chunk = doomed[:1]
        q = ",".join("?" * len(chunk))
        m._conn.execute(f"DELETE FROM reflections WHERE episode_id IN ({q})", chunk)
        m._conn.execute(f"DELETE FROM episodic_memories WHERE episode_id IN ({q})", chunk)
        # NO commit: process dies with an open, uncommitted transaction
        print(json.dumps({"doomed": len(doomed)}, default=str), flush=True)
        os._exit(1)
    elif mode == "verify":
        print(json.dumps(_verify(db), default=str), flush=True)
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    """
) % {
    "repo": str(Path(__file__).resolve().parent.parent),
    "t0": T0,
    "t0_60": "2026-01-01T00:01:00+00:00",
}


def _run(db, *args, expect=0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _HELPER, str(db), *args],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == expect, \
        f"helper {args!r} rc={proc.returncode}: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_prune_survives_restart_reopen_consistent(tmp_path):
    db = str(tmp_path / "r.db")
    _run(db, "seed")
    _run(db, "prune")                                   # prune in one process
    out = _run(db, "verify")                            # fresh process reopens
    assert out["episodes"] == ["ep-2"]                  # ep-0, ep-1 pruned
    assert out["reflections"] == 0                      # reflection went with
    assert out["consolidations"] == ["consol-1"]        # consolidation intact
    assert out["beliefs"] == ["b-0", "b-1", "b-2"]      # beliefs untouched
    assert out["plans"] == [1, 2]
    assert out["scheduler_work"] == [["sw-1", "worker-1", "running",
                                      "2026-01-01T00:01:00+00:00"]]


def test_crash_during_bounded_prune_recovers(tmp_path):
    db = str(tmp_path / "c.db")
    _run(db, "seed")
    before = _run(db, "verify")
    crashed = _run(db, "crash-mid-prune", expect=1)     # hard death, no commit
    assert crashed["doomed"] == 2

    after = _run(db, "verify")                          # fresh process: recovery
    assert after == before                              # rolled back, nothing lost

    # repeated prune after recovery: same result as a clean prune
    out = _run(db, "prune")
    assert out["removed"] == 2
    final = _run(db, "verify")
    assert final["episodes"] == ["ep-2"]
    assert final["consolidations"] == ["consol-1"]


def test_repeated_prune_after_recovery_idempotent(tmp_path):
    db = str(tmp_path / "i.db")
    _run(db, "seed")
    assert _run(db, "prune")["removed"] == 2
    assert _run(db, "prune")["removed"] == 0           # idempotent across restarts
    first = _run(db, "verify")
    assert _run(db, "verify") == first                  # stable across restarts


def test_consolidation_belief_consistency_after_restart(tmp_path):
    db = str(tmp_path / "k.db")
    _run(db, "seed")
    _run(db, "prune")                                   # episodes pruned
    out = _run(db, "refresh-consolidations")            # fresh process, flag on
    assert out["lifted"] == ["merged lesson from restart"]
    assert out["provenance"]["episode_ids"] == ["ep-0", "ep-1"]
    assert out["provenance"]["consolidation_ids"] == ["consol-1"]
    # idempotent across another restart
    again = _run(db, "refresh-consolidations")
    assert again["new"] == 0
    assert again["lifted"] == ["merged lesson from restart"]


def test_cognitive_prunes_survive_restart(tmp_path):
    db = str(tmp_path / "b.db")
    _run(db, "seed")
    assert _run(db, "prune-beliefs")["removed"] == 1    # b-1 superseded history
    assert _run(db, "prune-plans")["removed"] == 1      # v1 history
    out = _run(db, "verify")
    assert out["beliefs"] == ["b-1", "b-2"]             # superseded rows pruned
    assert out["plans"] == [2]
    assert out["episodes"] == ["ep-2", "ep-1", "ep-0"]  # memory untouched here


def test_scheduler_state_byte_identical_after_restart_prune(tmp_path):
    db = str(tmp_path / "s.db")
    _run(db, "seed")
    before = _run(db, "verify")
    _run(db, "prune")
    _run(db, "prune-beliefs")
    _run(db, "prune-plans")
    after = _run(db, "verify")
    for key in ("scheduler_work", "scheduler_instances",
                "scheduler_weights", "scheduler_deficit"):
        assert after[key] == before[key]
