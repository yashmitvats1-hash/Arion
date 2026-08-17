#!/usr/bin/env python3
"""ADR-014 addendum demo: cognitive-state archival (pruning + observability).

Deterministic, fully offline, single-process demonstration of the archival
policy (acceptance criteria A-I):

  A  memory prune: age/count/importance semantics, reflection coupling,
     consolidation protection, batch bounds, idempotency, dry-run.
  B  cognitive prune: superseded-belief pruning (active never pruned,
     keep_versions), goal-plan history bounding (latest never pruned).
  C  observability: bounded `memory.pruned` audit events, emitted only on
     real prunes (dry-run emits nothing and mutates nothing).
  D  CLI: `arion memory prune` / `arion cognition prune-superseded` /
     `arion cognition prune-plans` - dry-run, --json, exit 1 fail-closed.
  E  adversarial: forged telemetry / malformed ids / oversized values fail
     closed; pruning never touches scheduler authority; delete-all memory
     leaves scheduler behavior identical.
  F  consolidation-fed beliefs: merged lessons become procedural beliefs
     with complete provenance; idempotent; informational only.
  G  restart/reopen: prune survives a fresh store instance; re-derivation
     after pruning is deterministic.

No wall clock is used for any assertion (fixed timestamps everywhere).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arion.cognition.models import Belief
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.interfaces.cli import main as cli_main
from arion.memory.models import Episode, Reflection
from arion.memory.store import ConsolidationRecord, SQLiteMemoryStore
from arion.state.store import SQLiteStorage

CHECKS = 0
T0 = "2026-01-01T00:00:00+00:00"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def _prune_kw(kw):
    """Normalize a prune kwargs dict (empty = no criteria)."""
    return kw


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _tmp(name: str) -> str:
    return str(Path(tempfile.mkdtemp(prefix=f"arion-adr014-{name}-")) / "a.db")


def _ep(ep_id, t, importance=0.5, reflection=False):
    return Episode(
        episode_id=ep_id, task_id=f"t-{ep_id}", goal_id="g", goal=f"goal {ep_id}",
        outcome="completed", importance=importance,
        created_at=_iso_plus(T0, t), updated_at=_iso_plus(T0, t),
        reflection_id=f"refl-{ep_id}" if reflection else None,
    )


def _seed_memory(db, episodes, consolidation=None):
    m = SQLiteMemoryStore(db)
    for ep in episodes:
        m.record_episode(ep)
    for ep in episodes:
        if ep.reflection_id:
            m.record_reflection(Reflection(
                reflection_id=ep.reflection_id, episode_id=ep.episode_id,
                what_happened="x", what_worked="", what_failed="", why="",
                lesson=f"lesson of {ep.episode_id}", recommendation="",
                confidence="medium", importance=ep.importance,
                created_at=ep.created_at))
    if consolidation is not None:
        m.record_consolidation(consolidation)
    m.close()


def _dump(db, tables) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                for t in tables}
    finally:
        conn.close()


def _seed_beliefs(db):
    c = SQLiteCognitiveStore(db)
    for i in range(3):  # lineage X: three superseded versions
        c.record_belief(Belief(
            belief_id=f"b-X-{i}", category="semantic", statement="stmt X",
            confidence=0.5 + i * 0.1, importance=0.5, version=i + 1,
            provenance={"episode_ids": [f"ep-{i}"]}, source="deterministic",
            created_at=_iso_plus(T0, i * 10), updated_at=_iso_plus(T0, i * 10 + 5),
            superseded_at=_iso_plus(T0, i * 10 + 5)))
    c.record_belief(Belief(
        belief_id="b-X-active", category="semantic", statement="stmt X",
        confidence=0.9, importance=0.5, version=4,
        provenance={"episode_ids": ["ep-9"]}, source="deterministic",
        created_at=_iso_plus(T0, 40), updated_at=_iso_plus(T0, 40)))
    c.record_goal_plan("g1", 1, "direct", [{"v": 1}], reason="r1")
    c.record_goal_plan("g1", 2, "direct", [{"v": 2}], reason="r2")
    c.record_goal_plan("g1", 3, "direct", [{"v": 3}], reason="r3")
    c.close()


def _seed_scheduler_authority(db):
    SQLiteStorage(db).close()
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index,
                                    scheduler_id, worker_id, status, attempts,
                                    error, created_at, started_at, completed_at,
                                    lease_expires_at)
        VALUES ('sw-1', 't-1', 'g1', 0, 'sched-1', 'worker-1', 'running', 1, NULL,
                '{T0}', '{T0}', NULL, '{_iso_plus(T0, 60)}'),
               ('sw-2', 't-1', 'g1', 1, 'sched-1', NULL, 'queued', 0, NULL,
                '{_iso_plus(T0, 5)}', NULL, NULL, NULL);
        INSERT INTO scheduler_instances (scheduler_id, pid, registered_at,
                                         heartbeat_at, lease_expires_at)
        VALUES ('sched-1', 42, '{T0}', '{T0}', '{_iso_plus(T0, 60)}');
        INSERT INTO scheduler_config (key, value) VALUES ('max_lease_seconds', '120');
        INSERT INTO scheduler_goal_weights (goal_id, weight, enabled,
                                            updated_at, updated_by)
        VALUES ('g1', 3, 1, '{T0}', 'operator');
        INSERT INTO scheduler_goal_state (goal_id, deficit, updated_at)
        VALUES ('g1', 2, '{T0}');
        INSERT INTO scheduler_goal_reservations (goal_id, reservation, enabled,
                                                 updated_at, updated_by)
        VALUES ('g1', 1, 1, '{T0}', 'operator');
        INSERT INTO scheduler_goal_ceilings (goal_id, ceiling, enabled,
                                             updated_at, updated_by)
        VALUES ('g1', 5, 1, '{T0}', 'operator');
        INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id,
                                      task_id, work_id, step_index, event_type,
                                      reason, success, detail, schema_version)
        VALUES ('se-1', '{T0}', 'sched-1', 'worker-1', 'g1', 't-1', 'sw-1', 0,
                'work.claimed', NULL, 1, '{{"work_id": "sw-1"}}', 1);
    """)
    conn.commit()
    conn.close()


