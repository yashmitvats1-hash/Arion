"""PlanValidator tests (ADR-011): capability/action/param/resource validity.

The validator is the second gate (after the schema): it checks a structurally
valid PlanSchema against the LIVE capability registry and rejects impossible
plans before execution. It never grants permissions.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema, PlanValidationError
from arion.intelligence.plan_validator import PlanValidator
from arion.state.models import PlanStep


def _registry(sandbox):
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(sandbox))
    return reg


def _schema(steps):
    return PlanSchema(version=PLAN_SCHEMA_VERSION, intent="test", steps=steps)


def _step(capability="filesystem.read", action="read", params=None, verification=None):
    from arion.intelligence.plan_schema import StructuredStep
    from arion.state.models import VerificationPolicy

    return StructuredStep(
        intent="step",
        capability=capability,
        action=action,
        params=params if params is not None else {"path": "README.md"},
        verification=verification if verification is not None else VerificationPolicy("non_empty"),
    )


def test_valid_plan_returns_steps_with_registry_authority(sandbox):
    validator = PlanValidator(_registry(sandbox))
    steps = validator.validate(
        _schema([
            _step(action="read"),
            _step(action="list", params={"path": "."}, verification=None),
        ])
    )
    assert len(steps) == 2
    assert isinstance(steps[0], PlanStep)
    # scope is resolved from the registry's ActionSpec - the model never set it
    assert steps[0].scope == "filesystem:read"
    assert steps[0].params == {"path": "README.md"}
    assert steps[1].index == 1


def test_unknown_capability_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="not registered"):
        validator.validate(_schema([_step(capability="shell.exec", action="exec")]))


def test_unknown_action_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="not provided"):
        validator.validate(_schema([_step(action="delete")]))


def test_missing_required_parameter_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="requires parameter 'path'"):
        validator.validate(_schema([_step(params={})]))


def test_wrong_parameter_type_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="must be of type"):
        validator.validate(_schema([_step(params={"path": 123})]))


def test_arbitrary_injected_arguments_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="arbitrary tool arguments"):
        validator.validate(_schema([_step(params={"path": "README.md", "rm": True})]))


def test_resource_param_required_for_resource_actions(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="requires parameter 'path'"):
        validator.validate(_schema([_step(params={"filename": "README.md"})]))


def test_resource_kind_is_never_taken_from_plan(sandbox):
    """The plan schema forbids resource_kind entirely; the validator uses the
    ActionSpec's declared kind, so the model cannot redefine it."""
    validator = PlanValidator(_registry(sandbox))
    steps = validator.validate(_schema([_step()]))
    # sanity: the action's declared resource metadata is what governs
    spec = _registry(sandbox).action_spec("filesystem.read", "read")
    assert spec.resource_kind == "filesystem:path"
    assert spec.resource_param == "path"
    assert steps[0].params["path"] == "README.md"


def test_resource_smuggling_under_other_key_rejected(sandbox):
    validator = PlanValidator(_registry(sandbox))
    with pytest.raises(PlanValidationError, match="requires parameter 'path'"):
        validator.validate(_schema([_step(params={"Path": "README.md"})]))


def test_validator_does_not_touch_permissions(sandbox):
    """The validator never grants anything: it returns steps whose scope comes
    from the registry; authorization is the policy's job (engine-level)."""
    validator = PlanValidator(_registry(sandbox))
    steps = validator.validate(_schema([_step()]))
    assert steps[0].scope == "filesystem:read"
    # no permission-related fields exist on PlanStep beyond the declared scope
    assert not hasattr(steps[0], "approved")


def test_custom_capability_with_param_schema(sandbox):
    from arion.intelligence.plan_schema import StructuredStep

    class Tool:
        name = "calc.add"
        description = "add numbers"
        actions = [
            ActionSpec(name="add", description="add", required_scope="calc:run",
                       risk="low", side_effects="read_only", retry_safe=True,
                       param_schema={"a": {"type": "integer", "required": True},
                                     "b": {"type": "integer", "required": True}})
        ]

        def execute(self, action, params):
            return {"sum": params["a"] + params["b"]}

    reg = _registry(sandbox)
    reg.register(Tool())
    validator = PlanValidator(reg)

    ok = validator.validate(_schema([
        StructuredStep(intent="add", capability="calc.add", action="add",
                       params={"a": 1, "b": 2})
    ]))
    assert ok[0].scope == "calc:run"

    with pytest.raises(PlanValidationError, match="requires parameter 'a'"):
        validator.validate(_schema([_step(capability="calc.add", action="add", params={"b": 2})]))
    with pytest.raises(PlanValidationError, match="must be of type"):
        validator.validate(_schema([_step(capability="calc.add", action="add", params={"a": "x", "b": 2})]))
    with pytest.raises(PlanValidationError, match="arbitrary tool arguments"):
        validator.validate(_schema([_step(capability="calc.add", action="add", params={"a": 1, "b": 2, "c": 3})]))
