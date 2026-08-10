"""CLI lock inspection tests (ADR-021, Phase K).

`arion locks list|show <lock_id>|reclaim <lock_id>` with --json, using the
domain/store interfaces only. Output bounded + secret-free; reclaim fails
closed (active locks cannot be reclaimed; unknown ids error).
"""

import json

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.interfaces.cli import main as cli_main
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import PendingApprovalHandler, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.locks import MutationLockError
from arion.state.models import PlanStep, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=DeterministicPlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
    )
    return engine


def _seed(tmp_path, expired=False):
    """Create one lock (optionally expired) and return (db, sandbox, lock_id)."""
    sb = tmp_path / "csandbox"
    sb.mkdir(parents=True, exist_ok=True)
    db = str(tmp_path / "arion.db")
    engine = _engine(db, sb)
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).isoformat()
    lock = engine.mutation_lock_store.acquire(
        FS, "notes.txt", "filesystem.write", "write", "proc-cli",
        lease_seconds=-1 if expired else 300, now=now)
    if expired:
        # force expiry: rewrite expires_at in the past
        engine.mutation_lock_store.release(lock.lock_id, "proc-cli")
        from arion.state.locks import MutationLock
        lock2 = MutationLock(lock_id=lock.lock_id, resource_kind=FS, resource="notes.txt",
                             capability="filesystem.write", action="write", owner_id="proc-cli",
                             acquired_at=now, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        engine.mutation_lock_store._conn.execute(
            "INSERT INTO mutation_locks (lock_id, resource_kind, resource, capability, action,"
            " owner_id, acquired_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (lock2.lock_id, lock2.resource_kind, lock2.resource, lock2.capability,
             lock2.action, lock2.owner_id, lock2.acquired_at, lock2.expires_at))
        engine.mutation_lock_store._conn.commit()
    engine.storage.close()
    return db, sb, lock.lock_id


def _run(argv, capsys):
    rc = cli_main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_cli_locks_list_and_show(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, lock_id = _seed(tmp_path)

    rc, out = _run(["locks", "list", "--json", "--db", db], capsys)
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["resource"] == "notes.txt"
    assert data[0]["owner_id"] == "proc-cli"
    assert "content" not in json.dumps(data)  # bounded, secret-free

    rc, out = _run(["locks", "show", lock_id, "--json", "--db", db], capsys)
    d = json.loads(out)
    assert d["lock_id"] == lock_id and d["capability"] == "filesystem.write"

    rc, out = _run(["locks", "list", "--db", db], capsys)
    assert lock_id in out


def test_cli_locks_reclaim_active_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, lock_id = _seed(tmp_path)
    rc, out = _run(["locks", "reclaim", lock_id, "--db", db], capsys)
    assert rc == 1
    assert "active" in out.lower()
    # the lock is untouched
    engine = _engine(db, sb)
    assert engine.mutation_lock_store.get(lock_id) is not None
    engine.storage.close()


def test_cli_locks_reclaim_expired(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, lock_id = _seed(tmp_path, expired=True)
    rc, out = _run(["locks", "reclaim", lock_id, "--json", "--db", db], capsys)
    assert rc == 0
    d = json.loads(out)
    assert d["lock_id"] == lock_id and d["status"] == "reclaimed"
    engine = _engine(db, sb)
    assert engine.mutation_lock_store.get(lock_id) is None
    engine.storage.close()


def test_cli_locks_unknown_id(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, sb, lock_id = _seed(tmp_path)
    rc, out = _run(["locks", "show", "lock_nope", "--db", db], capsys)
    assert rc == 1
    rc, out = _run(["locks", "reclaim", "lock_nope", "--db", db], capsys)
    assert rc == 1
    assert "unknown" in out.lower() or "not found" in out.lower()
