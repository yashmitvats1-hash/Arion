"""Bootstrap: wiring of layers (composition root).

Central place where Storage, CapabilityRegistry, Planner, Router, Events, the
Policy and the Engine are assembled. New capabilities, policies and interfaces
are wired here without touching layer internals.

Runtime ownership is explicit (ADR-032): stores constructed here are registered
with a ResourceLifecycle and transferred to the engine. Partial construction
failures close everything already created in reverse order.
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
from arion.intelligence.config import load_model_config
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.providers import build_router
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger, JsonlFileSink
from arion.memory.model_reflector import ModelReflector
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.orchestration.authz import (
    ApprovalHandler,
    PermissionPolicy,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.notifications.config import load_webhook_config
from arion.notifications.outbox import WebhookOutboxSink
from arion.notifications.transport import StdlibWebhookTransport
from arion.notifications.worker import WebhookDeliveryWorker
from arion.runtime.lifecycle import ResourceLifecycle
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
    scheduler_reclaim_on_start: bool = True,
    reflector: Any | None = None,
    model_config: Any | None = None,
) -> ArionEngine:
    lifecycle = ResourceLifecycle()
    try:
        storage = lifecycle.register("state.storage", SQLiteStorage(db_path))

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
            # SQLite is the required audit trail. JSONL is a diagnostic mirror:
            # its filesystem being unavailable must not fail an operation that
            # was already durably audited in SQLite (ADR-033).
            events.add_sink(JsonlFileSink(jsonl_log), required=False)

        # Planners (DeterministicPlanner, RealModelPlanner, future planners) are
        # interchangeable; the router follows the same ModelRouter abstraction.
        # ADR-057 M5 (runtime opt-in): when no explicit model_config is given,
        # read the environment. A configured+enabled provider selects the
        # model-backed path (RealModelPlanner + shared model router + optional
        # ModelReflector); no provider keeps the deterministic spine
        # byte-for-byte. An EXPLICIT planner=/router= always wins over
        # environment-driven selection.
        resolved_config = model_config
        if resolved_config is None:
            resolved_config = load_model_config()
        model_router = None
        if resolved_config.enabled:
            model_router = build_router(resolved_config, sink=events)
        if planner is None:
            if model_router is not None:
                planner = RealModelPlanner(
                    model_router,
                    events=events,
                    fallback_enabled=resolved_config.fallback_enabled,
                )
            else:
                planner = DeterministicPlanner()
        if router is None:
            router = model_router if model_router is not None else DeterministicRouter(planner)

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
        memory_store = None
        if memory:
            memory_store = lifecycle.register(
                "memory.store", SQLiteMemoryStore(db_path)
            )

        # Reflector selection (ADR-057 M4/M5): an EXPLICIT reflector always
        # wins. Otherwise a model reflector is selected only when a provider
        # is actually configured AND reflection is enabled (sharing the
        # planner's model router); anything else keeps the existing
        # deterministic reflector (memory on) or None (memory off). This is
        # selection-only wiring - the model reflection path remains
        # informational and never touches authorization.
        if reflector is None and memory:
            if (resolved_config.enabled
                    and model_router is not None
                    and resolved_config.reflection_enabled):
                reflector = ModelReflector(model_router, events=events)
            if reflector is None:
                reflector = DeterministicReflector()

        # Cognitive State / World Model v1 (ADR-014): semantic beliefs, procedural
        # knowledge, preferences, environment facts - all with provenance, derived
        # deterministically. Informational only.
        cognition_facade = None
        belief_deriver = None
        world_monitor = None
        strategy_selector = None
        goal_manager = None
        if cognition and memory_store is not None:
            cognitive_store = lifecycle.register(
                "cognition.store", SQLiteCognitiveStore(db_path)
            )
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

        # Durable webhook notifications (ADR-059, M6-B). Disabled by default:
        # with ARION_WEBHOOK_ENABLED unset this constructs nothing, registers
        # no sink and starts no thread, so the default runtime is unchanged.
        #
        # The outbox sink is added with required=False deliberately (ADR-059
        # D1/D2): notification is strictly downstream of orchestration and
        # must never be able to fail a task. Capture is best-effort; once a
        # row is committed, delivery is at-least-once.
        #
        # The worker is started HERE, after storage exists, rather than by
        # adding a start hook to ResourceLifecycle - ADR-059 D3 keeps
        # ResourceLifecycle unmodified, so it remains a pure
        # register/health/shutdown contract.
        webhook_config = load_webhook_config()
        if webhook_config.enabled:
            events.add_sink(
                WebhookOutboxSink(storage, webhook_config), required=False
            )
            webhook_worker = lifecycle.register(
                "notifications.webhook_worker",
                WebhookDeliveryWorker(
                    storage, webhook_config, StdlibWebhookTransport()
                ),
            )
            webhook_worker.start()

        return ArionEngine(
            storage=storage,
            registry=registry,
            planner=planner,
            router=router,
            events=events,
            policy=policy,
            approval_handler=approval_handler,
            memory=memory_store,
            reflector=reflector,
            cognition=cognition_facade,
            belief_deriver=belief_deriver,
            world_monitor=world_monitor,
            strategy_selector=strategy_selector,
            goal_manager=goal_manager,
            scheduler_reclaim_on_start=scheduler_reclaim_on_start,
            lifecycle=lifecycle,
        )
    except BaseException:
        # Construction is transactional with respect to process resources: a
        # bad sandbox, store migration, world-state observation, or engine
        # constructor cannot leak connections opened earlier in composition.
        lifecycle.shutdown()
        raise
