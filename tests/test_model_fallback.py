"""ADR-057 M3: fallback composition tests (model proposes; deterministic Arion decides).

M3 moves model-path failure handling into the PLANNER layer:

    model failure (after permitted retries) -> DeterministicPlanner
        -> SAME validation -> SAME authorization -> SAME execution pipeline

This file proves:

1. The 7-category failure matrix: every typed category either falls back
   (default `fallback_enabled=True`) or fails durably with the TYPED
   category preserved (strict mode `fallback_enabled=False`); nothing else
   (unexpected exceptions) ever becomes fallback.
2. Retry separation: semantic retries (malformed/schema/capability) are
   bounded, re-issue the SAME goal+catalog, and never emit `model.retry`;
   provider transport categories are never semantically retried (M1 owns
   transport retry inside the adapter); `model.fallback` is emitted only
   after the semantic budget is exhausted (or immediately for provider
   categories).
3. Fallback invocation counts: exactly one `model.fallback` when falling
   back, zero when strict or when the model succeeds after retries.
4. Adversarial provider: a model response that tries to self-authorize is
   rejected by validation; fallback produces an INDEPENDENT deterministic
   plan that goes through the normal authorization pipeline (registry
   scopes only, nothing the model invented).
5. Replay: the stored-plan fast path reconstructs the plan WITHOUT calling
   the model (zero router calls, no planning events, `source:"stored"`).
6. Events: `model.fallback` is bounded metadata only; source markers on
   plan.produced / plan.versioned / planning.memory.influence are additive
   (legacy `deterministic: True` preserved).

All fakes are offline; no live provider dependency.
"""

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.errors import (
    MalformedProviderResponseError,
    PlanCapabilityValidationError,
    PlanSchemaValidationError,
    PlanValidationError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from arion.intelligence.model_planner import (
    _FALLBACK_CATEGORIES,
    _SEMANTIC_RETRY_CATEGORIES,
    RealModelPlanner,
)
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EVENT_KINDS, AuditEvent, EventLogger
from arion.orchestration.authz import Actor, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import (
    GoalStatus,
    PlanStep,
    StepStatus,
    TaskStatus,
    VerificationPolicy,
)
from arion.state.store import SQLiteStorage

FS = "filesystem:path"

# ADR-057 D5: the seven typed model/provider failure categories.
CATEGORY_ERRORS = {
    "provider_unavailable": ProviderUnavailableError,
    "provider_rate_limit": ProviderRateLimitError,
    "provider_auth": ProviderAuthenticationError,
    "provider_config": ProviderConfigurationError,
    "malformed_response": MalformedProviderResponseError,
    "schema_validation": PlanSchemaValidationError,
    "capability_validation": PlanCapabilityValidationError,
}

VALID_PLAN = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {"intent": "list root", "capability": "filesystem.read", "action": "list",
         "params": {"path": "."}, "verification": {"policy": "non_empty"}},
        {"intent": "read readme", "capability": "filesystem.read", "action": "read",
         "params": {"path": "README.md"},
         "verification": {"policy": "schema_keys", "args": {"keys": ["content"]}}},
    ],
}


class FakeModelRouter:
    """Deterministic mock model: returns a configurable structured plan.

    `fail_times=None` (default): raise `fail` on EVERY call.
    `fail_times=n`: raise `fail` for the first n calls, then succeed.
    """

    def __init__(self, plan_dict=None, fail=None, fail_times=None):
        self.plan_dict = plan_dict
        self.fail = fail  # exception to raise; None = succeed
        self.fail_times = fail_times
        self.calls = []
        self.api_key = "sk-test-SECRET"  # must never leak into events

    def generate(self, prompt, **kwargs):
        return "mock"

    def plan_structured(self, goal, capabilities, context):
        self.calls.append({"goal": goal, "capabilities": capabilities,
                           "context": context})
        if self.fail is not None and (
                self.fail_times is None or len(self.calls) <= self.fail_times):
            raise self.fail
        return PlanSchema.from_dict(self.plan_dict)