def _cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


# --------------------------------------------------------------------- A

def section_a_memory_prune() -> None:
    print("\n[A] memory prune: age / reflection coupling / consolidation / dry-run")
    db = _tmp("a")
    consol = ConsolidationRecord(
        consolidation_id="consol-1", source_episode_ids=["ep-0", "ep-1"],
        category="lesson", merged_lesson="merged lesson", count=2,
        importance=0.5, created_at=_iso_plus(T0, 60))
    _seed_memory(db, [
        _ep("ep-0", 0, importance=0.3),
        _ep("ep-1", 10, importance=0.5, reflection=True),
        _ep("ep-2", 20, importance=0.9),
        _ep("ep-3", 30, importance=0.4),
    ], consolidation=consol)

    m = SQLiteMemoryStore(db)
    before = _dump(db, ("episodic_memories", "reflections"))
    would = m.prune(older_than=_iso_plus(T0, 15), dry_run=True)
    check(would == 2 and _dump(db, ("episodic_memories", "reflections")) == before,
          f"dry-run reports 2 candidates ({would}) and is byte-identical")

    removed = m.prune(older_than=_iso_plus(T0, 15))
    check(removed == 2 and m.get_episode("ep-0") is None
          and m.get_episode("ep-1") is None,
          "real prune removes the 2 old episodes (ep-0, ep-1 gone)")
    check(m.list_recent_reflections(limit=10) == []
          and [c.consolidation_id for c in m.list_consolidations()] == ["consol-1"]
          and m.prune(older_than=_iso_plus(T0, 15)) == 0,
          "reflection pruned WITH episode; consolidation preserved; idempotent")

    removed = m.prune(older_than=_iso_plus(T0, 100), keep_importance=0.9)
    check(removed == 1 and m.list_recent(limit=10)[0].episode_id == "ep-2",
          f"importance floor protects ep-2 (0.9): removed {removed}")

    fail_closed = 0
    for kw in ({"batch_size": 0}, {"batch_size": 5001}, {},
               {"older_than": "junk"}, {"keep_importance": 2.0}):
        try:
            m.prune(**_prune_kw(kw))
            check(False, f"expected fail-closed for {kw or 'no criteria'}")
        except ValueError:
            fail_closed += 1
    check(fail_closed == 5, "fail closed: bad batch/importance/timestamp/no criteria")
    m.close()

    db2 = _tmp("a2")
    _seed_memory(db2, [_ep("e0", 0), _ep("e1", 10), _ep("e2", 20)])
    m2 = SQLiteMemoryStore(db2)
    removed = m2.prune(max_episodes=1)
    check(removed == 2 and m2.list_recent(limit=10)[0].episode_id == "e2",
          "max_episodes=1 keeps the NEWEST episode")
    m2.close()


