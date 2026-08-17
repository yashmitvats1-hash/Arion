"""Prune CLI + observability (ADR-014 addendum, Phase C) - tests first.

- `arion memory prune [--older-than TS] [--max-episodes N]
  [--keep-importance F] [--batch-size N] [--dry-run] [--json]`
- `arion cognition prune-superseded [--older-than TS] [--keep-versions N]
  [--batch-size N] [--dry-run] [--json]`
- `arion cognition prune-plans [--goal G] [--keep-latest N]
  [--batch-size N] [--dry-run] [--json]`

Exit 0 on success (including dry-run), 1 on invalid input (fail closed);
deterministic output; --dry-run never mutates (DB byte-identical, and NO
`memory.pruned` event is emitted); real prunes emit a bounded
`memory.pruned` audit event (counts + criteria only, never content).
"""

from __future__ import annotations

import json
import sqlite3

from arion.cognition.models import Belief
from arion.cognition.store import SQLiteCognitiveStore
from arion.interfaces.cli import main as cli_main
from arion.memory.models import Episode, Reflection
from arion.memory.store import SQLiteMemoryStore

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _seed_memory(db, n=3, start=0, step=10):
    """Seed n episodes (importance 0.5) + a reflection on the second."""
    store = SQLiteMemoryStore(db)
    for i in range(n):
        created = _iso_plus(T0, start + i * step)
        store.record_episode(Episode(
            episode_id=f"ep-{i}", task_id=f"t-{i}", goal_id="g", goal=f"goal {i}",
            outcome="completed", importance=0.5,
            created_at=created, updated_at=created,
            reflection_id="refl-1" if i == 1 else None,
        ))
    store.record_reflection(Reflection(
        reflection_id="refl-1", episode_id="ep-1",
        what_happened="x", what_worked="", what_failed="", why="",
        lesson="lesson", recommendation="", confidence="medium",
        importance=0.5, created_at=_iso_plus(T0, start + step),
    ))
    store.close()


def _seed_beliefs(db):
    store = SQLiteCognitiveStore(db)
    for i, (bid, created, superseded) in enumerate([
        ("b-0", _iso_plus(T0, 0), _iso_plus(T0, 5)),
        ("b-1", _iso_plus(T0, 10), _iso_plus(T0, 15)),
        ("b-2", _iso_plus(T0, 20), _iso_plus(T0, 25)),
        ("b-active", _iso_plus(T0, 30), None),
    ]):
        store.record_belief(Belief(
            belief_id=bid, category="semantic", statement="stmt A",
            confidence=0.5 + i * 0.05, importance=0.5, version=i + 1,
            provenance={"episode_ids": [f"ep-{i}"]}, source="deterministic",
            created_at=created, updated_at=superseded or created,
            superseded_at=superseded,
        ))
    store.close()


def _seed_plans(db):
    store = SQLiteCognitiveStore(db)
    for v in range(1, 5):
        store.record_goal_plan("g1", v, "direct", [{"v": v}], reason="r")
    store.close()


# Tables inside the prune boundary: the four stores pruning may touch plus
# the audit trail (proves dry-runs emit no event). environment_facts is
# deliberately excluded: the world monitor refreshes its updated_at on ANY
# engine construction (pre-existing behavior, unrelated to pruning).
_PRUNE_BOUNDARY = ("episodic_memories", "reflections", "beliefs",
                   "goal_plans", "audit_events")


def _dump_all(db, tables=_PRUNE_BOUNDARY) -> list[tuple]:
    """Deterministic dump of the prune-boundary tables (rowid order)."""
    conn = sqlite3.connect(db)
    try:
        out = []
        for t in tables:
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            out.append((t, rows))
        return out
    finally:
        conn.close()


def _pruned_events(db):
    """memory.pruned audit events persisted in the DB."""
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT kind, detail, ts FROM audit_events "
            "WHERE kind='memory.pruned' ORDER BY rowid").fetchall()
        return [(k, json.loads(d)) for k, d, _ in rows]
    finally:
        conn.close()


# ------------------------------------------------------------ memory prune

