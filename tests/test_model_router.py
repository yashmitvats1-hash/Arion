"""ModelRouter + OpenAI-compatible provider adapter tests (ADR-005, ADR-011).

The adapter is exercised through a fake HTTP transport - no credentials and
no network. It must produce structured PlanSchema objects and reject anything
malformed or adversarial.
"""

import json

import pytest

from arion.intelligence.errors import (
    MalformedProviderResponseError,
    ModelPlanError,
    PlanSchemaValidationError,
)
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema
from arion.intelligence.providers import OpenAICompatModelRouter

VALID_PLAN = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {"intent": "list", "capability": "filesystem.read", "action": "list",
         "params": {"path": "."}, "verification": {"policy": "non_empty"}},
        {"intent": "read", "capability": "filesystem.read", "action": "read",
         "params": {"path": "README.md"},
         "verification": {"policy": "schema_keys", "args": {"keys": ["content"]}}},
    ],
}


class MemorySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _router(content, status=200, usage=None, sink=None):
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage

    def transport(url, headers, body):
        return status, json.dumps(payload)

    return OpenAICompatModelRouter(
        model="test-model", base_url="https://example.test/v1", api_key="",
        transport=transport, sink=sink,
    )


def test_router_produces_structured_plan():
    sink = MemorySink()
    router = _router(json.dumps(VALID_PLAN), usage={"total_tokens": 42}, sink=sink)
    schema = router.plan_structured("Inspect this repository", [{"name": "filesystem.read"}], {})
    assert isinstance(schema, PlanSchema)
    assert schema.intent == "Inspect this repository"
    assert len(schema.steps) == 2
    # metadata event emitted without raw content
    meta = [e for e in sink.events if e.kind == "model.response.received"]
    assert len(meta) == 1
    assert meta[0].success is True
    assert meta[0].detail["provider"] == "openai-compatible"
    assert meta[0].detail["model"] == "test-model"
    assert "latency_ms" in meta[0].detail
    assert meta[0].detail["tokens"] == {"total_tokens": 42}
    # raw prompt/response never persisted
    raw = json.dumps(meta[0].detail)
    assert "Inspect this repository" not in raw


def test_router_rejects_malformed_json():
    router = _router("this is not json")
    with pytest.raises(MalformedProviderResponseError, match="invalid structured plan"):
        router.plan_structured("goal", [], {})


def test_router_rejects_missing_required_fields():
    router = _router(json.dumps({"version": PLAN_SCHEMA_VERSION, "intent": "x"}))
    with pytest.raises(PlanSchemaValidationError, match="invalid structured plan"):
        router.plan_structured("goal", [], {})


def test_router_rejects_scope_spoofing():
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["scope"] = "shell:exec"
    router = _router(json.dumps(bad))
    with pytest.raises(PlanSchemaValidationError, match="cannot set field"):
        router.plan_structured("goal", [], {})


def test_router_rejects_resource_kind_spoofing():
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["resource_kind"] = "filesystem:write"
    router = _router(json.dumps(bad))
    with pytest.raises(PlanSchemaValidationError, match="cannot set field"):
        router.plan_structured("goal", [], {})


def test_router_rejects_prose_response():
    router = _router("Sure! Here is a plan: first list the directory...")
    with pytest.raises(MalformedProviderResponseError):
        router.plan_structured("goal", [], {})


def test_router_http_error():
    router = _router("error", status=500)
    with pytest.raises(Exception, match="HTTP 500"):
        router.plan_structured("goal", [], {})


def test_router_provider_malformed_response():
    def transport(url, headers, body):
        return 200, "{broken"

    router = OpenAICompatModelRouter(model="m", api_key="", transport=transport)
    with pytest.raises(MalformedProviderResponseError, match="malformed"):
        router.plan_structured("goal", [], {})


def test_router_failure_event_no_raw_content():
    sink = MemorySink()
    router = _router("not json", sink=sink)
    with pytest.raises(MalformedProviderResponseError):
        router.plan_structured("goal", [], {})
    meta = [e for e in sink.events if e.kind == "model.response.received"]
    assert len(meta) == 1 and meta[0].success is False
    raw = json.dumps(meta[0].detail)
    assert "not json" not in raw


def test_generate_freeform():
    router = _router("hello there")
    assert router.generate("hi") == "hello there"


def test_deterministic_router_structured_offline():
    """The deterministic router produces a valid structured plan with no LLM."""
    from arion.intelligence.planner import DeterministicPlanner
    from arion.intelligence.router import DeterministicRouter

    router = DeterministicRouter(DeterministicPlanner())
    catalog = [{"name": "filesystem.read", "actions": [{"name": "read"}, {"name": "list"}]}]
    schema = router.plan_structured("Inspect this repository", catalog, {})
    assert isinstance(schema, PlanSchema)
    assert len(schema.steps) == 2
    with pytest.raises(ModelPlanError):
        router.plan_structured("play the banjo", catalog, {})
    with pytest.raises(ModelPlanError):
        router.plan_structured("Inspect", [], {})