# --------------------------------------------------------------------- B

def section_b_cognitive_prune() -> None:
    print("\n[B] cognitive prune: superseded beliefs + goal-plan history")
    db = _tmp("b")
    _seed_beliefs(db)
    c = SQLiteCognitiveStore(db)
    removed = c.prune_superseded_beliefs(keep_versions=1)
    check(removed == 2, f"keep_versions=1 prunes 2 of 3 superseded rows: {removed}")
    remaining = {b.belief_id for b in c.list_beliefs(limit=100, include_superseded=True)}
    check(remaining == {"b-X-2", "b-X-active"} and c.count_beliefs() == 1,
          f"newest superseded + ACTIVE belief kept, count unchanged: {sorted(remaining)}")
    check(c.prune_superseded_beliefs(keep_versions=1) == 0, "belief prune idempotent")

    removed = c.prune_goal_plans(goal_id="g1", keep_latest=1)
    check(removed == 2 and [p["plan_version"] for p in c.list_goal_plans("g1")] == [3]
          and c.latest_goal_plan("g1")["plan_version"] == 3,
          "goal-plan history bounded; latest plan never pruned; latest intact")

    fail_closed = 0
    for fn, kw in ((c.prune_superseded_beliefs, {"keep_versions": 0}),
                   (c.prune_goal_plans, {"keep_latest": 0}),
                   (c.prune_goal_plans, {"batch_size": 10 ** 9})):
        try:
            fn(**kw)
            check(False, f"expected fail-closed for {kw}")
        except ValueError:
            fail_closed += 1
    check(fail_closed == 3, "fail closed: keep_versions=0 / keep_latest=0 / huge batch")
    c.close()


# ----------------------------------------------------------------- C+D

def section_cd_observability_and_cli() -> None:
    print("\n[C+D] observability + CLI: bounded memory.pruned events, dry-run, exit 1")
    db = _tmp("cd")
    _seed_memory(db, [_ep("ep-0", 0, reflection=True), _ep("ep-1", 10),
                      _ep("ep-2", 20)])
    _seed_beliefs(db)

    rc, out = _cli(["memory", "prune", "--older-than", _iso_plus(T0, 5),
                    "--db", db, "--dry-run"])
    conn = sqlite3.connect(db)
    no_event = conn.execute("SELECT COUNT(*) FROM audit_events "
                            "WHERE kind='memory.pruned'").fetchone()[0] == 0
    conn.close()
    check(rc == 0 and "would remove 1 episode(s)" in out and no_event,
          "CLI dry-run rc=0, reports 1 episode, emits NO event (mutation-free)")

    rc, out = _cli(["memory", "prune", "--older-than", _iso_plus(T0, 5),
                    "--db", db])
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT detail FROM audit_events WHERE kind='memory.pruned'").fetchone()
    detail = json.loads(row[0])
    conn.close()
    check(rc == 0 and "removed 1 episode(s)" in out
          and detail["episodes"] == 1 and detail["reflections"] == 1
          and detail["scope"] == "memory.episodes" and detail["dry_run"] is False,
          f"CLI real prune emits bounded event: {detail}")
    check(set(detail) <= {"scope", "episodes", "reflections", "beliefs",
                          "goal_plans", "cutoff", "limit", "dry_run"},
          "event detail bounded (counts + criteria only, never content)")

    rc, out = _cli(["memory", "prune", "--max-episodes", "0", "--db", db])
    check(rc == 1, "CLI invalid --max-episodes 0 -> exit 1 (fail closed)")
    rc, out = _cli(["cognition", "prune-plans", "--keep-latest", "0", "--db", db])
    check(rc == 1, "CLI invalid --keep-latest 0 -> exit 1 (fail closed)")

    rc, out = _cli(["cognition", "prune-superseded", "--db", db])
    c = SQLiteCognitiveStore(db)
    active_ok = c.count_beliefs() == 1
    c.close()
    check(rc == 0 and "superseded belief(s)" in out and active_ok,
          "CLI prune-superseded rc=0; ACTIVE belief survives")

    rc, out = _cli(["cognition", "prune-plans", "--goal", "g1",
                    "--keep-latest", "2", "--db", db, "--json"])
    obj = json.loads(out)
    rc2, out2 = _cli(["memory", "prune", "--older-than", _iso_plus(T0, 5),
                      "--db", db, "--json"])
    obj2 = json.loads(out2)
    check(rc == 0 and obj["goal_plans"] == 1 and obj["limit"] == 2
          and rc2 == 0 and obj2["episodes"] == 0 and obj2["dry_run"] is False,
          f"CLI --json deterministic (plans {obj['goal_plans']}, then 0 episodes)")


