"""Bootstrap: wiring of layers (composition root).

Central place where Storage, CapabilityRegistry, Planner, Router, Events, the
Policy and the Engine are assembled. New capabilities, policies and interfaces
are wired here without touching layer internals.
"""

from __future__ import annotations

from pathlib import Path

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger, JsonlFileSink
from arion.orchestration.authz import (
    ApprovalHandler,
    PermissionPolicy,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.store import SQLiteStorage


def build_engine(
    db_path: str | Path,
    sandbox_root: str | Path,
    jsonl_log: str | Path | None = None,
    policy: PermissionPolicy | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> ArionEngine:
    storage = SQLiteStorage(db_path)

    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox_root))

    planner = DeterministicPlanner()
    router = DeterministicRouter(planner)

    events = EventLogger()
    events.add_sink(storage)  # Storage implements EventSink.append_event
    if jsonl_log:
        events.add_sink(JsonlFileSink(jsonl_log))

    # Fail-closed by default: the filesystem resource kind gets an explicit
    # boundary (relative, non-traversal paths - the capability enforces the
    # real sandbox root). Any other resource kind is DENIED until configured.
    if policy is None:
        policy = ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()})

    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=events,
        policy=policy,
        approval_handler=approval_handler,
    )
