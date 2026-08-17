"""Prune authority/adversarial boundary (ADR-014 addendum, Phase D).

Pruning is STORAGE HYGIENE ONLY. It must never modify, influence, or be
influenced by authority state:

- scheduler/task/goal authority tables (work ownership, scheduler leases,
  reservations, ceilings, weights, DWRR deficit/credit, config);
- mutation locks / lock waiters / approvals / recoveries / checkpoints;
- forged telemetry, fake memory metadata, malformed ids, oversized values,
  and planner/model output cannot influence prune selection;
- deleting ALL memory leaves scheduler behavior byte-identical (a control
  DB and a pruned DB produce identical scheduler outcomes afterwards).

All timestamps fixed; all comparisons deterministic.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from arion.cognition.models import Belief
from arion.cognition.store import SQLiteCognitiveStore
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _dump(db, exclude=()) -> dict[str, list]:
    conn = sqlite3.connect(db)
    try:
        out = {}
        for t in _tables(conn):
            if t in exclude:
                continue
            out[t] = conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
        return out
    finally:
        conn.close()


def _seed_memory(db, n=3):
    store = SQLiteMemoryStore(db)
    for i in range(n):
        created = _iso_plus(T0, i * 10)
        store.record_episode(Episode(
            episode_id=f"ep-{i}", task_id=f"t-{i}", goal_id="g1",
            goal=f"goal {i}", outcome="completed", importance=0.5,
            created_at=created, updated_at=created,
            reflection_id=f"refl-{i}" if i == 1 else None,
        ))
    store.record_reflection(Reflection(
        reflection_id="refl-1", episode_id="ep-1",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="lesson", recommendation="", confidence="medium",
        importance=0.5, created_at=_iso_plus(T0, 10)))
    store.close()


def _seed_cognition(db):
    store = SQLiteCognitiveStore(db)
    store.record_belief(Belief(
        belief_id="b-0", category="semantic", statement="stmt A",
        confidence=0.5, importance=0.5, version=1,
        provenance={"episode_ids": ["ep-0"]}, source="deterministic",
        created_at=_iso_plus(T0, 0), updated_at=_iso_plus(T0, 5),
        superseded_at=_iso_plus(T0, 5)))
    store.record_belief(Belief(
        belief_id="b-1", category="semantic", statement="stmt A",
        confidence=0.7, importance=0.5, version=2,
        provenance={"episode_ids": ["ep-1"]}, source="deterministic",
        created_at=_iso_plus(T0, 10), updated_at=_iso_plus(T0, 15),
        superseded_at=_iso_plus(T0, 15)))
    store.record_belief(Belief(
        belief_id="b-active", category="semantic", statement="stmt A",
        confidence=0.9, importance=0.5, version=3,
        provenance={"episode_ids": ["ep-2"]}, source="deterministic",
        created_at=_iso_plus(T0, 20), updated_at=_iso_plus(T0, 20)))
    for v in range(1, 4):
        store.record_goal_plan("g1", v, "direct", [{"v": v}], reason="r")
    store.close()


def _seed_authority_rows(db):
    """Direct-SQL seed of every authority table (realistic, fixed timestamps)."""
    _storage = SQLiteStorage(db)   # creates the state schema (migrations)
    _storage.close()
    conn = sqlite3.connect(db)
    try:
        conn.executescript(f"""
        INSERT INTO goals (id, description, source, status, version, strategy,
                           blockers, progress_metadata, created_at, updated_at)
        VALUES ('g1', 'goal one', 'test', 'active', 3, 'direct',
                '[]', '{{}}', '{T0}', '{_iso_plus(T0, 30)}');
        INSERT INTO tasks (id, goal_id, description, status, snapshot, updated_at)
        VALUES ('t-1', 'g1', 'task one', 'completed', '{{"steps": []}}', '{_iso_plus(T0, 20)}');
        INSERT INTO checkpoints (id, task_id, status, step_index, snapshot, reason, created_at)
        VALUES ('ck-1', 't-1', 'completed', 2, '{{"x": 1}}', 'step done', '{_iso_plus(T0, 10)}');
        INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index, scheduler_id,
                                    worker_id, status, attempts, error, created_at,
                                    started_at, completed_at, lease_expires_at)
        VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', 'worker-1', 'running', 1, NULL,
                '{T0}', '{T0}', NULL, '{_iso_plus(T0, 60)}'),
               ('sw-2', 't-1', 'g1', 1, 'sched-1', NULL, 'queued', 0, NULL,
                '{_iso_plus(T0, 5)}', NULL, NULL, NULL);
        INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                         heartbeat_at, lease_expires_at)
        VALUES ('sched-1', 4242, '{T0}', '{_iso_plus(T0, 20)}', '{_iso_plus(T0, 60)}');
        INSERT INTO scheduler_config (key, value) VALUES ('max_lease_seconds', '120');
        INSERT INTO scheduler_goal_weights (goal_id, weight, enabled, updated_at, updated_by)
        VALUES ('g1', 3, 1, '{_iso_plus(T0, 10)}', 'operator');
        INSERT INTO scheduler_goal_state (goal_id, deficit, updated_at)
        VALUES ('g1', 2, '{_iso_plus(T0, 15)}');
        INSERT INTO scheduler_goal_reservations (goal_id, reservation, enabled, updated_at, updated_by)
        VALUES ('g1', 1, 1, '{_iso_plus(T0, 10)}', 'operator');
        INSERT INTO scheduler_goal_ceilings (goal_id, ceiling, enabled, updated_at, updated_by)
        VALUES ('g1', 5, 1, '{_iso_plus(T0, 10)}', 'operator');
        INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id, task_id,
                                      work_id, step_index, event_type, reason, success,
                                      detail, schema_version)
        VALUES ('se-1', '{T0}', 'sched-1', 'worker-1', 'g1', 't-1', 'sw-1', 0,
                'work.claimed', NULL, 1, '{{"work_id": "sw-1"}}', 1);
        INSERT INTO mutation_locks (lock_id, resource_kind, resource, capability,
                                    action, owner_id, acquired_at, expires_at)
        VALUES ('lock-1', 'filesystem:path', 'x.txt', 'filesystem.write',
                'write', 'worker-1', '{T0}', '{_iso_plus(T0, 60)}');
        INSERT INTO mutation_lock_waiters (waiter_id, resource_kind, resource, task_id,
                                           goal_id, step_index, seq, enqueued_at,
                                           deadline, attempts, next_retry, status, created_at, updated_at)
        VALUES ('w-1', 'filesystem:path', 'x.txt', 't-1', 'g1', 1, 1, '{T0}',
                '{_iso_plus(T0, 60)}', 0, NULL, 'waiting', '{T0}', '{T0}');
        INSERT INTO approval_requests (approval_id, task_id, step_index, goal_id,
                                       capability, action, scope, risk, side_effects,
                                       resource_kind, resource, summary, status,
                                       requester_actor, actor_chain, params_keys,
                                       fingerprint, created_at, updated_at)
        VALUES ('ap-1', 't-1', 1, 'g1', 'filesystem.write', 'write', 'filesystem:write',
                'medium', 'mutating', 'filesystem:path', 'x.txt', 'write x',
                'pending', 'agent:arion', '["user:alice","agent:arion"]', '["path"]',
                'fp-1', '{T0}', '{T0}');
        INSERT INTO mutation_recoveries (recovery_id, task_id, goal_id, step_index,
                                         capability, action, resource, reason, status,
                                         created_at)
        VALUES ('rec-1', 't-1', 'g1', 1, 'filesystem.write', 'write', 'x.txt',
                'interrupted', 'required', '{T0}');
        """)
        conn.commit()
    finally:
        conn.close()


def _prune_everything(db):
    """Prune ALL memory + cognitive history (deterministic, fixed timestamps)."""
    store = SQLiteMemoryStore(db)
    assert store.prune(older_than=_iso_plus(T0, 100000)) == 3
    assert store.count_reflections() == 0
    store.close()
    cog = SQLiteCognitiveStore(db)
    cog.prune_superseded_beliefs(older_than=_iso_plus(T0, 100000))
    cog.prune_goal_plans(keep_latest=1)
    cog.close()


# --------------------------------------------- authority tables untouched

def test_prune_all_leaves_authority_tables_byte_identical(tmp_path):
    db = str(tmp_path / "a.db")
    _seed_memory(db)
    _seed_cognition(db)
    _seed_authority_rows(db)

    authority = ("goals", "tasks", "checkpoints", "approval_requests",
                 "mutation_recoveries", "mutation_locks", "mutation_lock_waiters",
                 "scheduler_work", "scheduler_instances", "scheduler_config",
                 "scheduler_goal_weights", "scheduler_goal_state",
                 "scheduler_goal_reservations", "scheduler_goal_ceilings",
                 "scheduler_events")
    before = _dump(db, exclude=("episodic_memories", "reflections", "beliefs",
                                "goal_plans", "audit_events"))
    assert set(authority) <= set(before)  # every authority table present

    _prune_everything(db)

    after = _dump(db, exclude=("episodic_memories", "reflections", "beliefs",
                               "goal_plans", "audit_events"))
    assert after == before
    # and the memory tables really were emptied
    store = SQLiteMemoryStore(db)
    assert store.list_recent(limit=1000) == []
    store.close()


def test_prune_all_leases_and_ownership_preserved(tmp_path):
    db = str(tmp_path / "b.db")
    _seed_memory(db)
    _seed_authority_rows(db)
    conn = sqlite3.connect(db)
    work_before = conn.execute(
        "SELECT work_id, worker_id, status, lease_expires_at FROM scheduler_work "
        "ORDER BY work_id").fetchall()
    inst_before = conn.execute(
        "SELECT scheduler_id, pid, heartbeat_at, lease_expires_at "
        "FROM scheduler_instances").fetchall()
    lock_before = conn.execute(
        "SELECT lock_id, owner_id, acquired_at, expires_at FROM mutation_locks"
    ).fetchall()
    conn.close()

    _prune_everything(db)

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT work_id, worker_id, status, lease_expires_at FROM scheduler_work "
        "ORDER BY work_id").fetchall() == work_before
    assert conn.execute(
        "SELECT scheduler_id, pid, heartbeat_at, lease_expires_at "
        "FROM scheduler_instances").fetchall() == inst_before
    assert conn.execute(
        "SELECT lock_id, owner_id, acquired_at, expires_at FROM mutation_locks"
    ).fetchall() == lock_before
    conn.close()


def test_scheduler_behavior_byte_identical_after_delete_all_memory(tmp_path):
    """A control DB and a fully-pruned DB produce identical scheduler outcomes."""
    def _scenario(db):
        # Deterministic seeding (fixed ids/timestamps): register + config +
        # one queued work row, then a REAL claim (moves DWRR deficit).
        _st = SQLiteStorage(db)      # create schema
        _st.close()
        conn = sqlite3.connect(db)
        conn.executescript(f"""
            INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                             heartbeat_at, lease_expires_at)
            VALUES ('sched-1', 111, '{T0}', '{T0}', '{_iso_plus(T0, 60)}');
            INSERT INTO scheduler_config (key, value) VALUES ('max_lease_seconds', '120');
            INSERT INTO scheduler_goal_weights (goal_id, weight, enabled,
                                                updated_at, updated_by)
            VALUES ('g1', 3, 1, '{_iso_plus(T0, 1)}', 'operator');
            INSERT INTO scheduler_goal_reservations (goal_id, reservation, enabled,
                                                     updated_at, updated_by)
            VALUES ('g1', 1, 1, '{_iso_plus(T0, 1)}', 'operator');
            INSERT INTO scheduler_goal_ceilings (goal_id, ceiling, enabled,
                                                 updated_at, updated_by)
            VALUES ('g1', 5, 1, '{_iso_plus(T0, 1)}', 'operator');
            INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                        scheduler_id, worker_id, status, attempts,
                                        error, created_at, started_at, completed_at,
                                        lease_expires_at)
            VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', NULL, 'queued', 0, NULL,
                    '{_iso_plus(T0, 2)}', NULL, NULL, NULL);
        """)
        conn.commit()
        conn.close()
        storage = SQLiteStorage(db)
        claimed = storage.claim_next("sched-1", "worker-1", lease_seconds=30,
                                     now=_iso_plus(T0, 3))
        assert claimed is not None and claimed.work_id == "sw-1"
        storage.close()

    def _probe(db):
        """Run a fresh scheduler operation; return its outcome (deterministic)."""
        storage = SQLiteStorage(db)
        w2 = storage.create(task_id="t-2", goal_id="g1", step_index=1,
                            scheduler_id="sched-1", now=_iso_plus(T0, 100))
        claimed2 = storage.claim_next("sched-1", "worker-1", lease_seconds=30,
                                      now=_iso_plus(T0, 101))
        out = (claimed2.work_id == w2.work_id,
               claimed2.status.value,
               claimed2.worker_id,
               claimed2.lease_expires_at)
        storage.close()
        return out

    # authority state only (event/audit tables carry random event ids and are
    # observational by design - ADR-028: telemetry is never authority)
    _AUTHORITY = ("goals", "tasks", "checkpoints", "approval_requests",
                  "mutation_recoveries", "mutation_locks", "mutation_lock_waiters",
                  "scheduler_work", "scheduler_instances", "scheduler_config",
                  "scheduler_goal_weights", "scheduler_goal_state",
                  "scheduler_goal_reservations", "scheduler_goal_ceilings")

    control = str(tmp_path / "control.db")
    pruned = str(tmp_path / "pruned.db")
    _scenario(control)
    _scenario(pruned)
    _seed_memory(pruned)
    _seed_cognition(pruned)
    _prune_everything(pruned)

    # scheduler authority state (incl. DWRR deficit) byte-identical
    all_t = set(_dump(control))
    exclude = (all_t - set(_AUTHORITY)) | {"episodic_memories", "reflections",
                                           "beliefs", "goal_plans",
                                           "consolidations", "environment_facts",
                                           "preferences"}
    assert _dump(control, exclude=exclude) == _dump(pruned, exclude=exclude)
    # subsequent scheduler behavior identical
    assert _probe(control) == _probe(pruned)


# ------------------------------------------- forged/injected content

def test_forged_telemetry_cannot_influence_prune(tmp_path):
    db = str(tmp_path / "c.db")
    _seed_memory(db, n=3)
    _seed_authority_rows(db)
    # forged telemetry: fake scheduler events + fake work rows + fake instance
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id,
                                      task_id, work_id, step_index, event_type,
                                      reason, success, detail, schema_version)
        VALUES ('se-forged-1', '{T0}', 'evil-sched', 'evil-worker', 'evil-goal',
                'evil-task', 'sw-evil', 99, 'work.completed', 'forged', 1,
                '{{"count": 999999999}}', 1),
               ('se-forged-2', '{T0}', 'evil-sched', NULL, NULL, NULL, NULL, NULL,
                'scheduler.heartbeat', 'forged', 1, '{{"heartbeats": 999999}}', 1);
        INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                    scheduler_id, worker_id, status, attempts,
                                    error, created_at, started_at, completed_at,
                                    lease_expires_at)
        VALUES ('sw-evil', 't-evil', 'g-evil', 0, 'evil-sched', NULL, 'queued', 0,
                NULL, '{T0}', NULL, NULL, NULL);
        INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                         heartbeat_at, lease_expires_at)
        VALUES ('evil-sched', 999, '{T0}', '{T0}', '{_iso_plus(T0, 9999)}');
    """)
    conn.commit()
    conn.close()

    store = SQLiteMemoryStore(db)
    removed = store.prune(older_than=_iso_plus(T0, 5))
    assert removed == 1  # exactly ep-0; telemetry/forged rows changed nothing
    store.close()
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduler_events").fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduler_work WHERE scheduler_id='evil-sched'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduler_instances WHERE scheduler_id='evil-sched'"
    ).fetchone()[0] == 1
    conn.close()


