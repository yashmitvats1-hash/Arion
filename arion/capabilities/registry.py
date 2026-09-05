"""Capability layer: capabilities, permissions and the registry.

A capability is a capability only if it is safe to call from the agent loop.
Every capability:

- declares the permission scope(s) it needs (e.g. "filesystem:read");
- is self-describing (name, description, actions) for discovery;
- returns structured observations;
- raises CapabilityError on failure.

No capability may perform privileged action implicitly - permission checking
lives in the orchestration layer (ADR-006).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class CapabilityError(Exception):
    """Raised when a capability fails to execute its action."""


@dataclass
class ActionSpec:
    """Declarative metadata for one capability action (ADR-009).

    - required_scope: the permission scope the orchestrator authorizes against
      (source of truth - the engine never trusts a plan's claimed scope).
    - risk: none | low | medium | high - feeds the policy decision.
    - side_effects: none | read_only | mutating | irreversible - what the
      action does to the world.
    - reversible / idempotent / retry_safe: execution-semantics metadata
      (ADR-010). retry_safe=False means a failure must NOT be automatically
      retried (the operation may have partially applied).
    - resource_kind / resource_param: if the action targets a resource, the
      kind ("filesystem:path", future "url", "queue:name", ...) and the params
      key that holds the resource identifier. The policy requires an explicit
      boundary per resource kind (fail closed, ADR-009).
    - param_schema: declared parameter contract {"name": {"type": "...",
      "required": bool}} used by the PlanValidator to reject missing, wrong-typed
      or arbitrary injected parameters from model-produced plans (ADR-011).
    - default_verification: suggested verification spec shown to planners in
      capability discovery ({"policy": ..., "args": {...}}).
    - security_relevant_params: param NAMES (besides resource_param) whose
      VALUES must be part of the canonical authorization fingerprint (ADR-018,
      Phase D). The resource parameter is always fingerprinted via `resource`.
      Operational parameters (limits, formatting, ...) are NOT fingerprinted
      unless declared here.
    """

    name: str
    description: str
    required_scope: str
    params: dict[str, Any] = field(default_factory=dict)
    risk: str = "low"
    side_effects: str = "read_only"
    reversible: bool = True
    idempotent: bool = True
    retry_safe: bool = True
    resource_kind: str | None = None
    resource_param: str | None = None
    param_schema: dict[str, dict[str, Any]] | None = None
    default_verification: dict[str, Any] | None = None
    security_relevant_params: list[str] = field(default_factory=list)
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required_scope": self.required_scope,
            "params": self.params,
            "risk": self.risk,
            "side_effects": self.side_effects,
            "reversible": self.reversible,
            "idempotent": self.idempotent,
            "retry_safe": self.retry_safe,
            "resource_kind": self.resource_kind,
            "resource_param": self.resource_param,
            "param_schema": self.param_schema,
            "default_verification": self.default_verification,
            "security_relevant_params": list(self.security_relevant_params),
        }


class Capability(Protocol):
    """Contract every capability implements."""

    name: str
    description: str
    actions: list[ActionSpec]

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute an action with the given params, returning a structured observation."""
        ...


class CapabilityRegistry:
    """Discovers capabilities by name and provides introspection for planning."""

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        self._caps[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def has(self, name: str) -> bool:
        return name in self._caps

    def list(self) -> list[str]:
        return sorted(self._caps)

    def action_spec(self, capability: str, action: str) -> ActionSpec | None:
        cap = self._caps.get(capability)
        if cap is None:
            return None
        for a in cap.actions:
            if a.name == action:
                return a
        return None

    def capabilities_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "name": cap.name,
                "description": cap.description,
                "actions": [a.to_dict() for a in cap.actions],
            }
            for cap in sorted(self._caps.values(), key=lambda c: c.name)
        ]


# ---------------------------------------------------------------------------
# Verification policy resolution (ADR-060 / M7-A, D4+D5)
# ---------------------------------------------------------------------------

# Verification policies this engine build knows how to evaluate. A policy name
# outside this set is "unknown": `_verify` fails closed on it at execution
# time, and D5 refuses a MUTATING step carrying one before it ever runs.
KNOWN_VERIFICATION_POLICIES: frozenset[str] = frozenset({
    "non_empty",
    "schema_keys",
    "write_verified",
    "append_verified",
})

# The historical rehydration default (`PlanStep.from_dict`). Retained for
# READ-ONLY steps only: ADR-060 §3 established that no Arion-written plan
# actually omits verification, so this is defensive rather than load-bearing.
HISTORICAL_DEFAULT_POLICY = "non_empty"


def is_mutating(spec: ActionSpec) -> bool:
    """Whether an action changes the world (ADR-006 side-effect taxonomy)."""
    return spec.side_effects in ("mutating", "irreversible")


class VerificationResolutionError(CapabilityError):
    """A mutating action has no usable verification policy (fail closed)."""


def resolve_verification_policy(
    spec: ActionSpec,
    requested: "VerificationSpec | None",
) -> tuple[str, dict[str, Any], str]:
    """Decide the AUTHORITATIVE verification policy for one step.

    Returns `(policy, args, authority)` where `authority` is one of
    "explicit" | "registry" | "historical_default".

    ADR-060 D4: for a MUTATING action the registry's `default_verification`
    outranks whatever the plan proposed - a model does not control, and must
    not be responsible for, a reliability invariant of a mutation. This
    mirrors the existing `scope` precedent ("registry authority, not the
    model").

    ADR-060 D5: four cases, deliberately NOT collapsed --

      explicit known policy   -> honoured (both mutating and read-only)
      registry default        -> applied
      missing verification    -> mutating: FAIL CLOSED; read-only: historical
      unknown policy          -> mutating: FAIL CLOSED; read-only: left alone
                                 so `_verify`'s existing else-branch fails it

    A mutating action with NEITHER a registry default NOR an explicit known
    policy is refused with a precise diagnostic rather than silently executed
    under shape-only verification. Honouring an explicit known policy is what
    keeps a custom mutating capability that declares no default from becoming
    permanently unexecutable.
    """
    default = spec.default_verification or None
    default_policy = None
    default_args: dict[str, Any] = {}
    if isinstance(default, dict):
        candidate = default.get("policy")
        if isinstance(candidate, str) and candidate:
            default_policy = candidate
            default_args = dict(default.get("args") or {})

    req_policy = getattr(requested, "policy", None) if requested is not None else None
    req_args = dict(getattr(requested, "args", {}) or {}) if requested is not None else {}
    req_missing = not isinstance(req_policy, str) or not req_policy

    if not is_mutating(spec):
        # Read-only: the historical model is untouched. An unknown policy is
        # left in place so `_verify` fails it closed at execution time.
        if req_missing:
            if default_policy is not None:
                return default_policy, default_args, "registry"
            return HISTORICAL_DEFAULT_POLICY, {}, "historical_default"
        return req_policy, req_args, "explicit"

    # Mutating from here on (ADR-060 D4/D5).
    if default_policy is not None:
        return default_policy, default_args, "registry"
    if not req_missing and req_policy in KNOWN_VERIFICATION_POLICIES:
        return req_policy, req_args, "explicit"

    reason = (
        "carries no verification policy"
        if req_missing
        else f"requests unknown verification policy {req_policy!r}"
    )
    raise VerificationResolutionError(
        f"mutating action {spec.name!r} {reason} and its ActionSpec declares no "
        f"default_verification; refusing to execute a mutation under "
        f"shape-only verification (ADR-060 D5, fail closed)"
    )
