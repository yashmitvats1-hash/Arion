"""M9-A Step 2: Injectable transport boundary.
Uses the adapter's injectable `transport` callable — no real HTTP, no credentials.
"""
from __future__ import annotations

import json

import pytest

from arion.intelligence.providers.openai_compat import OpenAICompatModelRouter
from arion.intelligence.plan_schema import PlanSchema, PLAN_SCHEMA_VERSION


def test_injectable_transport_returns_structured_plan():
    """Fake transport simulates a provider response; adapter parses to PlanSchema."""

    def fake_transport(url, headers, body):
        # Adapter expects OpenAI-compatible response envelope.
        inner = json.dumps({"version": PLAN_SCHEMA_VERSION, "intent":"test","steps":[{"intent":"read","capability":"filesystem.read","action":"read","params":{"path":"README.md"},"verification":{"policy":"non_empty","args":{}}}]})
        return (200, json.dumps({"choices":[{"message":{"content": inner}}]}))

    router = OpenAICompatModelRouter(
        model="fake",
        base_url="http://fake",
        api_key="fake-key-not-used",
        transport=fake_transport,
    )
    # Protocol method
    schema = router.plan_structured("test goal", [], {"task_id": "t"})
    assert schema.version == PLAN_SCHEMA_VERSION
    assert len(schema.steps) == 1
    assert schema.steps[0].capability == "filesystem.read"


def test_injectable_transport_rejects_bad_json():
    def bad_transport(url, headers, body):
        return (200, "not json")

    router = OpenAICompatModelRouter(
        model="fake", base_url="http://fake", api_key="fake",
        transport=bad_transport,
    )
    # Adapter should raise typed failure (not silent degradation)
    from arion.intelligence.errors import MalformedProviderResponseError
    with pytest.raises(MalformedProviderResponseError):
        router.plan_structured("bad", [], {})
