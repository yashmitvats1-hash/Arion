"""http.get through the real orchestration path (ADR-018, Phase C DoD):

    Goal -> planner -> PlanValidator -> url ResourceBoundary -> authorization
    -> injected HTTP transport -> verification -> completion

plus the adversarial redirect-escape case being denied end-to-end.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.http import FakeTransport, HttpGetCapability, HttpResponse
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, transport, allowed_origins):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(HttpGetCapability(transport=transport, allowed_origins=allowed_origins))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(
            allowed_scopes={"filesystem:read", "http:get"},
            boundaries={FS: RelativePathBoundary(),
                        "url": __import__("arion.capabilities.http", fromlist=["UrlBoundary"]).UrlBoundary(allowed_origins)},
        ),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage, registry


def test_http_goal_full_orchestration_path(tmp_path, sandbox):
    routes = {
        "https://allowed.example.com/readme.json": HttpResponse(
            status=200, headers={"content-type": "application/json"}, body='{"name": "arion"}'),
    }
    engine, gm, storage, registry = _engine(
        tmp_path / "h.db", sandbox, FakeTransport(routes), {"https://allowed.example.com"})

    goal = engine.submit_goal("fetch https://allowed.example.com/readme.json")
    final = engine.run_goal(goal.id)
    assert final.status == GoalStatus.COMPLETED
    tasks = gm.task_history(goal.id)
    assert tasks[-1].status == TaskStatus.COMPLETED
    steps = tasks[-1].steps
    assert len(steps) == 1
    assert steps[0].capability == "http.get" and steps[0].action == "get"
    assert steps[0].status.value == "succeeded"
    assert steps[0].result["body"] == '{"name": "arion"}'
    kinds = [e.kind for e in storage.list_events()]
    assert "capability.discovered" in kinds
    assert "permission.checked" in kinds
    assert "capability.executed" in kinds
    assert "verification.passed" in kinds
    assert [h["plan_version"] for h in gm.plan_history(goal.id)] == [1]
    engine.storage.close()


def test_http_plan_validator_accepts_url_resource(tmp_path, sandbox):
    from arion.intelligence.errors import PlanCapabilityValidationError
    from arion.intelligence.plan_schema import PlanSchema, StructuredStep
    from arion.intelligence.plan_validator import PlanValidator

    storage = SQLiteStorage(tmp_path / "v.db")
    registry = CapabilityRegistry()
    registry.register(HttpGetCapability(transport=FakeTransport({})))
    schema = PlanSchema(
        version="1.0",
        intent="fetch data",
        steps=[StructuredStep(
            intent="fetch", capability="http.get", action="get",
            params={"url": "https://allowed.example.com/x"},
            verification={"policy": "schema_keys", "args": {"keys": ["status", "body"]}},
        )],
    )
    steps = PlanValidator(registry).validate(schema)
    assert steps[0].capability == "http.get"
    # missing url -> capability validation error
    bad = PlanSchema(version="1.0", intent="fetch", steps=[StructuredStep(
        intent="fetch", capability="http.get", action="get", params={},
        verification={"policy": "schema_keys", "args": {"keys": ["status", "body"]}},
    )])
    with pytest.raises(PlanCapabilityValidationError):
        PlanValidator(registry).validate(bad)
    storage.close()


def test_http_redirect_escape_denied_end_to_end(tmp_path, sandbox):
    routes = {
        "https://allowed.example.com/start": HttpResponse(
            status=302, headers={"location": "https://evil.example.com/payload"}, body=""),
        "https://evil.example.com/payload": HttpResponse(status=200, headers={}, body="pwned"),
    }
    engine, gm, storage, registry = _engine(
        tmp_path / "r.db", sandbox, FakeTransport(routes), {"https://allowed.example.com"})
    goal = engine.submit_goal("fetch https://allowed.example.com/start")
    final = engine.run_goal(goal.id)
    # the step failed; the goal stays ACTIVE with a persisted failure; the
    # escaped target was NEVER fetched
    assert final.status == GoalStatus.ACTIVE
    tasks = gm.task_history(goal.id)
    assert tasks[-1].status == TaskStatus.FAILED
    assert "redirect escaped" in (tasks[-1].error or "")
    transport = registry.get("http.get").transport
    assert "https://evil.example.com/payload" not in transport.calls
    engine.storage.close()


def test_http_goal_discoverable_via_registry(tmp_path, sandbox):
    engine, gm, storage, registry = _engine(
        tmp_path / "d.db", sandbox, FakeTransport({}), {"https://allowed.example.com"})
    summary = {c["name"]: c for c in registry.capabilities_summary()}
    assert "http.get" in summary
    engine.storage.close()