class ExplodingRouter:
    """Router that must NEVER be called: raises on any use and counts calls."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, **kwargs):
        return "mock"

    def plan_structured(self, goal, capabilities, context):
        self.calls.append(goal)
        raise AssertionError("stored-plan fast path must not consult the model")


def _fs_policy() -> ResourcePolicy:
    return ResourcePolicy(boundaries={FS: RelativePathBoundary()})


def _engine(db_path, sandbox, router, fallback_enabled=True, memory=False,
            events=None):
    """Standalone engine (execute_goal path, no GoalManager)."""
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    event_logger = events or EventLogger(sinks=[storage])
    planner = RealModelPlanner(
        router, events=event_logger, fallback_enabled=fallback_enabled)
    kwargs = {}
    if memory:
        kwargs["memory"] = SQLiteMemoryStore(db_path)
        kwargs["reflector"] = DeterministicReflector()
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner, router=router,
        events=event_logger, policy=_fs_policy(), **kwargs)
    return engine, storage


def _gm_engine(db_path, sandbox, router, fallback_enabled=True, memory=True):
    """Managed-goal engine (run_goal path) wired with GoalManager + memory,
    mirroring the managed-goal fixtures used across the suite."""
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = RealModelPlanner(
        router, events=events, fallback_enabled=fallback_enabled)
    memory_store = SQLiteMemoryStore(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()),
               source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner, router=router,
        events=events, policy=_fs_policy(),
        actor=Actor.agent("system"),
        memory=memory_store if memory else None,
        reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=wm,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage


def _fallback_event(storage, task_id):
    for e in storage.list_events(task_id):
        if e.kind == "model.fallback":
            return e
    return None


def _kinds(storage, task_id):
    return [e.kind for e in storage.list_events(task_id)]


# ============================================================ categories


def test_semantic_retry_and_fallback_category_sets():
    assert _SEMANTIC_RETRY_CATEGORIES == frozenset(
        {"malformed_response", "schema_validation", "capability_validation"})
    assert _FALLBACK_CATEGORIES == frozenset(CATEGORY_ERRORS)
    assert _SEMANTIC_RETRY_CATEGORIES <= _FALLBACK_CATEGORIES


def test_model_fallback_kind_registered_and_structured():
    assert "model.fallback" in EVENT_KINDS
    event = AuditEvent(kind="model.fallback", task_id="t1", detail={
        "reason": "provider_unavailable", "attempts": 1,
        "fallback": "deterministic",
    })
    assert event.kind == "model.fallback"
    assert event.detail["fallback"] == "deterministic"


def test_m3_defaults():
    planner = RealModelPlanner(router=ExplodingRouter())
    assert planner.fallback_enabled is True      # default: fall back
    assert planner.semantic_max_retries == 2     # bounded semantic budget
    assert planner.last_source is None


# ============================================ 7-category failure matrix

# provider categories are never semantically retried: 1 router call, 1
# attempt reported on the fallback event. Semantic categories retry up to
# the bounded budget (2) before the final attempt falls back: 3 calls, 3
# attempts.
CATEGORY_EXPECTATIONS = {
    "provider_unavailable": (1, 1),
    "provider_rate_limit": (1, 1),
    "provider_auth": (1, 1),
    "provider_config": (1, 1),
    "malformed_response": (3, 3),
    "schema_validation": (3, 3),
    "capability_validation": (3, 3),
}


@pytest.mark.parametrize("category", sorted(CATEGORY_ERRORS))
def test_matrix_fallback_mode_completes_via_deterministic(sandbox, db_path,
                                                          category):
    """Default mode: every typed category falls back to the deterministic
    planner; the task COMPLETES through the normal pipeline; exactly one
    bounded `model.fallback`; source marker is deterministic."""
    calls, attempts = CATEGORY_EXPECTATIONS[category]
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=CATEGORY_ERRORS[category]("boom"))
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    assert len(router.calls) == calls
    fallback = _fallback_event(storage, task.id)
    assert fallback is not None
    assert fallback.detail == {
        "reason": category, "attempts": attempts, "fallback": "deterministic",
    }
    kinds = _kinds(storage, task.id)
    assert "plan.validation.failed" in kinds
    # the fallback plan entered the SAME authorization pipeline
    checked = [e for e in storage.list_events(task.id)
               if e.kind == "permission.checked"]
    assert checked and all(c.detail["scope"] == "filesystem:read" for c in checked)
    produced = [e for e in storage.list_events(task.id)
                if e.kind == "plan.produced"][0]
    assert produced.detail["source"] == "deterministic"


@pytest.mark.parametrize("category", sorted(CATEGORY_ERRORS))
def test_matrix_strict_mode_fails_durably_typed(sandbox, db_path, category):
    """Strict mode: the typed category is preserved as a durable failure;
    no fallback; nothing executes; no new blocker, no infinite loop."""
    calls, _ = CATEGORY_EXPECTATIONS[category]
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=CATEGORY_ERRORS[category]("boom"))
    engine, storage = _engine(db_path, sandbox, router, fallback_enabled=False)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.FAILED
    assert "planning failed" in (task.error or "")
    assert _fallback_event(storage, task.id) is None
    assert "capability.executed" not in _kinds(storage, task.id)
    error_events = [e for e in storage.list_events(task.id) if e.kind == "error"]
    assert error_events
    assert error_events[0].detail["category"] == category
    failed = [e for e in storage.list_events(task.id)
              if e.kind == "plan.validation.failed"]
    assert failed[0].detail["category"] == category
    assert len(router.calls) == calls  # semantic retries bounded; provider once


def test_strict_mode_raises_typed_errors_directly(sandbox):
    """At the planner boundary, strict mode surfaces the TYPED exception
    (not a generic wrapper) so callers can distinguish categories."""
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    for category, exc_cls in CATEGORY_ERRORS.items():
        router = FakeModelRouter(plan_dict=VALID_PLAN, fail=exc_cls("boom"))
        planner = RealModelPlanner(router, events=None,
                                   fallback_enabled=False)
        with pytest.raises(exc_cls) as ei:
            planner.plan("Inspect this repository", "t1", registry)
        assert ei.value.category == category


# ============================================================ retry policy


def test_semantic_retries_same_goal_and_catalog(sandbox, db_path):
    """Semantic retries re-issue the SAME goal + SAME catalog."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=PlanSchemaValidationError("bad"),
                             fail_times=2)
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    assert len(router.calls) == 3  # fail, fail, success (budget = 2)
    goals = {c["goal"] for c in router.calls}
    assert len(goals) == 1
    catalogs = {json.dumps(c["capabilities"], sort_keys=True)
                for c in router.calls}
    assert len(catalogs) == 1
    # succeeded within budget: no fallback, source is model
    assert _fallback_event(storage, task.id) is None
    kinds = _kinds(storage, task.id)
    assert "plan.validation.passed" in kinds
    assert "model.retry" not in kinds  # semantic retries never emit model.retry
    produced = [e for e in storage.list_events(task.id)
                if e.kind == "plan.produced"][0]
    assert produced.detail["source"] == "model"