# --------------------------------------------------------------------- E

def section_e_adversarial() -> None:
    print("\n[E] adversarial: forged inputs fail closed; scheduler authority untouched")
    db = _tmp("e")
    _seed_memory(db, [_ep("ep-0", 0), _ep("ep-1", 10), _ep("ep-2", 20)])
    _seed_scheduler_authority(db)
    authority = ("scheduler_work", "scheduler_instances", "scheduler_config",
                 "scheduler_goal_weights", "scheduler_goal_state",
                 "scheduler_goal_reservations", "scheduler_goal_ceilings",
                 "scheduler_events")
    before = _dump(db, authority)

    m = SQLiteMemoryStore(db)
    removed = m.prune(older_than=_iso_plus(T0, 100000))
    check(removed == 3 and _dump(db, authority) == before,
          "delete-ALL memory prune leaves scheduler authority byte-identical")
    m.close()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, goal_id,"
        " task_id, work_id, step_index, event_type, reason, success, detail,"
        " schema_version) VALUES ('se-forged', ?, 'evil', NULL, NULL, NULL, NULL,"
        " NULL, 'work.completed', 'forged', 1, '{\"n\": 999999999}', 1)",
        (_iso_plus(T0, 1),))
    conn.commit()
    conn.close()

    m = SQLiteMemoryStore(db)
    try:
        m.prune(older_than=_iso_plus(T0, 5), keep_importance=2.0)
        check(False, "forged/oversized importance must fail closed")
    except ValueError:
        check(True, "oversized keep_importance fails closed")
    m.close()
    conn = sqlite3.connect(db)
    check(conn.execute(
        "SELECT COUNT(*) FROM scheduler_events WHERE scheduler_id='evil'"
    ).fetchone()[0] == 1, "forged telemetry rows untouched by prune")
    conn.close()

    # delete-all memory leaves scheduler BEHAVIOR identical: a fresh claim on
    # the queued row still succeeds exactly as before
    storage = SQLiteStorage(db)
    claimed = storage.claim_next("sched-1", "worker-2", lease_seconds=30,
                                 now=_iso_plus(T0, 200))
    check(claimed is not None and claimed.work_id == "sw-2"
          and claimed.status.value == "running" and claimed.worker_id == "worker-2",
          "scheduler claim works identically after delete-all memory")
    storage.close()

    # malformed episode ids are opaque and cannot influence pruning
    db2 = _tmp("e2")
    m2 = SQLiteMemoryStore(db2)
    evil = "ep'; DROP TABLE episodic_memories;--"
    m2.record_episode(_ep("good", 0))
    m2.record_episode(_ep(evil, 10))
    removed = m2.prune(older_than=_iso_plus(T0, 5))
    check(removed == 1 and m2.get_episode(evil) is not None,
          "malformed id treated as opaque; injection has no effect")
    m2.close()


# --------------------------------------------------------------------- F