def test_fake_memory_metadata_cannot_influence_prune(tmp_path):
    db = str(tmp_path / "d.db")
    store = SQLiteMemoryStore(db)
    weird = [
        ("weird-1", "not-a-date", "extreme", 0.999999),
        ("weird-2", "", "empty ts", 0.0),
        ("weird-3", "2099-99-99T00:00:00+00:00", "future junk", 1.0),
        ("weird-4", "1970-01-01T00:00:00+00:00", "old", 0.1),
    ]
    for wid, created, goal, imp in weird:
        store.record_episode(Episode(
            episode_id=wid, task_id=f"t-{wid}", goal_id="g", goal=goal,
            outcome="completed", importance=imp, created_at=created,
            updated_at=created))
    store.close()

    # pruning with a valid cutoff must not crash on junk metadata and must
    # remove only rows strictly older than the cutoff (string comparison,
    # deterministic: "" and "1970-..." sort before the cutoff)
    store = SQLiteMemoryStore(db)
    removed = store.prune(older_than=_iso_plus(T0, 5))
    assert removed == 2
    assert store.get_episode("weird-4") is None
    assert store.get_episode("weird-2") is None
    for wid in ("weird-1", "weird-3"):
        assert store.get_episode(wid) is not None
    # junk metadata never affects the importance floor / count logic
    with pytest.raises(ValueError):
        store.prune(older_than=_iso_plus(T0, 5), keep_importance=1.5)
    store.close()