def test_semantic_budget_exhausted_falls_back_once(sandbox, db_path):
    """Permanent semantic failure: bounded retries, then ONE fallback."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=PlanSchemaValidationError("bad"))
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    assert len(router.calls) == 3  # 2 retries + 1 final attempt
    assert len([e for e in storage.list_events(task.id)
                if e.kind == "model.fallback"]) == 1
    assert _fallback_event(storage, task.id).detail["attempts"] == 3


def test_provider_category_never_semantically_retried(sandbox, db_path):
    """Transport categories (M1's domain) are not reprompted at the planner
    layer: exactly one router call, immediate fallback."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=ProviderUnavailableError("down"))
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    assert len(router.calls) == 1
    fallback = _fallback_event(storage, task.id)
    assert fallback.detail["attempts"] == 1
    assert "model.retry" not in _kinds(storage, task.id)


def test_fallback_never_emits_model_retry_for_any_category(sandbox, db_path):
    """`model.retry` is M1 transport-retry observability owned by the
    provider adapter; the planner/fallback path never emits it."""
    for category, exc_cls in CATEGORY_ERRORS.items():
        db = f"{db_path}-{category}.db"  # db_path is a str path
        router = FakeModelRouter(plan_dict=VALID_PLAN, fail=exc_cls("boom"))
        engine, storage = _engine(db, sandbox, router)
        task = engine.execute_goal("Inspect this repository")
        assert task.status == TaskStatus.COMPLETED, task.error
        assert "model.retry" not in _kinds(storage, task.id)
        assert _fallback_event(storage, task.id) is not None


def test_unknown_exception_never_fallback_or_retry(sandbox, db_path):
    """A programming error (non-PlanningError) must never silently degrade
    to deterministic cognition: no retry, no fallback, wrapped + durable."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=RuntimeError("planner bug"))
    engine, storage = _engine(db_path, sandbox, router)  # fallback ENABLED

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.FAILED
    assert len(router.calls) == 1  # never retried
    assert _fallback_event(storage, task.id) is None
    # the planner boundary wraps the unknown error as a typed
    # PlanValidationError (category "unknown" at the planner, "plan_validation"
    # on the engine's durable error event) - never a fallback category.
    failed = [e for e in storage.list_events(task.id)
              if e.kind == "plan.validation.failed"]
    assert failed[0].detail["category"] == "unknown"
    error_events = [e for e in storage.list_events(task.id) if e.kind == "error"]
    assert error_events
    assert error_events[0].detail["error_type"] == "PlanValidationError"
    assert error_events[0].detail["category"] == "plan_validation"


# ============================================================ adversarial


def test_adversarial_provider_cannot_self_authorize(sandbox, db_path):
    """The model forges scope/capability: validation rejects it, the
    fallback plan is the independent deterministic plan, and every
    authorization decision uses registry scopes - never the model's."""
    forged = json.loads(json.dumps(VALID_PLAN))
    forged["steps"][0]["scope"] = "shell:exec"
    forged["steps"][0]["capability"] = "shell.exec"
    forged["steps"][0]["action"] = "execute"
    router = FakeModelRouter(plan_dict=forged)
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    assert _fallback_event(storage, task.id) is not None
    # the executed plan is the DETERMINISTIC one (registry capability)
    assert all(s.capability == "filesystem.read" for s in task.steps)
    # the model's forged capability never reached authorization/execution
    checked = [e for e in storage.list_events(task.id)
               if e.kind == "permission.checked"]
    assert checked and all(c.detail["scope"] == "filesystem:read"
                           for c in checked)
    assert not any("shell" in json.dumps(s.to_dict()) for s in task.steps)


def _planning_shape(step: PlanStep) -> dict:
    """The planning-relevant projection of a step (excludes execution state
    such as status/result that the engine mutates after planning)."""
    return {
        "index": step.index, "intent": step.intent,
        "capability": step.capability, "action": step.action,
        "scope": step.scope, "params": step.params,
        "verification": {"policy": step.verification.policy,
                         "args": step.verification.args},
    }


def test_fallback_plan_independent_of_model(sandbox, db_path):
    """Fallback steps match a fresh DeterministicPlanner run exactly: the
    failed model response influenced nothing."""
    forged = json.loads(json.dumps(VALID_PLAN))
    forged["steps"][0]["scope"] = "shell:exec"  # schema rejection
    router = FakeModelRouter(plan_dict=forged)
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    expected = DeterministicPlanner().plan(
        "Inspect this repository", task.id, registry)
    assert [_planning_shape(s) for s in task.steps] == [
        _planning_shape(s) for s in expected]
    # plan.validation.failed carries only the typed summary (no raw response)
    failed = [e for e in storage.list_events(task.id)
              if e.kind == "plan.validation.failed"]
    assert failed and failed[0].detail["category"] == "schema_validation"


# ============================================================ replay (D5)


def test_stored_plan_fast_path_zero_model_calls(tmp_path, sandbox):
    """Replay: the stored-plan fast path reconstructs the plan WITHOUT
    consulting the model - zero router calls, no planning/fallback events,
    `source:"stored"` marker."""
    sb = sandbox
    db = str(tmp_path / "replay.db")
    router = ExplodingRouter()
    engine, gm, storage = _gm_engine(db, sb, router, fallback_enabled=True)

    gid = engine.submit_goal("Inspect this repository").id
    steps = [
        PlanStep(index=0, intent="list root", capability="filesystem.read",
                 action="list", scope="filesystem:read", params={"path": "."},
                 verification=VerificationPolicy("non_empty")),
        PlanStep(index=1, intent="read readme", capability="filesystem.read",
                 action="read", scope="filesystem:read",
                 params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys",
                                                 {"keys": ["content"]})),
    ]
    gm.record_plan_version(gid, "direct", [s.to_dict() for s in steps],
                           reason="initial_plan")

    goal = engine.run_goal(gid, max_replans=1)

    assert goal.status_value == GoalStatus.COMPLETED.value
    assert router.calls == []  # THE invariant: zero model calls
    tasks = [t for t in storage.list_tasks() if t.goal_id == gid]
    completed = [t for t in tasks if t.status == TaskStatus.COMPLETED]
    assert completed
    task_id = completed[0].id
    kinds = _kinds(storage, task_id)
    for forbidden in ("planning.requested", "plan.validation.failed",
                      "plan.validation.passed", "model.fallback",
                      "model.retry"):
        assert forbidden not in kinds, f"replay must not emit {forbidden}"
    produced = [e for e in storage.list_events(task_id)
                if e.kind == "plan.produced"][0]
    assert produced.detail["source"] == "stored"
    assert produced.detail["stored_plan"] is True
    # the stored plan still passes the LIVE authorization pipeline
    checked = [e for e in storage.list_events(task_id)
               if e.kind == "permission.checked"]
    assert checked and all(c.detail["scope"] == "filesystem:read"
                           for c in checked)
    engine.shutdown()
    storage.close()