def test_memory_prune_cli_dry_run_mutation_free_and_deterministic(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed_memory(db)
    cli_main(["memory", "stats", "--db", db])          # warm-up (startup writes)
    capsys.readouterr()
    before = _dump_all(db)

    rc = cli_main(["memory", "prune", "--older-than", _iso_plus(T0, 15),
                   "--db", db, "--dry-run"])
    out1 = capsys.readouterr().out
    assert rc == 0
    assert "2 episode(s)" in out1 and "dry-run" in out1
    assert "reflection" not in out1   # dry-run reports episode candidates only

    rc2 = cli_main(["memory", "prune", "--older-than", _iso_plus(T0, 15),
                    "--db", db, "--dry-run"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out1 == out2                               # deterministic output

    assert _dump_all(db) == before                    # byte-identical, no event
    assert _pruned_events(db) == []                   # dry-run emits NO event


def test_memory_prune_cli_removes_and_emits_bounded_event(tmp_path, capsys):
    db = str(tmp_path / "m2.db")
    _seed_memory(db)
    rc = cli_main(["memory", "prune", "--older-than", _iso_plus(T0, 15),
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 episode(s)" in out and "1 reflection(s)" in out
    store = SQLiteMemoryStore(db)
    assert [e.episode_id for e in store.list_recent(limit=100)] == ["ep-2"]
    assert store.list_recent_reflections(limit=100) == []
    store.close()

    events = _pruned_events(db)
    assert len(events) == 1
    kind, detail = events[0]
    assert kind == "memory.pruned"
    assert detail["scope"] == "memory.episodes"
    assert detail["episodes"] == 2
    assert detail["reflections"] == 1
    assert detail["beliefs"] == 0 and detail["goal_plans"] == 0
    assert detail["cutoff"] == _iso_plus(T0, 15)
    assert detail["limit"] is None
    assert detail["dry_run"] is False


def test_memory_prune_cli_json_deterministic(tmp_path, capsys):
    db = str(tmp_path / "m3.db")
    _seed_memory(db)
    cli_main(["memory", "prune", "--max-episodes", "1",
              "--db", db, "--json"])
    obj1 = json.loads(capsys.readouterr().out)
    assert obj1 == {
        "scope": "memory.episodes", "episodes": 2, "reflections": 1,
        "beliefs": 0, "goal_plans": 0,
        "cutoff": None, "limit": 1, "dry_run": False,
    }
    # second run: nothing left to prune, output still deterministic
    rc = cli_main(["memory", "prune", "--max-episodes", "1",
                   "--db", db, "--json"])
    obj2 = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert obj2 == {
        "scope": "memory.episodes", "episodes": 0, "reflections": 0,
        "beliefs": 0, "goal_plans": 0,
        "cutoff": None, "limit": 1, "dry_run": False,
    }


def test_memory_prune_cli_invalid_input_exit_1_no_mutation(tmp_path, capsys):
    db = str(tmp_path / "m4.db")
    _seed_memory(db)
    cli_main(["memory", "stats", "--db", db])
    capsys.readouterr()
    before = _dump_all(db)

    for argv in (
        ["memory", "prune", "--max-episodes", "0", "--db", db],
        ["memory", "prune", "--older-than", "not-a-date", "--db", db],
        ["memory", "prune", "--keep-importance", "2.0", "--db", db],
        ["memory", "prune", "--batch-size", "99999", "--db", db],
        ["memory", "prune", "--db", db],            # no criterion
    ):
        rc = cli_main(argv)
        assert rc == 1, argv
        capsys.readouterr()
    assert _dump_all(db) == before                  # nothing mutated
    assert _pruned_events(db) == []


# -------------------------------------------- cognition prune-superseded

def test_cognition_prune_superseded_cli_removes_and_emits(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    _seed_beliefs(db)
    rc = cli_main(["cognition", "prune-superseded", "--older-than",
                   _iso_plus(T0, 20), "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 superseded belief(s)" in out
    store = SQLiteCognitiveStore(db)
    remaining = {b.belief_id for b in
                 store.list_beliefs(limit=100, include_superseded=True)}
    assert remaining == {"b-2", "b-active"}         # active + newest superseded
    assert store.count_beliefs() == 1               # active count unchanged
    store.close()

    events = _pruned_events(db)
    assert len(events) == 1
    _, detail = events[0]
    assert detail["scope"] == "cognition.beliefs"
    assert detail["beliefs"] == 2
    assert detail["cutoff"] == _iso_plus(T0, 20)
    assert detail["limit"] == 1                     # keep_versions
    assert detail["dry_run"] is False


def test_cognition_prune_superseded_cli_dry_run_mutation_free(tmp_path, capsys):
    db = str(tmp_path / "c2.db")
    _seed_beliefs(db)
    cli_main(["cognition", "beliefs", "--db", db])   # warm-up
    capsys.readouterr()
    before = _dump_all(db)
    rc = cli_main(["cognition", "prune-superseded", "--older-than",
                   _iso_plus(T0, 20), "--db", db, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 superseded belief(s)" in out and "dry-run" in out
    assert _dump_all(db) == before
    assert _pruned_events(db) == []


def test_cognition_prune_superseded_cli_invalid_exit_1(tmp_path, capsys):
    db = str(tmp_path / "c3.db")
    _seed_beliefs(db)
    for argv in (
        ["cognition", "prune-superseded", "--keep-versions", "0", "--db", db],
        ["cognition", "prune-superseded", "--older-than", "junk", "--db", db],
    ):
        rc = cli_main(argv)
        assert rc == 1, argv
        capsys.readouterr()


# --------------------------------------------------- cognition prune-plans

def test_cognition_prune_plans_cli_removes_and_emits(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    _seed_plans(db)
    rc = cli_main(["cognition", "prune-plans", "--goal", "g1",
                   "--keep-latest", "2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 historical plan(s)" in out
    store = SQLiteCognitiveStore(db)
    assert [p["plan_version"] for p in store.list_goal_plans("g1")] == [3, 4]
    assert store.latest_goal_plan("g1")["plan_version"] == 4
    store.close()

    events = _pruned_events(db)
    assert len(events) == 1
    _, detail = events[0]
    assert detail["scope"] == "cognition.goal_plans"
    assert detail["goal_plans"] == 2
    assert detail["goal_id"] == "g1"
    assert detail["limit"] == 2                     # keep_latest
    assert detail["dry_run"] is False


def test_cognition_prune_plans_cli_dry_run_mutation_free(tmp_path, capsys):
    db = str(tmp_path / "p2.db")
    _seed_plans(db)
    cli_main(["cognition", "goals", "g1", "--db", db])  # warm-up
    capsys.readouterr()
    before = _dump_all(db)
    rc = cli_main(["cognition", "prune-plans", "--keep-latest", "2",
                   "--db", db, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 historical plan(s)" in out and "dry-run" in out
    assert _dump_all(db) == before
    assert _pruned_events(db) == []


def test_cognition_prune_plans_cli_invalid_exit_1(tmp_path, capsys):
    db = str(tmp_path / "p3.db")
    _seed_plans(db)
    for argv in (
        ["cognition", "prune-plans", "--keep-latest", "0", "--db", db],
        ["cognition", "prune-plans", "--batch-size", "-5", "--db", db],
    ):
        rc = cli_main(argv)
        assert rc == 1, argv
        capsys.readouterr()


# ----------------------------------------------------- event boundedness

def test_prune_event_detail_bounded_no_content(tmp_path, capsys):
    """memory.pruned detail carries counts/criteria ONLY - never content."""
    db = str(tmp_path / "e.db")
    _seed_memory(db, n=4, start=0, step=5)
    _seed_beliefs(db)
    _seed_plans(db)
    assert cli_main(["memory", "prune", "--older-than", _iso_plus(T0, 8),
                     "--db", db]) == 0
    capsys.readouterr()
    events = _pruned_events(db)
    assert len(events) == 1
    _, detail = events[0]
    allowed = {"scope", "episodes", "reflections", "beliefs", "goal_plans",
               "cutoff", "limit", "dry_run", "goal_id"}
    assert set(detail) <= allowed
    blob = json.dumps(detail)
    for secret in ("ep-0", "ep-1", "goal 0", "lesson", "b-0", "stmt A", "why-1"):
        assert secret not in blob                    # no content leaks
