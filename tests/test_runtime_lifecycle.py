"""ADR-032: explicit runtime lifecycle ownership and health contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.runtime.lifecycle import (
    HealthStatus,
    LifecycleError,
    LifecycleState,
    ResourceLifecycle,
)
from arion.state.store import SQLiteStorage


class _Resource:
    def __init__(self, name: str, closed: list[str], *, fail: bool = False):
        self.name = name
        self.closed = closed
        self.fail = fail
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self.closed.append(self.name)
        if self.fail:
            raise RuntimeError(f"cannot close {self.name}")


def test_owned_resources_close_once_in_reverse_construction_order() -> None:
    closed: list[str] = []
    lifecycle = ResourceLifecycle()
    first = _Resource("first", closed)
    second = _Resource("second", closed)
    lifecycle.register("first", first)
    lifecycle.register("second", second)

    report = lifecycle.shutdown()
    repeated = lifecycle.shutdown()

    assert closed == ["second", "first"]
    assert first.close_count == second.close_count == 1
    assert report.state is LifecycleState.STOPPED
    assert report.status is HealthStatus.STOPPED
    assert repeated == report


def test_cleanup_continues_and_reports_each_component_after_failure() -> None:
    closed: list[str] = []
    lifecycle = ResourceLifecycle()
    lifecycle.register("state", _Resource("state", closed))
    lifecycle.register("memory", _Resource("memory", closed, fail=True))
    lifecycle.register("cognition", _Resource("cognition", closed))

    report = lifecycle.shutdown()

    assert closed == ["cognition", "memory", "state"]
    assert report.state is LifecycleState.FAILED
    assert report.status is HealthStatus.UNHEALTHY
    components = {component.name: component for component in report.components}
    assert components["memory"].status is HealthStatus.UNHEALTHY
    assert "cannot close memory" in components["memory"].detail
    assert components["state"].status is HealthStatus.STOPPED
    assert components["cognition"].status is HealthStatus.STOPPED


def test_duplicate_resource_name_or_identity_is_rejected() -> None:
    lifecycle = ResourceLifecycle()
    resource = _Resource("state", [])
    lifecycle.register("state", resource)

    with pytest.raises(LifecycleError, match="name"):
        lifecycle.register("state", _Resource("other", []))
    with pytest.raises(LifecycleError, match="already owned"):
        lifecycle.register("alias", resource)


def test_build_engine_owns_and_closes_all_composed_stores(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    engine = build_engine(tmp_path / "arion.db", sandbox)

    running = engine.health()
    assert running.state is LifecycleState.RUNNING
    assert running.status is HealthStatus.HEALTHY
    assert {component.name for component in running.components} == {
        "orchestration.scheduler",
        "state.storage",
        "memory.store",
        "cognition.store",
    }

    engine.shutdown()
    stopped = engine.health()
    engine.close()  # compatibility alias and repeated cleanup are both safe

    assert stopped.state is LifecycleState.STOPPED
    assert stopped.status is HealthStatus.STOPPED
    assert all(component.status is HealthStatus.STOPPED for component in stopped.components)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        engine.storage.list_tasks()


def test_engine_context_manager_closes_bootstrap_resources(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    with build_engine(tmp_path / "arion.db", sandbox) as engine:
        assert engine.health().status is HealthStatus.HEALTHY

    assert engine.health().status is HealthStatus.STOPPED


def test_manually_injected_dependencies_remain_borrowed(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    storage = SQLiteStorage(tmp_path / "arion.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = DeterministicPlanner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
    )

    engine.shutdown()

    # ArionEngine did not construct this store, so its historical borrowed
    # ownership behavior remains intact.
    assert storage.list_tasks() == []
    storage.close()


def test_partial_bootstrap_failure_closes_resources_already_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arion.bootstrap as bootstrap

    instances: list[SQLiteStorage] = []

    class TrackingStorage(SQLiteStorage):
        def __init__(self, path):
            super().__init__(path)
            self.was_closed = False
            instances.append(self)

        def close(self) -> None:
            self.was_closed = True
            super().close()

    monkeypatch.setattr(bootstrap, "SQLiteStorage", TrackingStorage)

    with pytest.raises(CapabilityError, match="sandbox root does not exist"):
        bootstrap.build_engine(tmp_path / "arion.db", tmp_path / "missing")

    assert len(instances) == 1
    assert instances[0].was_closed is True
