"""Execution-semantics tests (ADR-010): at-least-once + retry safety.

- Automatic retries only for actions metadata-marked retry_safe.
- Non-retry-safe actions fail immediately (a partially applied side effect is
  never blindly re-run).
- Verification failures respect the same retry safety.
- At-least-once after crash is covered in test_persistence.py; here we cover
  the in-loop retry gating.
"""

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


def _engine_with_capability(db_path, capability):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(capability)
    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=DeterministicPlanner(),
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
    )


class FlakyCapability:
    """Fails N times with CapabilityError, then succeeds."""

    def __init__(self, name, retry_safe, fail_times=1):
        self.name = name
        self.description = "flaky"
        self.calls = 0
        self.fail_times = fail_times
        self.actions = [
            ActionSpec(name="run", description="run", required_scope="filesystem:read",
                       risk="low", side_effects="read_only", retry_safe=retry_safe)
        ]

    def execute(self, action, params):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise CapabilityError("transient failure")
        return {"content": "ok"}


def _make_task(engine, capability, max_attempts=2, verification=None):
    goal = engine.submit_goal("run")
    task = engine.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="run", capability=capability.name, action="run",
                 scope="filesystem:read", params={"path": "x"},
                 verification=verification or VerificationPolicy("non_empty"),
                 max_attempts=max_attempts)
    ]
    engine.storage.save_task(task)
    return task


def test_retry_safe_action_retries_and_succeeds(db_path):
    cap = FlakyCapability("flaky.retry", retry_safe=True, fail_times=1)
    engine = _engine_with_capability(db_path, cap)
    task = _make_task(engine, cap)

    result = engine.run_task(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert result.steps[0].attempts == 2
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "step.retrying" in kinds
    assert "verification.passed" in kinds


def test_non_retry_safe_action_fails_immediately(db_path):
    cap = FlakyCapability("flaky.noretry", retry_safe=False, fail_times=1)
    engine = _engine_with_capability(db_path, cap)
    task = _make_task(engine, cap)

    result = engine.run_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.steps[0].attempts == 1  # no automatic retry
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "step.retrying" not in kinds
    assert "transient failure" in (result.error or "")


def test_verification_failure_respects_retry_safety(db_path):
    # verification demands a key the capability never produces
    verification = VerificationPolicy("schema_keys", {"keys": ["never"]})

    cap_retry = FlakyCapability("v.retry", retry_safe=True, fail_times=0)
    engine = _engine_with_capability(db_path, cap_retry)
    task = _make_task(engine, cap_retry, max_attempts=3, verification=verification)
    result = engine.run_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.steps[0].attempts == 3  # retried up to max_attempts
    assert "step.retrying" in [e.kind for e in engine.storage.list_events(task.id)]

    cap_noretry = FlakyCapability("v.noretry", retry_safe=False, fail_times=0)
    engine2 = _engine_with_capability(db_path, cap_noretry)
    task2 = _make_task(engine2, cap_noretry, max_attempts=3, verification=verification)
    result2 = engine2.run_task(task2.id)
    assert result2.status == TaskStatus.FAILED
    assert result2.steps[0].attempts == 1  # no retry on verification failure
    kinds2 = [e.kind for e in engine2.storage.list_events(task2.id)]
    assert "step.retrying" not in kinds2


def test_capability_metadata_available_to_engine(db_path):
    cap = FlakyCapability("meta.cap", retry_safe=True)
    engine = _engine_with_capability(db_path, cap)
    spec = engine.registry.action_spec("meta.cap", "run")
    assert spec is not None
    assert spec.required_scope == "filesystem:read"
    assert spec.risk == "low"
    assert spec.side_effects == "read_only"
    assert spec.reversible is True
    assert spec.idempotent is True
    assert spec.retry_safe is True
