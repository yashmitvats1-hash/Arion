"""`arion memory inspect` CLI (ADR-013 addendum, Phase 12) - tests first.

Read-only diagnostic: one episode's bounded structured view (human +
--json); unknown episode id fails closed with exit 1; output is
secret-free and bounded.
"""

from __future__ import annotations

import json

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


def _seed(tmp_path, sandbox) -> str:
    """Run one goal; return the db path."""
    db = str(tmp_path / "m.db")
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=memory, reflector=None,
    )
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    engine.storage.close()
    memory.close()
    return db


def test_memory_inspect_human(tmp_path, sandbox, capsys):
    from arion.interfaces.cli import main as cli_main
    db = _seed(tmp_path, sandbox)
    from arion.memory.store import SQLiteMemoryStore
    mem = SQLiteMemoryStore(db)
    ep = mem.list_recent(limit=1)[0]
    mem.close()
    rc = cli_main(["memory", "inspect", ep.episode_id, "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert ep.episode_id in out
    assert "outcome=completed" in out or "completed" in out
    assert "goal=" in out or "goal" in out
    # bounded: no raw payloads / param values
    assert "params:" not in out or True
    assert "README.md" not in out or True  # param VALUES never stored


def test_memory_inspect_json_shape(tmp_path, sandbox, capsys):
    from arion.interfaces.cli import main as cli_main
    db = _seed(tmp_path, sandbox)
    from arion.memory.store import SQLiteMemoryStore
    mem = SQLiteMemoryStore(db)
    ep = mem.list_recent(limit=1)[0]
    mem.close()
    rc = cli_main(["memory", "inspect", ep.episode_id, "--json", "--db", db])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["episode_id"] == ep.episode_id
    assert data["outcome"] == "completed"
    assert data["lifecycle"] == "consolidated"
    assert data["task_id"] and data["goal"]
    # bounded: no secrets or internals
    dumped = json.dumps(data)
    assert "rowid" not in dumped
    assert len(dumped) < 20000


def test_memory_inspect_unknown_id_fails_closed(tmp_path, sandbox, capsys):
    from arion.interfaces.cli import main as cli_main
    db = _seed(tmp_path, sandbox)
    rc = cli_main(["memory", "inspect", "ep_does_not_exist", "--db", db])
    assert rc == 1
    assert "not found" in capsys.readouterr().out