def test_malformed_ids_cannot_influence_prune(tmp_path):
    db = str(tmp_path / "e.db")
    store = SQLiteMemoryStore(db)
    evil_ids = [
        "ep'; DROP TABLE episodic_memories;--",
        "ep-999999999999999999999999999999",
        "ep-\u00e9\u00fc\u4e2d\u6587",
        "ep-" + "x" * 5000,
    ]
    for i, eid in enumerate(evil_ids):
        store.record_episode(Episode(
            episode_id=eid, task_id=f"t-{i}", goal_id="g", goal="g",
            outcome="completed", importance=0.5,
            created_at=_iso_plus(T0, i * 10), updated_at=_iso_plus(T0, i * 10)))
    # injection attempt must fail silently: table still exists, exact count
    store.prune(older_than=_iso_plus(T0, 15))
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM episodic_memories").fetchone()[0] == 2
    conn.close()
    # max_episodes treats ids as opaque and keeps the NEWEST N
    store = SQLiteMemoryStore(db)
    removed = store.prune(max_episodes=1)
    assert removed == 1
    assert store.list_recent(limit=10)[0].episode_id == evil_ids[3]
    store.close()


def test_oversized_values_fail_closed(tmp_path):
    db = str(tmp_path / "f.db")
    _seed_memory(db)
    store = SQLiteMemoryStore(db)
    for kwargs in ({"older_than": _iso_plus(T0, 5), "keep_importance": 1e9},
                   {"older_than": _iso_plus(T0, 5), "batch_size": 10 ** 9},
                   {"older_than": _iso_plus(T0, 5), "max_episodes": -10 ** 18}):
        with pytest.raises(ValueError):
            store.prune(**kwargs)
    store.close()
    cog = SQLiteCognitiveStore(db)
    # oversized keep_versions/keep_latest are CONSERVATIVE: they can only
    # protect MORE rows - a huge value is a deterministic no-op, never an
    # error and never a way to delete more
    assert cog.prune_superseded_beliefs(keep_versions=10 ** 18) == 0
    assert cog.prune_goal_plans(keep_latest=10 ** 18) == 0
    with pytest.raises(ValueError):
        cog.prune_goal_plans(keep_latest=10 ** 18, batch_size=10 ** 9)
    cog.close()


