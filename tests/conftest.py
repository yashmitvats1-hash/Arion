"""Shared fixtures: a sandboxed repo, a fresh engine, and event capture.

Every test runs with NO LLM (ADR-008): DeterministicPlanner + router, real
SQLite in a tmp dir, and a real sandboxed filesystem.read capability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.bootstrap import build_engine  # noqa: E402
from arion.capabilities.filesystem import FilesystemReadCapability  # noqa: E402
from arion.capabilities.registry import CapabilityRegistry  # noqa: E402
from arion.intelligence.planner import DeterministicPlanner  # noqa: E402
from arion.intelligence.router import DeterministicRouter  # noqa: E402
from arion.observability.events import EventLogger  # noqa: E402
from arion.orchestration.engine import ArionEngine  # noqa: E402
from arion.state.store import SQLiteStorage  # noqa: E402


class MemorySink:
    """Captures emitted events in memory for assertions."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def by_kind(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A fake repository with known files, used as the capability sandbox."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# Test Repo\n\nA sandboxed repo for Arion tests.\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello arion\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "design.md").write_text("# Design\n\nRead-only capability spec.\n", encoding="utf-8")
    # a symlink escaping the sandbox, to prove the boundary holds
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (root / "escape.txt").symlink_to(outside)
    except OSError:
        pass  # symlink unsupported on this platform: boundary test will skip
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "arion.db")


@pytest.fixture
def storage(db_path: str) -> SQLiteStorage:
    return SQLiteStorage(db_path)


@pytest.fixture
def registry(sandbox: Path) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(sandbox))
    return reg


@pytest.fixture
def sink() -> MemorySink:
    return MemorySink()


@pytest.fixture
def engine(storage: SQLiteStorage, registry: CapabilityRegistry, sink: MemorySink) -> ArionEngine:
    planner = DeterministicPlanner()
    router = DeterministicRouter(planner)
    events = EventLogger(sinks=[storage, sink])
    return ArionEngine(storage=storage, registry=registry, planner=planner, router=router, events=events)


@pytest.fixture
def factory():
    """A factory that builds an engine the same way bootstrap does."""

    def _make(db: str, root: Path, sink: MemorySink | None = None, policy=None):
        storage = SQLiteStorage(db)
        reg = CapabilityRegistry()
        reg.register(FilesystemReadCapability(root))
        planner = DeterministicPlanner()
        router = DeterministicRouter(planner)
        events = EventLogger(sinks=[storage, sink] if sink else [storage])
        return ArionEngine(storage=storage, registry=reg, planner=planner, router=router, events=events, policy=policy)

    return _make


@pytest.fixture
def fresh_engine():
    """A factory that builds an engine on the same DB file from scratch.

    Simulates a fresh process: new SQLite connections, new objects, same DB.
    """

    def _make(db_path: str, sandbox_root: Path, sink: MemorySink | None = None):
        storage = SQLiteStorage(db_path)
        reg = CapabilityRegistry()
        reg.register(FilesystemReadCapability(sandbox_root))
        planner = DeterministicPlanner()
        router = DeterministicRouter(planner)
        events = EventLogger(sinks=[storage, sink] if sink else [storage])
        return ArionEngine(storage=storage, registry=reg, planner=planner, router=router, events=events)

    return _make

