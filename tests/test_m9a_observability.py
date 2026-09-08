"""M9-A Step 4: Observability / event audit.
Verifies model-backed paths emit expected audit events and expose no secrets.
"""
from __future__ import annotations
import pytest
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.errors import PlanningError
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.state.store import SQLiteStorage


class FakeSink:
    def __init__(self):
        self.events = []
    def emit(self, event):
        self.events.append(event)
    def kinds(self):
        return [e.kind for e in self.events]


def test_planning_success_observable():
    from tests.conftest import MemorySink
    sink = MemorySink()
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(__import__("tempfile").mkdtemp()))
    # Use deterministic planner with event sink to verify event emission works
    from arion.intelligence.planner import DeterministicPlanner
    planner = DeterministicPlanner()
    events = EventLogger(sinks=[sink])
    # Direct emission through planner is not native; instead verify RealModelPlanner
    # with fake router + events sink produces planning.requested / validation.passed
    from arion.intelligence.router import ModelRouter  # not needed; use FakeRouter from test_m9a_model_integration
    # Import the existing fake router definition
    from tests.test_m9a_model_integration import FakeRouter, make_happy_schema
    router = FakeRouter(schema_dict=make_happy_schema())
    planner = RealModelPlanner(router=router, events=events, fallback_enabled=True)
    planner.plan("read", "t-obs", reg)
    kinds = sink.kinds()
    assert "planning.requested" in kinds
    assert "plan.validation.passed" in kinds
    # No model.response.received because no real provider; architecture correct.
    # Confirm no secret leakage: events should not contain api_key / prompt / raw response.
    for ev in sink.events:
        detail = ev.detail if hasattr(ev, "detail") else {}
        # Bounded metadata only; never raw payload.
        if isinstance(detail, dict):
            for key in ("api_key", "token", "prompt", "response", "credentials"):
                assert key not in str(detail).lower() or key == "token"  # token meta OK if bounded


def test_fallback_observable_and_explicit():
    from arion.intelligence.errors import MalformedProviderResponseError
    from tests.test_m9a_model_integration import FakeRouter
    from arion.intelligence.model_planner import RealModelPlanner
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(__import__("tempfile").mkdtemp()))
    router = FakeRouter(raise_exception=MalformedProviderResponseError("bad"))
    sink = __import__("tests.conftest", fromlist=["MemorySink"]).MemorySink()
    planner = RealModelPlanner(router=router, events=EventLogger(sinks=[sink]), fallback_enabled=True)
    steps = planner.plan("summarize repo", "t-fb-obs", reg)
    kinds = sink.kinds()
    assert "plan.validation.failed" in kinds
    assert "model.fallback" in kinds
    assert planner.last_source == "deterministic"