# ============================================================ source markers


def test_plan_versioned_source_markers(sandbox, tmp_path):
    """plan.versioned carries the additive source marker: "deterministic"
    for fallback, "model" for model success; absent for legacy callers."""
    # fallback path
    db1 = str(tmp_path / "v1.db")
    r1 = FakeModelRouter(plan_dict=VALID_PLAN,
                         fail=ProviderUnavailableError("down"))
    engine1, gm1, storage1 = _gm_engine(db1, sandbox, r1)
    g1 = engine1.submit_goal("Inspect this repository")
    engine1.run_goal(g1.id, max_replans=1)
    versions1 = [e for e in storage1.list_events() if e.kind == "plan.versioned"]
    assert versions1 and versions1[0].detail["source"] == "deterministic"
    engine1.shutdown()
    storage1.close()

    # model success path
    db2 = str(tmp_path / "v2.db")
    r2 = FakeModelRouter(plan_dict=VALID_PLAN)
    engine2, gm2, storage2 = _gm_engine(db2, sandbox, r2)
    g2 = engine2.submit_goal("Inspect this repository")
    engine2.run_goal(g2.id, max_replans=1)
    versions2 = [e for e in storage2.list_events() if e.kind == "plan.versioned"]
    assert versions2 and versions2[0].detail["source"] == "model"
    engine2.shutdown()
    storage2.close()


