"""PlanValidator: the validation boundary between intelligence and orchestration (ADR-011).

The validator checks a PlanSchema against the LIVE capability registry before
any step is executed:

- capability exists and action exists (with the action's real metadata);
- parameters satisfy the action's declared param_schema (required keys, types,
  no unknown/injected arguments);
- resource-bearing actions declare their resource parameter and the resource
  is present and well-formed; the plan cannot redefine the resource kind;
- verification specifications are valid;
- dependencies are valid (in range, no self-reference, no cycles) and steps
  are ordered for execution (topological order; stable = array order for
  dependency-free plans).

Security: the validator NEVER grants permissions. It resolves each step's
scope/risk/side effects from the ActionSpec (the registry is authoritative),
and it performs no boundary checks - boundary enforcement belongs exclusively
to PermissionPolicy during authorization. Malformed or impossible plans are
rejected before execution.

Typed errors: capability/parameter/resource/ordering failures raise
PlanCapabilityValidationError so callers and audit events can distinguish them
from schema failures (PlanSchemaValidationError).
"""

from __future__ import annotations

from typing import Any

from arion.capabilities.registry import (
    ActionSpec,
    CapabilityRegistry,
    VerificationResolutionError,
    is_mutating,
    resolve_verification_policy,
)
from arion.intelligence.errors import PlanCapabilityValidationError
from arion.intelligence.plan_schema import PlanSchema, PlanValidationError, StructuredStep
from arion.state.models import PlanStep, VerificationPolicy


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


def topo_sort_steps(steps: list[PlanStep]) -> list[PlanStep]:
    """Order steps so every step comes after all its dependencies.

    - Rejects out-of-range / non-integer / self-references (PlanValidationError).
    - Detects and rejects cycles (PlanValidationError).
    - Stable: for dependency-free plans (and any plan whose dependencies only
      reference earlier indices) the order is exactly the array order, so
      deterministic sequential behavior is preserved.
    """
    import heapq

    n = len(steps)
    indegree = [0] * n
    dependents: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, step in enumerate(steps):
        for ref in step.depends_on:
            if not isinstance(ref, int) or isinstance(ref, bool):
                raise PlanValidationError(f"step {i}: depends_on entries must be integers")
            if ref < 0 or ref >= n:
                raise PlanValidationError(f"step {i}: depends_on references out-of-range step {ref}")
            if ref == i:
                raise PlanValidationError(f"step {i}: cannot depend on itself")
            indegree[i] += 1
            dependents[ref].append(i)

    ready = [i for i in range(n) if indegree[i] == 0]
    heapq.heapify(ready)  # index tie-break -> stable, deterministic order
    order: list[int] = []
    while ready:
        i = heapq.heappop(ready)
        order.append(i)
        for j in sorted(dependents[i]):
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(ready, j)

    if len(order) < n:
        cyclic = sorted(set(range(n)) - set(order))
        raise PlanValidationError(f"dependency cycle detected involving step(s) {cyclic}")
    return [steps[i] for i in order]


class PlanValidator:
    """Validates a structured plan against the capability registry."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def validate(self, schema: PlanSchema) -> list[PlanStep]:
        """Validate the schema and convert it to executable PlanSteps.

        Raises PlanValidationError (typed subclass) on the first invalid step.
        Scope and resource metadata come from the registry's ActionSpec - never
        from the model. Returns steps in dependency-safe execution order.
        """
        steps: list[PlanStep] = []
        for i, s in enumerate(schema.steps):
            spec = self.registry.action_spec(s.capability, s.action)
            if spec is None:
                if self.registry.get(s.capability) is None:
                    raise PlanCapabilityValidationError(
                        f"step {i}: capability {s.capability!r} is not registered "
                        f"(registered: {self.registry.list()})"
                    )
                raise PlanCapabilityValidationError(
                    f"step {i}: action {s.action!r} is not provided by capability {s.capability!r}"
                )
            self._validate_params(i, spec, s.params)
            self._validate_resource(i, spec, s.params)
            # ADR-060 D4: registry-authoritative verification, applied here as
            # a SECONDARY early pass for model plans. The engine repeats this
            # in `_execute_step` and is the authority - DeterministicPlanner
            # and stored-plan rehydration never reach this code.
            verification, guidance = self._normalize_verification(i, spec, s.verification)
            steps.append(
                PlanStep(
                    index=i,
                    intent=s.intent,
                    capability=s.capability,
                    action=s.action,
                    scope=spec.required_scope,  # registry authority, not the model
                    params=dict(s.params),
                    verification=verification,
                    depends_on=list(s.depends_on),
                    guidance=guidance,
                )
            )
        return topo_sort_steps(steps)

    # ---------- internals ----------

    def _normalize_verification(
        self, step_index: int, spec: ActionSpec, requested
    ) -> tuple[VerificationPolicy, list[dict[str, Any]]]:
        """Registry-authoritative verification for a model-proposed step.

        ADR-060 D4: a model does not control - and must not be responsible
        for - the reliability invariant of a mutation, exactly as it does not
        control `scope`. A weaker proposed policy is deterministically
        UPGRADED rather than rejected, and the original request is preserved
        as provenance so "the model asked for X, why did Arion run Y?" stays
        answerable.

        A mutating action with no usable policy is rejected here (fail
        closed); the engine independently reaches the same conclusion.
        """
        requested_policy = getattr(requested, "policy", None)
        requested_args = dict(getattr(requested, "args", {}) or {})
        try:
            policy, args, authority = resolve_verification_policy(spec, requested)
        except VerificationResolutionError as exc:
            raise PlanCapabilityValidationError(f"step {step_index}: {exc}") from None

        if policy == requested_policy and args == requested_args:
            return VerificationPolicy(policy=policy, args=args), []
        return (
            VerificationPolicy(policy=policy, args=args),
            [{
                "kind": "verification_normalized",
                "capability": spec.name,
                "action": spec.name,
                "requested": requested_policy,
                "applied": policy,
                "authority": authority,
                "mutating": is_mutating(spec),
            }],
        )

    def _validate_params(self, step_index: int, spec: ActionSpec, params: dict[str, Any]) -> None:
        """Parameters must match the action's declared param_schema."""
        schema = spec.param_schema
        if schema is None:
            return  # action declares no param contract: nothing to check (resource still checked below)
        for key, rule in schema.items():
            rule = rule or {}
            if rule.get("required") and key not in params:
                raise PlanCapabilityValidationError(
                    f"step {step_index}: action {spec.name!r} requires parameter {key!r} (missing from params)"
                )
        for key, value in params.items():
            if key not in schema:
                raise PlanCapabilityValidationError(
                    f"step {step_index}: action {spec.name!r} has no parameter {key!r} - "
                    "arbitrary tool arguments are rejected"
                )
            rule = schema[key] or {}
            expected = rule.get("type")
            if expected and not _type_ok(expected, value):
                raise PlanCapabilityValidationError(
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
            raise PlanCapabilityValidationError(
                f"capability {spec.name!r} misconfigured: resource_kind without resource_param"
            )
        value = params.get(spec.resource_param)
        if not isinstance(value, str) or not value:
            raise PlanCapabilityValidationError(
                f"step {step_index}: action {spec.name!r} requires resource parameter "
                f"{spec.resource_param!r} (a non-empty string)"
            )