def section_f_consolidation_fed_beliefs() -> None:
    print("\n[F] consolidation-fed beliefs: provenance, idempotent, informational")
    db = _tmp("f")
    consol = ConsolidationRecord(
        consolidation_id="consol-1", source_episode_ids=["ep-0", "ep-1"],
        category="lesson", merged_lesson="verify before mutating", count=2,
        importance=0.6, created_at=_iso_plus(T0, 60))
    _seed_memory(db, [_ep("ep-0", 0), _ep("ep-1", 10)], consolidation=consol)

    facade = CognitiveState(memory=SQLiteMemoryStore(db),
                            cognition=SQLiteCognitiveStore(db))
    base = facade.refresh_from_memory(limit=20)          # flag off (default)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "verify before mutating" in b.statement]
    check(base >= 0 and lifted == [],
          "include_consolidations=False preserves existing behavior (no lift)")

    new_count = facade.refresh_from_memory(limit=20, include_consolidations=True)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "verify before mutating" in b.statement]
    check(new_count >= 1 and len(lifted) == 1
          and lifted[0].category == "procedural"
          and lifted[0].statement == "verify before mutating",
          "merged lesson lifted into ONE procedural belief with the lesson")
    b = lifted[0]
    check(b.provenance["episode_ids"] == ["ep-0", "ep-1"]
          and b.provenance["consolidation_ids"] == ["consol-1"],
          "complete provenance (source episodes + consolidation id)")
    check(b.confidence == 0.8, f"deterministic confidence from importance: {b.confidence}")

    again = facade.refresh_from_memory(limit=20, include_consolidations=True)
    check(again == 0 and len([x for x in facade.cognition.list_beliefs(limit=1000)
                              if "verify before mutating" in x.statement]) == 1,
          "consolidation lift is idempotent (no duplicates)")
    check(facade.cognition.list_preferences(limit=100) == []
          and facade.cognition.list_environment_facts(limit=100) == [],
          "consolidation feed is informational only (no preferences/facts)")
    facade.cognition.close()


# --------------------------------------------------------------------- G

def section_g_restart_reopen() -> None:
    print("\n[G] restart/reopen: prune survives a fresh store instance")
    db = _tmp("g")
    consol = ConsolidationRecord(
        consolidation_id="consol-1", source_episode_ids=["ep-0"],
        category="lesson", merged_lesson="persistent lesson", count=1,
        importance=0.5, created_at=_iso_plus(T0, 60))
    _seed_memory(db, [_ep("ep-0", 0), _ep("ep-1", 10), _ep("ep-2", 20)],
                 consolidation=consol)

    m = SQLiteMemoryStore(db)
    m.prune(older_than=_iso_plus(T0, 15))
    m.close()

    # fresh instance == fresh process equivalent
    m2 = SQLiteMemoryStore(db)
    check([e.episode_id for e in m2.list_recent(limit=10)] == ["ep-2"]
          and m2.list_recent_reflections(limit=10) == [],
          "reopened store sees pruned episodes + reflections gone")
    check([c.consolidation_id for c in m2.list_consolidations()] == ["consol-1"]
          and m2.prune(older_than=_iso_plus(T0, 15)) == 0,
          "reopened store: consolidation intact, repeated prune deterministic")

    facade = CognitiveState(memory=m2, cognition=SQLiteCognitiveStore(db))
    new_count = facade.refresh_from_memory(limit=20, include_consolidations=True)
    lifted = [b for b in facade.cognition.list_beliefs(limit=1000)
              if "persistent lesson" in b.statement]
    check(new_count >= 1 and len(lifted) == 1,
          "re-derivation after pruning works (catch-up deterministic)")
    facade.cognition.close()
    m2.close()


def main() -> int:
    print("ADR-014 addendum demo: cognitive-state archival")
    print(f"  fixed timeline T0 = {T0} (no wall clock in any assertion)")
    section_a_memory_prune()
    section_b_cognitive_prune()
    section_cd_observability_and_cli()
    section_e_adversarial()
    section_f_consolidation_fed_beliefs()
    section_g_restart_reopen()
    print("\n" + "=" * 78)
    print(f"ADR-014 demo PASSED ({CHECKS} checks) - archival policy verified")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
