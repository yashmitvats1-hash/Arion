"""ADR-037: exact execution resources stay out of presentation-only state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arion.capabilities.http import (
    FakeTransport,
    HttpGetCapability,
    HttpResponse,
    UrlBoundary,
)
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.cognition.deriver import DeterministicBeliefDeriver
from arion.cognition.state import CognitiveState
from arion.cognition.store import SQLiteCognitiveStore
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.guidance import DeterministicMemoryGuidance
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger, JsonlFileSink
from arion.orchestration.authz import ApprovalOutcome, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.resource_identifiers import (
    MAX_RESOURCE_DISPLAY_CHARS,
    present_resource,
)
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


class _HttpPlanner:
    def __init__(self, url: str, *, capability="http.get", action="get",
                 scope="http:get"):
        self.url = url
        self.capability = capability
        self.action = action
        self.scope = scope

    def required_capabilities(self, goal_description):
        return {self.capability}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=0,
            intent="use URL resource",
            capability=self.capability,
            action=self.action,
            scope=self.scope,
            params={"url": self.url},
            verification=VerificationPolicy(
                "schema_keys", {"keys": ["status", "body"]}
                if self.capability == "http.get" else {"keys": ["review"]}
            ),
            max_attempts=1,
        )]


def test_url_presentation_removes_userinfo_query_and_fragment() -> None:
    first = present_resource(
        "url",
        "https://alice:password@example.com:8443/path/to/data?token=secret&page=1#private",
    )
    second = present_resource(
        "url",
        "https://example.com:8443/path/to/data?token=other&page=1#different",
    )

    assert first.display == (
        "https://example.com:8443/path/to/data?"
        "[query omitted]#[fragment omitted]"
    )
    assert "alice" not in first.display
    assert "password" not in first.display
    assert "secret" not in first.display
    assert "private" not in first.display
    assert first.redacted is True
    assert len(first.fingerprint) == 64
    assert first.display == second.display
    assert first.fingerprint != second.fingerprint


def test_path_and_generic_presentations_remain_useful_and_bounded() -> None:
    path = present_resource("filesystem:path", "docs/design.md")
    oversized = present_resource("queue:name", "q" * 2000)

    assert path.display == "docs/design.md"
    assert path.redacted is False
    assert len(path.fingerprint) == 64
    assert len(oversized.display) <= MAX_RESOURCE_DISPLAY_CHARS
    assert oversized.redacted is True


def _http_engine(tmp_path: Path, url: str, routes: dict, *, name="http"):
    db = tmp_path / f"{name}.db"
    jsonl = tmp_path / f"{name}.jsonl"
    storage = SQLiteStorage(db)
    memory = SQLiteMemoryStore(db)
    cognitive_store = SQLiteCognitiveStore(db)
    belief_deriver = DeterministicBeliefDeriver()
    cognition = CognitiveState(memory, cognitive_store, belief_deriver)
    capability = HttpGetCapability(
        transport=FakeTransport(routes),
        allowed_origins={"https://allowed.example"},
    )
    registry = CapabilityRegistry()
    registry.register(capability)
    events = EventLogger(sinks=[storage, JsonlFileSink(jsonl)])
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
        cognition=cognition,
        belief_deriver=belief_deriver,
    )
    return engine, storage, memory, cognitive_store, capability, jsonl


def _close(*values) -> None:
    for value in values:
        try:
            value.shutdown() if hasattr(value, "shutdown") else value.close()
        except Exception:
            pass


def test_successful_http_exact_url_is_execution_only(tmp_path: Path) -> None:
    query_secret = "QUERY-TOKEN-SECRET"
    fragment_secret = "FRAGMENT-SECRET"
    url = (
        "https://allowed.example/data?"
        f"access_token={query_secret}&page=1#{fragment_secret}"
    )
    engine, storage, memory, cognitive, capability, jsonl = _http_engine(
        tmp_path,
        url,
        {url: HttpResponse(200, {"Content-Type": "text/plain"}, "ok")},
    )

    task = engine.execute_goal("fetch authorized resource")
    persisted = storage.load_task(task.id)
    checkpoints = storage.list_checkpoints(task.id)
    events = storage.list_events(task.id)
    episode = memory.get_episode_by_task(task.id)
    beliefs = cognitive.list_beliefs(limit=100)

    # Exact value remains where execution/recovery requires it.
    assert persisted.steps[0].params["url"] == url
    assert any(
        checkpoint.snapshot["steps"][0]["params"]["url"] == url
        for checkpoint in checkpoints
    )
    assert capability.transport.calls == [url]

    # Result/audit/knowledge destinations receive presentation only.
    result = persisted.steps[0].result
    assert query_secret not in result["url"]
    assert fragment_secret not in result["url"]
    assert result["url_fingerprint"]
    assert query_secret not in json.dumps([event.to_dict() for event in events])
    assert fragment_secret not in json.dumps([event.to_dict() for event in events])
    assert episode is not None
    assert query_secret not in json.dumps(episode.to_dict())
    assert fragment_secret not in json.dumps(episode.to_dict())
    assert query_secret not in " ".join(belief.statement for belief in beliefs)
    assert fragment_secret not in " ".join(belief.statement for belief in beliefs)
    assert query_secret not in jsonl.read_text(encoding="utf-8")
    assert fragment_secret not in jsonl.read_text(encoding="utf-8")

    guidance = DeterministicMemoryGuidance().build(
        [episode],
        memory.list_recent_reflections(limit=10),
    )
    assert guidance and guidance[0].category == "informational"
    assert guidance[0].resource is None
    _close(engine, storage, memory, cognitive)


def test_denied_userinfo_is_not_copied_to_non_execution_state(
    tmp_path: Path,
) -> None:
    username = "embedded-user"
    password = "PASSWORD-SECRET"
    url = (
        f"https://{username}:{password}@allowed.example/private?"
        "token=QUERY-SECRET#FRAGMENT-SECRET"
    )
    engine, storage, memory, cognitive, capability, jsonl = _http_engine(
        tmp_path, url, {}, name="denied"
    )

    task = engine.execute_goal("deny credentialed URL")
    persisted = storage.load_task(task.id)
    episode = memory.get_episode_by_task(task.id)
    events = storage.list_events(task.id)
    beliefs = cognitive.list_beliefs(limit=100)

    assert task.status is TaskStatus.FAILED
    assert capability.transport.calls == []
    assert persisted.steps[0].params["url"] == url  # durable attempted plan
    assert username not in (persisted.error or "")
    assert password not in (persisted.error or "")
    non_execution = json.dumps([event.to_dict() for event in events])
    non_execution += json.dumps(episode.to_dict()) if episode else ""
    non_execution += " ".join(belief.statement for belief in beliefs)
    non_execution += jsonl.read_text(encoding="utf-8")
    assert username not in non_execution
    assert password not in non_execution
    assert "QUERY-SECRET" not in non_execution
    assert "FRAGMENT-SECRET" not in non_execution
    _close(engine, storage, memory, cognitive)


class _ReviewCapability:
    name = "url.review"
    description = "medium-risk URL review"
    actions = [ActionSpec(
        name="review",
        description="review URL",
        required_scope="url:review",
        risk="medium",
        side_effects="read_only",
        resource_kind="url",
        resource_param="url",
        param_schema={"url": {"type": "string", "required": True}},
    )]

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, action, params):
        self.calls.append(params["url"])
        return {"review": "ok"}


def _approval_engine(tmp_path: Path, url: str, capability, *, reopen=False):
    db = tmp_path / "approval.db"
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(capability)
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=_HttpPlanner(
            url, capability="url.review", action="review", scope="url:review"
        ),
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            allowed_scopes={"url:review"},
            boundaries={"url": UrlBoundary({"https://allowed.example"})},
        ),
        scheduler_reclaim_on_start=not reopen,
    )
    return engine, storage


@pytest.mark.parametrize("legacy_fingerprint", [False, True])
def test_approval_display_is_safe_and_exact_resume_still_executes(
    tmp_path: Path, legacy_fingerprint: bool,
) -> None:
    secret = "APPROVAL-QUERY-SECRET"
    url = f"https://allowed.example/review?token={secret}#private-fragment"
    first_capability = _ReviewCapability()
    first_engine, first_storage = _approval_engine(
        tmp_path, url, first_capability
    )

    task = first_engine.execute_goal("review protected URL")
    request = first_storage.list_requests()[0]

    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert first_storage.load_task(task.id).steps[0].params["url"] == url
    assert secret not in (request.resource or "")
    assert secret not in request.summary
    assert secret not in json.dumps(request.fingerprint)
    assert request.fingerprint["resource_fingerprint"]

    if legacy_fingerprint:
        legacy = dict(request.fingerprint)
        legacy.pop("resource_fingerprint", None)
        legacy.pop("resource_redacted", None)
        legacy["resource"] = url
        request.fingerprint = legacy
        first_storage.update_request(request)
        persisted = first_storage.load_task(task.id)
        for record in persisted.approvals:
            if record.get("approval_id") == request.approval_id:
                record["fingerprint"] = dict(legacy)
        first_storage.save_task(persisted)

    first_engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED, actor="user:approver"
    )
    first_engine.shutdown()
    first_storage.close()

    resumed_capability = _ReviewCapability()
    resumed_engine, resumed_storage = _approval_engine(
        tmp_path, url, resumed_capability, reopen=True
    )
    resumed = resumed_engine.run_task(task.id)

    assert resumed.status is TaskStatus.COMPLETED
    assert resumed_capability.calls == [url]
    assert len(resumed_storage.list_requests()) == 1
    assert secret not in json.dumps(
        [event.to_dict() for event in resumed_storage.list_events(task.id)]
    )
    _close(resumed_engine, resumed_storage)


def test_new_fingerprint_detects_exact_query_change(tmp_path: Path) -> None:
    original = "https://allowed.example/review?token=one"
    changed = "https://allowed.example/review?token=two"
    capability = _ReviewCapability()
    engine, storage = _approval_engine(tmp_path, original, capability)
    task = engine.execute_goal("review changing URL")
    request = storage.list_requests()[0]
    engine.resolve_approval_request(request.approval_id, ApprovalOutcome.APPROVED)

    persisted = storage.load_task(task.id)
    persisted.steps[0].params["url"] = changed
    storage.save_task(persisted)
    resumed = engine.run_task(task.id)

    assert resumed.status is TaskStatus.AWAITING_APPROVAL
    assert capability.calls == []
    assert len(storage.list_requests()) == 2
    assert storage.list_requests()[-1].fingerprint["resource_fingerprint"] != (
        request.fingerprint["resource_fingerprint"]
    )
    _close(engine, storage)
