"""ADR-035: capability observations have an explicit durable contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import arion.capabilities.observations as observation_contract
from arion.capabilities.http import (
    FakeTransport,
    HttpGetCapability,
    HttpResponse,
    UrlBoundary,
)
from arion.capabilities.observations import (
    ObservationContractError,
    normalize_observation,
)
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import Checkpoint, PlanStep, Task, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


def test_raw_mapping_is_canonicalized_and_detached() -> None:
    source = {
        "items": ("a", "b"),
        "nested": {"count": 2},
    }

    observation = normalize_observation(source)
    source["nested"]["count"] = 99

    assert observation == {
        "items": ["a", "b"],
        "nested": {"count": 2},
    }
    assert isinstance(observation, dict)


def test_observation_requires_json_object_and_string_keys() -> None:
    with pytest.raises(ObservationContractError, match="mapping"):
        normalize_observation(["not", "an", "object"])
    with pytest.raises(ObservationContractError, match="string keys"):
        normalize_observation({"nested": {1: "bad"}})
    with pytest.raises(ObservationContractError, match="JSON serializable"):
        normalize_observation({"values": {"not", "json"}})
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ObservationContractError, match="acyclic"):
        normalize_observation(cyclic)


def test_observation_encoded_size_budget_is_explicit() -> None:
    with pytest.raises(ObservationContractError, match="exceeds") as raised:
        normalize_observation({"body": "x" * 500}, max_bytes=128)

    assert "128" in str(raised.value)
    assert "xxxxx" not in str(raised.value)


class _OneStepPlanner:
    def __init__(self, capability: str, action: str, scope: str):
        self.capability = capability
        self.action = action
        self.scope = scope

    def required_capabilities(self, goal_description):
        return {self.capability}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="execute capability",
            capability=self.capability,
            action=self.action,
            scope=self.scope,
            params={},
            verification=VerificationPolicy("schema_keys", {"keys": ["body"]}),
            max_attempts=1,
        )]


def _engine(tmp_path: Path, capability, planner, scope: str):
    storage = SQLiteStorage(tmp_path / "arion.db")
    registry = CapabilityRegistry()
    registry.register(capability)
    events = EventLogger(sinks=[storage])
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=events,
        policy=ResourcePolicy(allowed_scopes={scope}),
    )
    return engine, storage


def test_oversized_dynamic_observation_fails_before_result_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "OVERSIZED-RESULT-MARKER"

    class Capability:
        name = "dynamic.large"
        description = "large result"
        actions = [ActionSpec(
            name="get",
            description="get",
            required_scope="dynamic:get",
            retry_safe=False,
            param_schema={},
        )]

        def execute(self, action, params):
            return {"body": marker + "x" * 2000}

    monkeypatch.setattr(
        observation_contract,
        "MAX_DURABLE_OBSERVATION_BYTES",
        512,
    )
    planner = _OneStepPlanner("dynamic.large", "get", "dynamic:get")
    engine, storage = _engine(tmp_path, Capability(), planner, "dynamic:get")

    task = engine.execute_goal("fetch large result")
    persisted = storage.load_task(task.id)
    checkpoints = storage.list_checkpoints(task.id)

    assert task.status is TaskStatus.FAILED
    assert task.steps[0].result is None
    assert persisted is not None and persisted.steps[0].result is None
    assert marker not in json.dumps(persisted.to_dict())
    assert all(marker not in json.dumps(checkpoint.snapshot) for checkpoint in checkpoints)
    assert "exceeds" in (task.steps[0].error or "")
    engine.shutdown()
    storage.close()


def test_invalid_result_after_non_retry_safe_mutation_requires_recovery(
    tmp_path: Path,
) -> None:
    class Capability:
        name = "dynamic.mutate"
        description = "mutation with invalid result"
        calls = 0
        actions = [ActionSpec(
            name="write",
            description="write",
            required_scope="dynamic:write",
            side_effects="mutating",
            reversible=False,
            idempotent=False,
            retry_safe=False,
            param_schema={},
        )]

        def execute(self, action, params):
            self.calls += 1
            return {"body": "applied", "invalid": {1, 2, 3}}

    capability = Capability()
    planner = _OneStepPlanner("dynamic.mutate", "write", "dynamic:write")
    engine, storage = _engine(tmp_path, capability, planner, "dynamic:write")

    task = engine.execute_goal("perform mutation")
    recoveries = engine.recovery_store.list_recoveries()

    assert capability.calls == 1
    assert task.status is TaskStatus.FAILED
    assert task.steps[0].result is None
    assert len(recoveries) == 1
    assert recoveries[0].capability == "dynamic.mutate"
    assert "recovery required" in (task.steps[0].error or "")
    engine.shutdown()
    storage.close()


def test_http_result_retains_only_bounded_safe_headers() -> None:
    url = "https://allowed.example/data"
    capability = HttpGetCapability(
        transport=FakeTransport({
            url: HttpResponse(
                status=200,
                headers={
                    "Content-Type": "application/json",
                    "ETag": "abc123",
                    "Cache-Control": "private\r\nno-store",
                    "Authorization": "Bearer response-secret",
                    "Proxy-Authorization": "Basic proxy-secret",
                    "Set-Cookie": "session=cookie-secret",
                    "WWW-Authenticate": "Bearer realm=private",
                    "X-Untrusted": "external metadata",
                    "Last-Modified": "x" * 5000,
                },
                body='{"ok":true}',
            ),
        }),
        allowed_origins={"https://allowed.example"},
    )

    result = capability.execute("get", {"url": url})

    assert result["body"] == '{"ok":true}'
    assert result["status"] == 200
    assert result["url"] == url
    assert result["headers"]["content-type"] == "application/json"
    assert result["headers"]["etag"] == "abc123"
    assert "\r" not in result["headers"]["cache-control"]
    assert "\n" not in result["headers"]["cache-control"]
    assert len(result["headers"]["last-modified"]) <= 1024
    for forbidden in (
        "authorization",
        "proxy-authorization",
        "set-cookie",
        "www-authenticate",
        "x-untrusted",
    ):
        assert forbidden not in result["headers"]


class _HttpPlanner:
    def __init__(self, url: str):
        self.url = url

    def required_capabilities(self, goal_description):
        return {"http.get"}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="fetch HTTP data",
            capability="http.get",
            action="get",
            scope="http:get",
            params={"url": self.url},
            verification=VerificationPolicy(
                "schema_keys", {"keys": ["status", "body"]}
            ),
        )]


def test_http_sensitive_headers_never_enter_task_checkpoints_or_memory(
    tmp_path: Path,
) -> None:
    url = "https://allowed.example/data"
    body_marker = "AUTHORIZED-BODY-CONTENT"
    header_secret = "RESPONSE-HEADER-SECRET"
    storage = SQLiteStorage(tmp_path / "http.db")
    memory = SQLiteMemoryStore(tmp_path / "http.db")
    capability = HttpGetCapability(
        transport=FakeTransport({
            url: HttpResponse(
                status=200,
                headers={
                    "Content-Type": "text/plain",
                    "Authorization": f"Bearer {header_secret}",
                    "Set-Cookie": f"session={header_secret}",
                },
                body=body_marker,
            ),
        }),
        allowed_origins={"https://allowed.example"},
    )
    registry = CapabilityRegistry()
    registry.register(capability)
    events = EventLogger(sinks=[storage])
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=_HttpPlanner(url),
        router=DeterministicRouter(DeterministicPlanner()),
        events=events,
        policy=ResourcePolicy(
            allowed_scopes={"http:get"},
            boundaries={"url": UrlBoundary({"https://allowed.example"})},
        ),
        memory=memory,
        reflector=DeterministicReflector(),
    )

    task = engine.execute_goal("fetch authorized HTTP data")
    persisted = storage.load_task(task.id)
    checkpoints = storage.list_checkpoints(task.id)
    episode = memory.get_episode_by_task(task.id)

    assert task.status is TaskStatus.COMPLETED
    assert persisted is not None
    assert persisted.steps[0].result["body"] == body_marker
    assert persisted.steps[0].result["headers"] == {
        "content-type": "text/plain"
    }
    assert body_marker in json.dumps(persisted.to_dict())
    assert header_secret not in json.dumps(persisted.to_dict())
    assert all(
        header_secret not in json.dumps(checkpoint.snapshot)
        for checkpoint in checkpoints
    )
    assert episode is not None
    assert body_marker not in json.dumps(episode.to_dict())
    assert header_secret not in json.dumps(episode.to_dict())
    engine.shutdown()
    storage.close()
    memory.close()


def test_legacy_task_result_dictionary_remains_readable() -> None:
    legacy = Task.from_dict({
        "id": "task-legacy",
        "goal_id": "goal-legacy",
        "description": "legacy result",
        "status": "completed",
        "steps": [{
            "index": 0,
            "capability": "legacy.capability",
            "action": "read",
            "scope": "legacy:read",
            "status": "succeeded",
            "result": {
                "headers": {"Set-Cookie": "historical-value"},
                "body": "historical content",
            },
        }],
        "current_step": 0,
    })

    assert legacy.steps[0].result == {
        "headers": {"Set-Cookie": "historical-value"},
        "body": "historical content",
    }
    checkpoint = Checkpoint.from_dict({
        "task_id": legacy.id,
        "status": "completed",
        "step_index": 0,
        "snapshot": legacy.to_dict(),
        "reason": "legacy checkpoint",
    })
    restored = Task.from_dict(checkpoint.snapshot)
    assert restored.steps[0].result == legacy.steps[0].result
