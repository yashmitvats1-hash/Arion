"""RealModelPlanner + orchestration integration tests (ADR-011).

The integration test drives the full pipeline with a MOCKED ModelRouter:

    free-form goal -> ModelRouter -> structured plan -> validator
    -> authorization -> capability -> verification -> completion

Adversarial tests prove the model cannot bypass the capability registry or
authorization layer: spoofed scopes/fields are rejected by the schema,
unknown capabilities/actions and bad params by the validator, and
out-of-boundary resources by the authorization policy.
"""

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import PathPrefixBoundary, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import StepStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _fs_policy(**kw) -> ResourcePolicy:
    return ResourcePolicy(boundaries={FS: RelativePathBoundary()}, **kw)


class FakeModelRouter:
    """Deterministic mock model: returns a configurable structured plan."""

    def __init__(self, plan_dict: dict):
        self.plan_dict = plan_dict
        self.calls = []

    def generate(self, prompt, **kwargs):
        return "mock"

    def plan_structured(self, goal, capabilities, context):
        self.calls.append({"goal": goal, "capabilities": capabilities, "context": context})
        return PlanSchema.from_dict(self.plan_dict)


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


def _build_engine(db_path, sandbox, router, policy=None, events=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = RealModelPlanner(router, events=events or EventLogger(sinks=[storage]))
    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=events or EventLogger(sinks=[storage]),
        policy=policy or _fs_policy(),
    )


def test_integration_free_form_goal_to_completion(sandbox, db_path):
    """Full pipeline: goal -> mocked ModelRouter -> structured plan -> validator
    -> authorization -> capability -> verification -> completion."""
    router = FakeModelRouter(VALID_PLAN)
    engine = _build_engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.COMPLETED
    assert len(task.steps) == 2
    assert all(s.status == StepStatus.SUCCEEDED for s in task.steps)
    assert task.steps[0].result["entries"]  # list produced entries
    assert "content" in task.steps[1].result

    # the model was consulted with the live capability catalog (no hardcoded list)
    assert router.calls, "model router was never consulted"
    catalog_names = {c["name"] for c in router.calls[0]["capabilities"]}
    assert catalog_names == {"filesystem.read"}
    action_meta = router.calls[0]["capabilities"][0]["actions"][0]
    assert action_meta["required_scope"] == "filesystem:read"
    assert action_meta["resource_kind"] == "filesystem:path"
    assert action_meta["resource_param"] == "path"
    assert "risk" in action_meta and "side_effects" in action_meta
    assert "retry_safe" in action_meta and "default_verification" in action_meta

    # audit trail records the intelligence/validation/action lifecycle
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    for expected in ("planning.requested", "plan.validation.passed", "permission.checked",
                     "capability.executed", "verification.passed", "task.completed"):
        assert expected in kinds, f"missing {expected}"
    # steps authorized under the registry scope, not anything the model invented
    checked = [e for e in engine.storage.list_events(task.id) if e.kind == "permission.checked"]
    assert all(c.detail["scope"] == "filesystem:read" for c in checked)


def test_integration_scope_spoofing_rejected(sandbox, db_path):
    """The model emits scope=shell:exec: the schema rejects it before execution."""
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["scope"] = "shell:exec"
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")

    assert task.status == TaskStatus.FAILED
    assert "cannot set field" in (task.error or "")
    assert "planning failed" in (task.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "plan.validation.failed" in kinds
    assert "capability.executed" not in kinds  # nothing ever ran


def test_integration_unknown_capability_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["capability"] = "shell.exec"
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "not registered" in (task.error or "")


def test_integration_unknown_action_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["action"] = "delete"
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "not provided" in (task.error or "")


def test_integration_wrong_param_type_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["params"] = {"path": 123}
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "must be of type" in (task.error or "")


def test_integration_missing_required_param_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["params"] = {}
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "requires parameter 'path'" in (task.error or "")


def test_integration_resource_param_smuggling_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][1]["params"] = {"Path": "README.md"}
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "requires parameter 'path'" in (task.error or "")


def test_integration_resource_outside_boundary_denied_by_authorization(sandbox, db_path):
    """Schema+validator pass; the authorization policy denies the resource."""
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["steps"][1]["params"] = {"path": "../outside.txt"}
    policy = _fs_policy()
    engine = _build_engine(db_path, sandbox, FakeModelRouter(plan), policy=policy)

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "outside boundary" in (task.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "permission.denied" in kinds
    assert "plan.validation.passed" in kinds  # validation is not authorization


def test_integration_path_prefix_denial(sandbox, db_path):
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["steps"][1]["params"] = {"path": "notes.txt"}
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    engine = _build_engine(db_path, sandbox, FakeModelRouter(plan), policy=policy)

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "outside boundary" in (task.error or "")


def test_integration_injected_arbitrary_args_rejected(sandbox, db_path):
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["steps"][0]["params"] = {"path": ".", "chmod": "777"}
    engine = _build_engine(db_path, sandbox, FakeModelRouter(bad))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.FAILED
    assert "arbitrary tool arguments" in (task.error or "")


def test_deterministic_planner_still_functions_with_zero_llm(sandbox, db_path):
    """Regression (ADR-008): the deterministic planner + router work with no model."""
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = DeterministicPlanner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=EventLogger(sinks=[storage]),
        policy=_fs_policy(),
    )
    task = engine.execute_goal("summarize this repository")
    assert task.status == TaskStatus.COMPLETED


def test_deterministic_structured_path_through_real_model_planner(sandbox, db_path):
    """RealModelPlanner over the deterministic router: the structured pipeline
    (schema -> validator) works end-to-end with zero LLM access."""
    router = DeterministicRouter(DeterministicPlanner())
    engine = _build_engine(db_path, sandbox, router)

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.COMPLETED
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "plan.validation.passed" in kinds
