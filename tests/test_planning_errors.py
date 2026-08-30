"""Typed planning error taxonomy tests (ADR-011, required fixes).

Each failure category must remain distinguishable:
- provider unavailable
- provider authentication/configuration
- malformed provider response
- schema validation failure
- capability/parameter/resource validation failure

And the orchestration layer must fail the task gracefully while recording the
typed category in the audit trail.
"""

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.errors import (
    MalformedProviderResponseError,
    PlanCapabilityValidationError,
    PlanSchemaValidationError,
    PlanValidationError,
    PlanningError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION
from arion.intelligence.plan_validator import PlanValidator
from arion.intelligence.providers import OpenAICompatModelRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

VALID = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {"intent": "read", "capability": "filesystem.read", "action": "read",
         "params": {"path": "README.md"},
         "verification": {"policy": "schema_keys", "args": {"keys": ["content"]}}},
    ],
}


def _router_with_status(status=200, content=None):
    def transport(url, headers, body):
        payload = {"choices": [{"message": {"content": content}}]}
        return status, json.dumps(payload)

    # max_retries=0: these tests assert single-attempt typed error mapping;
    # retry behavior has its own suite (tests/test_transport_retry.py).
    return OpenAICompatModelRouter(model="m", api_key="", transport=transport, max_retries=0)


def _router_with_raising_transport(exc):
    def transport(url, headers, body):
        raise exc

    # max_retries=0: see _router_with_status; avoids real backoff sleeps.
    return OpenAICompatModelRouter(model="m", api_key="", transport=transport, max_retries=0)


def _router_with_raw_response(text):
    def transport(url, headers, body):
        return 200, text

    return OpenAICompatModelRouter(model="m", api_key="", transport=transport)


# --- provider unavailable ---


def test_provider_unreachable_network_error():
    router = _router_with_raising_transport(ConnectionError("connection refused"))
    with pytest.raises(ProviderUnavailableError) as ei:
        router.plan_structured("goal", [], {})
    assert ei.value.category == "provider_unavailable"


def test_provider_http_500_is_unavailable():
    router = _router_with_status(500, "boom")
    with pytest.raises(ProviderUnavailableError, match="HTTP 500"):
        router.plan_structured("goal", [], {})


# --- provider authentication / configuration ---


def test_provider_http_401_is_auth_error():
    router = _router_with_status(401, "unauthorized")
    with pytest.raises(ProviderAuthenticationError, match="authentication"):
        router.plan_structured("goal", [], {})


def test_provider_http_403_is_auth_error():
    router = _router_with_status(403, "forbidden")
    with pytest.raises(ProviderAuthenticationError):
        router.plan_structured("goal", [], {})


def test_provider_http_400_is_config_error():
    router = _router_with_status(400, "bad request")
    with pytest.raises(ProviderConfigurationError, match="configuration"):
        router.plan_structured("goal", [], {})


# --- malformed provider response ---


def test_malformed_envelope_is_malformed_response():
    router = _router_with_raw_response("{broken json")
    with pytest.raises(MalformedProviderResponseError, match="malformed"):
        router.plan_structured("goal", [], {})


def test_prose_content_is_malformed_response():
    router = _router_with_status(200, "Sure! First list the directory then...")
    with pytest.raises(MalformedProviderResponseError, match="invalid structured plan"):
        router.plan_structured("goal", [], {})


# --- schema validation failure ---


def test_schema_invalid_plan_is_schema_error():
    bad = json.loads(json.dumps(VALID))
    bad["steps"][0]["scope"] = "shell:exec"  # forbidden field
    router = _router_with_status(200, json.dumps(bad))
    with pytest.raises(PlanSchemaValidationError, match="cannot set field") as ei:
        router.plan_structured("goal", [], {})
    assert ei.value.category == "schema_validation"
    assert isinstance(ei.value, PlanValidationError)  # still the base for callers


# --- capability/parameter/resource validation failure ---


def test_capability_validation_error_type(sandbox):
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    validator = PlanValidator(registry)
    from arion.intelligence.plan_schema import PlanSchema, StructuredStep

    schema = PlanSchema(
        version=PLAN_SCHEMA_VERSION,
        intent="x",
        steps=[StructuredStep(intent="a", capability="shell.exec", action="exec", params={})],
    )
    with pytest.raises(PlanCapabilityValidationError) as ei:
        validator.validate(schema)
    assert ei.value.category == "capability_validation"
    assert isinstance(ei.value, PlanValidationError)


def test_wrong_param_type_is_capability_error(sandbox):
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    validator = PlanValidator(registry)
    from arion.intelligence.plan_schema import PlanSchema, StructuredStep

    schema = PlanSchema(
        version=PLAN_SCHEMA_VERSION,
        intent="x",
        steps=[StructuredStep(intent="a", capability="filesystem.read", action="read",
                              params={"path": 123})],
    )
    with pytest.raises(PlanCapabilityValidationError, match="must be of type"):
        validator.validate(schema)


# --- distinguishability ---


def test_categories_are_distinguishable():
    classes = [
        ProviderUnavailableError,
        ProviderRateLimitError,
        ProviderAuthenticationError,
        ProviderConfigurationError,
        MalformedProviderResponseError,
        PlanSchemaValidationError,
        PlanCapabilityValidationError,
    ]
    categories = {cls("test").category for cls in classes}
    assert len(categories) == len(classes)  # all distinct
    assert all(isinstance(cls("test"), PlanningError) for cls in classes)


# --- orchestration records the typed category ---


def _build_engine(db_path, sandbox, router):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = RealModelPlanner(router, events=events)
    return ArionEngine(
        storage=storage, registry=registry, planner=planner, router=router,
        events=events, policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
    ), storage


def test_engine_records_provider_unavailable_category(tmp_path, sandbox):
    router = _router_with_raising_transport(TimeoutError("timed out"))
    engine, storage = _build_engine(tmp_path / "a.db", sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.FAILED
    assert "planning failed" in (task.error or "")
    error_events = [e for e in storage.list_events(task.id) if e.kind == "error"]
    assert error_events
    assert error_events[0].detail["error_type"] == "ProviderUnavailableError"
    assert error_events[0].detail["category"] == "provider_unavailable"


def test_engine_records_schema_validation_category(tmp_path, sandbox):
    bad = json.loads(json.dumps(VALID))
    bad["steps"][0]["risk"] = "high"  # forbidden field
    router = _router_with_status(200, json.dumps(bad))
    engine, storage = _build_engine(tmp_path / "b.db", sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.FAILED
    error_events = [e for e in storage.list_events(task.id) if e.kind == "error"]
    assert error_events
    assert error_events[0].detail["error_type"] == "PlanSchemaValidationError"
    assert error_events[0].detail["category"] == "schema_validation"
    # plan.validation.failed also carries the typed detail
    failed = [e for e in storage.list_events(task.id) if e.kind == "plan.validation.failed"]
    assert failed[0].detail["category"] == "schema_validation"