def test_memory_influence_source_marker_preserves_legacy(sandbox, db_path):
    """planning.memory.influence gains the additive "source" key while the
    legacy "deterministic": True boolean is preserved."""
    router = FakeModelRouter(plan_dict=VALID_PLAN)
    engine, storage = _engine(db_path, sandbox, router, memory=True)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    influence = [e for e in storage.list_events(task.id)
                 if e.kind == "planning.memory.influence"][0]
    assert influence.detail["deterministic"] is True  # legacy preserved
    assert influence.detail["source"] == "deterministic"


def test_memory_influence_marker_stable_after_fallback(sandbox, db_path):
    """The memory-influence pipeline is a deterministic mechanism even when
    the plan came from fallback - the marker is stable in both paths."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=ProviderUnavailableError("down"))
    engine, storage = _engine(db_path, sandbox, router, memory=True)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    influence = [e for e in storage.list_events(task.id)
                 if e.kind == "planning.memory.influence"][0]
    assert influence.detail["deterministic"] is True
    assert influence.detail["source"] == "deterministic"


# ============================================================ event boundedness


def test_fallback_event_bounded_no_sensitive_data(sandbox, db_path):
    """model.fallback carries ONLY reason/attempts/fallback - never
    prompts, responses, credentials, or provider payloads."""
    secret = "sk-SECRET-abcdef123456"
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=ProviderAuthenticationError("auth failed"))
    router.api_key = secret
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository with secret goal")

    fallback = _fallback_event(storage, task.id)
    assert fallback is not None
    assert set(fallback.detail) == {"reason", "attempts", "fallback"}
    assert fallback.detail["reason"] == "provider_auth"
    assert fallback.detail["attempts"] == 1
    assert fallback.detail["fallback"] == "deterministic"
    serialized = json.dumps(fallback.detail)
    assert secret not in serialized
    assert "Inspect this repository with secret goal" not in serialized
    assert "sk-" not in serialized


def test_no_second_execution_path_single_pipeline(sandbox, db_path):
    """Fallback returns an ORDINARY plan: exactly one plan.produced, one
    fallback, no replan loop - the deterministic plan entered the SAME
    pipeline (no second execution path)."""
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=PlanCapabilityValidationError("bad"))
    engine, storage = _engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED, task.error
    kinds = _kinds(storage, task.id)
    assert kinds.count("plan.produced") == 1
    # plan.validation.failed fires once: on the FINAL model-path failure
    # after the bounded retries (retries themselves emit nothing)
    assert kinds.count("plan.validation.failed") == 1
    assert kinds.count("model.fallback") == 1
    # validation events gate the MODEL path; deterministic fallback plans are
    # constructed from the live registry and enter the identical downstream
    # pipeline (normalization -> immutable version -> live authorization).
    assert "plan.validation.passed" not in kinds
    # the deterministic plan executed through the standard step lifecycle
    assert all(s.status == StepStatus.SUCCEEDED for s in task.steps)


def test_strict_mode_max_replans_boundary_untouched(sandbox, tmp_path):
    """Strict mode introduces no new blocker/replan mechanics: a strict
    failure surfaces exactly like the pre-M3 durable failure - the task is
    FAILED with the typed category and run_goal returns (goal stays ACTIVE
    for the caller to decide); no fallback, no loop."""
    db = str(tmp_path / "strict.db")
    router = FakeModelRouter(plan_dict=VALID_PLAN,
                             fail=ProviderUnavailableError("down"))
    engine, gm, storage = _gm_engine(db, sandbox, router,
                                     fallback_enabled=False)
    gid = engine.submit_goal("Inspect this repository").id

    goal = engine.run_goal(gid, max_replans=1)

    # pre-M3 semantics preserved: one task planned, failed durably, stop
    assert goal.status_value == GoalStatus.ACTIVE.value
    assert len(router.calls) == 1
    tasks = [t for t in storage.list_tasks() if t.goal_id == gid]
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.FAILED
    assert "planning failed" in (tasks[0].error or "")
    assert not [e for e in storage.list_events() if e.kind == "model.fallback"]
    engine.shutdown()
    storage.close()
