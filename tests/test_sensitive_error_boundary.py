"""ADR-034: external error text must not cross durable observability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.intelligence.errors import ProviderUnavailableError
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.providers.openai_compat import OpenAICompatModelRouter
from arion.intelligence.router import DeterministicRouter
from arion.memory.model_reflector import ModelReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.error_boundary import (
    ErrorSource,
    sanitize_error_text,
    summarize_error,
)
from arion.observability.events import AuditEvent, EventLogger, JsonlFileSink
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, VerificationPolicy
from arion.state.store import SQLiteStorage


class _MemorySink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# Safe test repository\n", encoding="utf-8")
    return root


def _real_model_engine(tmp_path: Path, transport, *, memory: bool = False):
    sandbox = _sandbox(tmp_path)
    storage = SQLiteStorage(tmp_path / "arion.db")
    memory_store = SQLiteMemoryStore(tmp_path / "arion.db") if memory else None
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    jsonl = tmp_path / "events.jsonl"
    events = EventLogger(sinks=[storage, JsonlFileSink(jsonl)])
    router = OpenAICompatModelRouter(
        model="test-model",
        base_url="https://provider.example/v1",
        api_key="sk-configured-secret-123456",
        transport=transport,
        sink=events,
    )
    planner = RealModelPlanner(router, events=events)
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=events,
        policy=ResourcePolicy(
            boundaries={"filesystem:path": RelativePathBoundary()}
        ),
        memory=memory_store,
    )
    return engine, storage, memory_store, jsonl


def _error_details(events: list[AuditEvent]) -> list[dict]:
    return [
        event.detail for event in events
        if event.kind in {
            "model.response.received",
            "plan.validation.failed",
            "reflection.validation.failed",
            "capability.executed",
            "error",
            "task.failed",
        }
    ]


def test_trusted_error_text_is_useful_bounded_and_conservatively_redacted() -> None:
    secret = "sk-live-abcdef123456"
    raw = (
        "connection timed out\nAuthorization: Bearer " + secret
        + "; api_key=another-secret; " + "x" * 1000
    )

    safe = sanitize_error_text(raw, max_length=180, secrets=(secret,))

    assert safe.startswith("connection timed out")
    assert "\n" not in safe
    assert secret not in safe
    assert "another-secret" not in safe
    assert "<redacted>" in safe
    assert len(safe) <= 180


def test_external_summary_drops_arbitrary_text_but_keeps_type_category_and_status() -> None:
    raw = ProviderUnavailableError(
        "provider unavailable (HTTP 503): Authorization: Bearer leaked; PRIVATE PROMPT"
    )

    summary = summarize_error(
        raw,
        source=ErrorSource.EXTERNAL,
        category=raw.category,
    )

    assert summary.message == "provider unavailable (HTTP 503)"
    assert summary.error_type == "ProviderUnavailableError"
    assert summary.category == "provider_unavailable"
    assert summary.source is ErrorSource.EXTERNAL
    assert "leaked" not in str(summary.to_event_detail())
    assert "PRIVATE PROMPT" not in str(summary.to_event_detail())


def test_provider_http_error_never_exposes_response_body_or_configured_key() -> None:
    secret = "sk-configured-secret-123456"
    external = (
        f'{{"error":"Authorization: Bearer {secret}; '
        'PRIVATE PROMPT; COMPLETION FRAGMENT"}'
    )
    sink = _MemorySink()

    def transport(url, headers, body):
        assert secret in headers["Authorization"]
        return 500, external

    router = OpenAICompatModelRouter(
        model="m", api_key=secret, transport=transport, sink=sink
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        router.plan_structured("PRIVATE PROMPT", [], {})

    assert "HTTP 500" in str(raised.value)
    assert secret not in str(raised.value)
    assert "PRIVATE PROMPT" not in str(raised.value)
    assert "COMPLETION FRAGMENT" not in str(raised.value)
    failed = next(event for event in sink.events if not event.success)
    encoded = json.dumps(failed.detail)
    assert secret not in encoded
    assert "PRIVATE PROMPT" not in encoded
    assert "COMPLETION FRAGMENT" not in encoded
    assert failed.detail["category"] == "provider_unavailable"
    assert failed.detail["error_source"] == "external"


def test_provider_body_cannot_reach_task_audit_jsonl_or_memory(tmp_path: Path) -> None:
    secret = "sk-configured-secret-123456"
    response_marker = "RAW-UPSTREAM-BODY-MARKER"
    prompt_marker = "PROMPT-ECHO-MARKER"

    def transport(url, headers, body):
        return 401, (
            f"Authorization: Bearer {secret}; {response_marker}; {prompt_marker}"
        )

    engine, storage, memory, jsonl = _real_model_engine(
        tmp_path, transport, memory=True
    )
    task = engine.execute_goal("Inspect this repository")
    events = storage.list_events(task.id)
    episode = memory.get_episode_by_task(task.id)

    for value in (task.error or "", *(str(d) for d in _error_details(events))):
        assert secret not in value
        assert response_marker not in value
        assert prompt_marker not in value
    assert episode is not None
    assert secret not in str(episode.failures)
    assert response_marker not in str(episode.failures)
    assert prompt_marker not in str(episode.failures)

    jsonl_errors = [
        json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] in {
            "model.response.received", "plan.validation.failed", "error", "task.failed"
        }
    ]
    assert jsonl_errors
    encoded = json.dumps(jsonl_errors)
    assert secret not in encoded
    assert response_marker not in encoded
    assert prompt_marker not in encoded
    assert any(
        row["detail"].get("category") == "provider_auth"
        for row in jsonl_errors
    )

    engine.shutdown()
    storage.close()
    memory.close()


def test_transport_exception_text_is_not_emitted() -> None:
    secret = "sk-transport-secret-123456"
    marker = "REQUEST-BODY-PROMPT-MARKER"
    sink = _MemorySink()

    def transport(url, headers, body):
        raise TimeoutError(
            f"Authorization: Bearer {secret}; body={marker}; timed out"
        )

    router = OpenAICompatModelRouter(
        model="m", api_key=secret, transport=transport, sink=sink
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        router.plan_structured("goal", [], {})

    emitted = json.dumps([event.detail for event in sink.events])
    assert secret not in str(raised.value)
    assert marker not in str(raised.value)
    assert secret not in emitted
    assert marker not in emitted
    assert "provider unavailable" in emitted


def test_model_controlled_schema_text_is_minimized_in_durable_errors(
    tmp_path: Path,
) -> None:
    secret = "sk-model-output-secret-123456"
    marker = "RAW-COMPLETION-FRAGMENT"
    content = json.dumps({
        "version": f"{secret}-{marker}",
        "intent": "bad",
        "steps": [],
    })

    def transport(url, headers, body):
        return 200, json.dumps({"choices": [{"message": {"content": content}}]})

    engine, storage, _, _ = _real_model_engine(tmp_path, transport)
    task = engine.execute_goal("Inspect this repository")
    details = _error_details(storage.list_events(task.id))

    assert secret not in (task.error or "")
    assert marker not in (task.error or "")
    assert secret not in str(details)
    assert marker not in str(details)
    assert any(d.get("category") == "schema_validation" for d in details)
    assert any(d.get("error_type") == "PlanSchemaValidationError" for d in details)
    assert any(d.get("error_source") == "mixed" for d in details)
    engine.shutdown()
    storage.close()


class _FailingCapability:
    name = "external.fail"
    description = "test failure"
    actions = [ActionSpec(
        name="run",
        description="fail",
        required_scope="external:run",
        resource_kind=None,
        resource_param=None,
        param_schema={},
    )]

    def execute(self, action, params):
        raise CapabilityError(
            "upstream timed out\nAuthorization: Bearer cap-secret-123456; "
            "token=secondary-secret; " + "x" * 1000
        )


class _FailingPlanner:
    def required_capabilities(self, goal_description):
        return {"external.fail"}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="fail safely",
            capability="external.fail",
            action="run",
            scope="external:run",
            params={},
            verification=VerificationPolicy("non_empty"),
        )]


def test_capability_error_is_redacted_and_bounded_before_durable_state(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "cap.db")
    registry = CapabilityRegistry()
    registry.register(_FailingCapability())
    planner = _FailingPlanner()
    events = EventLogger(sinks=[storage])
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=events,
        policy=ResourcePolicy(allowed_scopes={"external:run"}),
    )

    task = engine.execute_goal("run external capability")
    persisted = storage.load_task(task.id)
    details = _error_details(storage.list_events(task.id))

    assert persisted is not None
    assert "upstream timed out" in (persisted.steps[0].error or "")
    assert "cap-secret-123456" not in (persisted.steps[0].error or "")
    assert "secondary-secret" not in (persisted.steps[0].error or "")
    assert "\n" not in (persisted.steps[0].error or "")
    assert len(persisted.steps[0].error or "") <= 500
    assert "cap-secret-123456" not in str(details)
    assert "secondary-secret" not in str(details)
    engine.shutdown()
    storage.close()


def test_model_reflector_failure_uses_external_summary(tmp_path: Path) -> None:
    secret = "sk-reflection-secret-123456"

    class FailingReflectionRouter:
        def generate(self, prompt, **kwargs):
            raise ProviderUnavailableError(
                f"provider unavailable (HTTP 502): Bearer {secret}; RAW REFLECTION BODY"
            )

    sandbox = _sandbox(tmp_path)
    storage = SQLiteStorage(tmp_path / "reflect.db")
    memory = SQLiteMemoryStore(tmp_path / "reflect.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = DeterministicPlanner()
    events = EventLogger(sinks=[storage])
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=events,
        policy=ResourcePolicy(
            boundaries={"filesystem:path": RelativePathBoundary()}
        ),
        memory=memory,
        reflector=ModelReflector(FailingReflectionRouter(), events=events),
    )

    task = engine.execute_goal("summarize this repository")
    failed = next(
        event for event in storage.list_events(task.id)
        if event.kind == "reflection.validation.failed"
    )

    assert secret not in str(failed.detail)
    assert "RAW REFLECTION BODY" not in str(failed.detail)
    assert failed.detail["category"] == "reflection_validation"
    assert failed.detail["error_source"] == "external"
    engine.shutdown()
    storage.close()
    memory.close()


def test_sink_failure_diagnostic_is_redacted_and_bounded() -> None:
    class BrokenSink:
        def emit(self, event):
            raise OSError(
                "mirror failed\nAuthorization: Bearer sink-secret-123456; "
                + "x" * 1000
            )

    logger = EventLogger()
    logger.add_sink(BrokenSink(), required=False)
    logger.emit(AuditEvent(kind="task.created"))

    failure = logger.last_failures[0]
    assert failure.message.startswith("mirror failed")
    assert "sink-secret-123456" not in failure.message
    assert "\n" not in failure.message
    assert len(failure.message) <= 300
