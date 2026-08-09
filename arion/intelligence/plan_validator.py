"""PlanValidator: the validation boundary between intelligence and orchestration (ADR-011).

The validator checks a PlanSchema against the LIVE capability registry before
any step is executed:

- capability exists and action exists (with the action's real metadata);
- parameters satisfy the action's declared param_schema (required keys, types,
  no unknown/injected arguments);
- resource-bearing actions declare their resource parameter and the resource
  is present and well-formed; the plan cannot redefine the resource kind;
- verification specifications are valid.

Security: the validator NEVER grants permissions. It resolves each step's
scope/risk/side effects from the ActionSpec (the registry is authoritative),
and it performs no boundary checks - boundary enforcement belongs exclusively
to PermissionPolicy during authorization. Malformed or impossible plans are
rejected before execution.
"""

from __future__ import annotations

from typing import Any

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.plan_schema import PlanSchema, PlanValidationError, StructuredStep
from arion.state.models import PlanStep


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected in ("list", "array"):
        return isinstance(value, list)
    if expected in ("object", "dict"):
        return isinstance(value, dict)
    return True  # unknown declared type: do not reject on type grounds


class PlanValidator:
    """Validates a structured plan against the capability registry."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def validate(self, schema: PlanSchema) -> list[PlanStep]:
        """Validate the schema and convert it to executable PlanSteps.

        Raises PlanValidationError on the first invalid step. Scope and
        resource metadata come from the registry's ActionSpec - never from the
        model.
        """
        steps: list[PlanStep] = []
        for i, s in enumerate(schema.steps):
            spec = self.registry.action_spec(s.capability, s.action)
            if spec is None:
                if self.registry.get(s.capability) is None:
                    raise PlanValidationError(
                        f"step {i}: capability {s.capability!r} is not registered "
                        f"(registered: {self.registry.list()})"
                    )
                raise PlanValidationError(
                    f"step {i}: action {s.action!r} is not provided by capability {s.capability!r}"
                )
            self._validate_params(i, spec, s.params)
            self._validate_resource(i, spec, s.params)
            steps.append(
                PlanStep(
                    index=i,
                    intent=s.intent,
                    capability=s.capability,
                    action=s.action,
                    scope=spec.required_scope,  # registry authority, not the model
                    params=dict(s.params),
                    verification=s.verification,
                )
            )
        return steps

    # ---------- internals ----------

    def _validate_params(self, step_index: int, spec: ActionSpec, params: dict[str, Any]) -> None:
        """Parameters must match the action's declared param_schema."""
        schema = spec.param_schema
        if schema is None:
            return  # action declares no param contract: nothing to check (resource still checked below)
        for key, rule in schema.items():
            rule = rule or {}
            if rule.get("required") and key not in params:
                raise PlanValidationError(
                    f"step {step_index}: action {spec.name!r} requires parameter {key!r} (missing from params)"
                )
        for key, value in params.items():
            if key not in schema:
                raise PlanValidationError(
                    f"step {step_index}: action {spec.name!r} has no parameter {key!r} - "
                    "arbitrary tool arguments are rejected"
                )
            rule = schema[key] or {}
            expected = rule.get("type")
            if expected and not _type_ok(expected, value):
                raise PlanValidationError(
                    f"step {step_index}: parameter {key!r} must be of type {expected!r} (got {type(value).__name__})"
                )

    def _validate_resource(self, step_index: int, spec: ActionSpec, params: dict[str, Any]) -> None:
        """Resource-bearing actions must carry their declared resource param.

        The resource kind and parameter come from the ActionSpec; a plan
        cannot redefine them (the schema forbids those fields entirely).
        Boundary enforcement is authorization's job, not validation's.
        """
        if not spec.resource_kind:
            return
        if not spec.resource_param:
            raise PlanValidationError(
                f"capability {spec.name!r} misconfigured: resource_kind without resource_param"
            )
        value = params.get(spec.resource_param)
        if not isinstance(value, str) or not value:
            raise PlanValidationError(
                f"step {step_index}: action {spec.name!r} requires resource parameter "
                f"{spec.resource_param!r} (a non-empty string)"
            )