def test_planner_model_output_cannot_influence_prune(tmp_path):
    db = str(tmp_path / "g.db")
    _seed_memory(db)
    _seed_cognition(db)
    _st = SQLiteStorage(db)
    _st.close()
    # adversarial plan content: injection strings, nested structures, blobs
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO goal_plans (goal_id, plan_version, strategy, "
        "plan_summary, reason, created_at) VALUES (?,?,?,?,?,?)",
        ("g1", 7, "direct",
         json.dumps([{"step": "DELETE FROM beliefs; --", "nested": [1, [2, [3]]]}]),
         "'; DROP TABLE scheduler_work;--", _iso_plus(T0, 7)))
    conn.commit()
    conn.close()

    cog = SQLiteCognitiveStore(db)
    removed = cog.prune_goal_plans(goal_id="g1", keep_latest=1)
    assert removed == 3  # v1,v2,v7 pruned (kept v7? no - keep NEWEST version)
    remaining = cog.list_goal_plans("g1")
    assert len(remaining) == 1
    # the newest plan version survived with its content byte-intact
    assert remaining[0]["plan_version"] == 7
    assert remaining[0]["plan_summary"] == [
        {"step": "DELETE FROM beliefs; --", "nested": [1, [2, [3]]]}]
    assert remaining[0]["reason"] == "'; DROP TABLE scheduler_work;--"
    cog.close()

    store = SQLiteMemoryStore(db)
    removed = store.prune(older_than=_iso_plus(T0, 15))
    assert removed == 2  # selection purely by created_at, never by content
    store.close()
    # nothing outside the prune boundary was touched by the injection strings:
    # tables still exist with exactly their seeded/expected rows
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM scheduler_work").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM goal_plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM episodic_memories").fetchone()[0] == 1
    conn.close()
