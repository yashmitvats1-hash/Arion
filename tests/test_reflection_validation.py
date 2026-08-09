"""Reflection schema + ModelReflector tests (learning milestone).

Reflections are strictly validated: authority-bearing fields (scope,
permissions, actor, grant, approve, authorization, capability_registration,
resource_boundary, ...) are REJECTED and can never influence execution.
ModelReflector produces the same structured Reflection; malformed model output
is rejected safely (the engine falls back to the deterministic reflector).
"""

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.model_reflector import ModelReflector
from arion.memory.models import Episode, Reflection
from arion.memory.reflection_schema import (
    ReflectionValidationError,
    reflection_from_json,
    validate_reflection_dict,
)
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus, utcnow
from arion.state.store import SQLiteStorage

VALID = {
    "what_happened": "the task ran",
    "what_worked": "the steps passed",
    "what_failed": "nothing",
    "why": "inputs were valid",
    "lesson": "this approach works",
    "recommendation": "reuse it for similar goals",
    "confidence": "high",
    "importance": 0.7,
}


def _episode():
    return Episode(
        episode_id="ep_1", task_id="task_1", goal_id="g", goal="inspect the repository",
        plan_summary=[], actions=[], outcome="completed", verification={}, failures=[],
        authorization={}, recovery={}, tags=["filesystem.read"], importance=0.5,
        created_at=utcnow(), updated_at=utcnow(),
    )


class FakeRouter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append(prompt)
        if callable(self.response):
            return self.response()
        return self.response


# ---- strict schema validation ----


def test_valid_reflection_accepted():
    ref = validate_reflection_dict(dict(VALID), episode_id="ep_1")
    assert isinstance(ref, Reflection)
    assert ref.episode_id == "ep_1"
    assert ref.confidence == "high"
    assert ref.importance == 0.7


def test_reflection_from_json_valid():
    ref = reflection_from_json(json.dumps(VALID), episode_id="ep_1")
    assert ref.lesson == "this approach works"


def test_malformed_json_rejected():
    with pytest.raises(ReflectionValidationError):
        reflection_from_json("{not json", episode_id="ep_1")


def test_missing_required_field_rejected():
    d = {k: v for k, v in VALID.items() if k != "lesson"}
    with pytest.raises(ReflectionValidationError, match="lesson"):
        validate_reflection_dict(d, episode_id="ep_1")


def test_bad_confidence_rejected():
    with pytest.raises(ReflectionValidationError, match="confidence"):
        validate_reflection_dict(dict(VALID, confidence="certain"), episode_id="ep_1")


def test_bad_importance_rejected():
    with pytest.raises(ReflectionValidationError, match="importance"):
        validate_reflection_dict(dict(VALID, importance=1.5), episode_id="ep_1")
    with pytest.raises(ReflectionValidationError, match="importance"):
        validate_reflection_dict(dict(VALID, importance="high"), episode_id="ep_1")


# ---- adversarial: authority-bearing fields rejected ----


@pytest.mark.parametrize("field", ["scope", "permissions", "actor", "grant", "approve",
                                   "authorization", "capability_registration",
                                   "resource_boundary", "allowed", "policy"])
def test_authority_fields_rejected(field):
    d = dict(VALID, **{field: "anything"})
    with pytest.raises(ReflectionValidationError, match="forbidden"):
        validate_reflection_dict(d, episode_id="ep_1")


def test_unknown_field_rejected():
    with pytest.raises(ReflectionValidationError, match="unknown"):
        validate_reflection_dict(dict(VALID, flavor="chocolate"), episode_id="ep_1")


def test_nested_authority_fields_rejected():
    """Authority fields inside a nested object are also rejected."""
    d = dict(VALID, what_worked={"nested": {"scope": "shell:exec"}})
    with pytest.raises(ReflectionValidationError):
        validate_reflection_dict(d, episode_id="ep_1")


# ---- ModelReflector ----


def test_model_reflector_valid():
    router = FakeRouter(json.dumps(VALID))
    ref = ModelReflector(router).reflect(_episode())
    assert isinstance(ref, Reflection)
    assert ref.episode_id == "ep_1"
    assert router.calls and "inspect the repository" in router.calls[0]


def test_model_reflector_rejects_forbidden_fields():
    d = dict(VALID, scope="filesystem:write")
    router = FakeRouter(json.dumps(d))
    with pytest.raises(ReflectionValidationError, match="forbidden"):
        ModelReflector(router).reflect(_episode())


def test_model_reflector_rejects_malformed_json():
    router = FakeRouter("this is not json")
    with pytest.raises(ReflectionValidationError):
        ModelReflector(router).reflect(_episode())


def test_model_reflector_rejects_prose():
    router = FakeRouter("Sure! Here is my reflection...")
    with pytest.raises(ReflectionValidationError):
        ModelReflector(router).reflect(_episode())


def test_model_reflector_propagates_provider_failure():
    def boom():
        raise ConnectionError("provider down")

    router = FakeRouter(boom)
    with pytest.raises(ReflectionValidationError):
        ModelReflector(router).reflect(_episode())


# ---- engine fallback (malformed model reflection never breaks the loop) ----


def test_engine_falls_back_to_deterministic_reflector(tmp_path, sandbox):
    """A failing model reflector -> reflection.validation.failed + the task
    still completes with a deterministic reflection stored."""
    from arion.memory.model_reflector import ModelReflector

    storage = SQLiteStorage(tmp_path / "r.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(tmp_path / "r.db")

    class AlwaysInvalidModel:
        def generate(self, prompt, **kwargs):
            return json.dumps(dict(VALID, scope="shell:exec"))

    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
        memory=memory,
        reflector=ModelReflector(AlwaysInvalidModel(), events=events),
    )

    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "reflection.validation.failed" in kinds
    # deterministic reflection was stored instead
    episodes = memory.list_recent(limit=5)
    assert episodes and episodes[0].reflection_id
    ref = memory.get_reflection(episodes[0].reflection_id)
    assert ref is not None and "achievable" in ref.lesson  # deterministic lesson
    storage.close()
    memory.close()


def test_deterministic_reflector_remains_default_offline(tmp_path, sandbox):
    storage = SQLiteStorage(tmp_path / "d.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(tmp_path / "d.db")
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
    )
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED
    ep = memory.list_recent(limit=1)[0]
    ref = memory.get_reflection(ep.reflection_id)
    assert ref is not None and ref.confidence in ("low", "medium", "high")
    storage.close()
    memory.close()
