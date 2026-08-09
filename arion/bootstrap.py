"""Bootstrap: wiring of layers (composition root).

Central place where Storage, CapabilityRegistry, Planner, Router, Events, the
Policy and the Engine are assembled. New capabilities, policies and interfaces
are wired here without touching layer internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.write import FilesystemWriteCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.http import HttpGetCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.deriver import DeterministicBeliefDeriver
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger, JsonlFileSink
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
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
    planner: Any | None = None,
    router: Any | None = None,
    memory: bool = True,
    cognition: bool = True,
) -> ArionEngine:
    storage = SQLiteStorage(db_path)

    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox_root))
    # filesystem.write (ADR-019) and filesystem.append (ADR-020) are
    # REGISTRY-DISCOVERABLE but DENIED by the default policy below
    # (allowed_scopes has no filesystem:write): no mutation without explicit
    # operator authorization. Fail closed.
    registry.register(FilesystemWriteCapability(sandbox_root))
    registry.register(FilesystemAppendCapability(sandbox_root))
    registry.register(GitLogCapability(sandbox_root))
    # http.get is DISCOVERABLE by default but DENIED until an operator configures
    # a 'url' resource boundary (fail closed): no allowlist = no network access.
    registry.register(HttpGetCapability())

    events = EventLogger()
    events.add_sink(storage)  # Storage implements EventSink.append_event
    if jsonl_log:
        events.add_sink(JsonlFileSink(jsonl_log))

    # Planners (DeterministicPlanner, RealModelPlanner, future planners) are
    # interchangeable; the router follows the same ModelRouter abstraction.
    if planner is None:
        planner = DeterministicPlanner()
    if router is None:
        router = DeterministicRouter(planner)

    # Fail-closed by default: the filesystem resource kind gets an explicit
    # boundary (relative, non-traversal paths - the capability enforces the
    # real sandbox root). Any other resource kind is DENIED until configured.
    # git.log (read-only history inspection) is allowed under its own scope.
    if policy is None:
        policy = ResourcePolicy(
            allowed_scopes={"filesystem:read", "git:read"},
            # NOTE: no 'url' boundary by default -> http.get is DENIED (fail
            # closed). Configure UrlBoundary to enable network access.
            boundaries={"filesystem:path": RelativePathBoundary()},
        )

    # Persistent cognitive memory (ADR-012): same DB file, structured episodes
    # + reflections. Memory is informational - never an authorization mechanism.
    memory_store = SQLiteMemoryStore(db_path) if memory else None

    # Cognitive State / World Model v1 (ADR-014): semantic beliefs, procedural
    # knowledge, preferences, environment facts - all with provenance, derived
    # deterministically. Informational only.
    cognition_facade = None
    belief_deriver = None
    world_monitor = None
    strategy_selector = None
    goal_manager = None
    if cognition and memory_store is not None:
        cognitive_store = SQLiteCognitiveStore(db_path)
        belief_deriver = DeterministicBeliefDeriver()
        cognition_facade = CognitiveState(memory_store, cognitive_store, belief_deriver)
        # World State (ADR-015): observe the current system state through the
        # monitor so future changes are DETECTED (versioned + events).
        world_monitor = WorldStateMonitor(cognitive_store, sink=events)
        world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
        strategy_selector = StrategySelector()
        # GoalManager is the authoritative goal state machine (ADR-016):
        # lifecycle transitions, plan versioning, progress evaluation,
        # strategy selection, world-state awareness.
        goal_manager = GoalManager(
            storage=storage,
            cognitive_store=cognitive_store,
            events=events,
            strategy_selector=strategy_selector,
            progress_evaluator=DeterministicProgressEvaluator(),
            world_monitor=world_monitor,
        )

    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=events,
        policy=policy,
        approval_handler=approval_handler,
        memory=memory_store,
        reflector=DeterministicReflector() if memory else None,
        cognition=cognition_facade,
        belief_deriver=belief_deriver,
        world_monitor=world_monitor,
        strategy_selector=strategy_selector,
        goal_manager=goal_manager,
    )
