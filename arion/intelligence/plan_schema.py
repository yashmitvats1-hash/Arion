"""Versioned, strict schema for model-produced plans (ADR-011).

The model proposes; the system remains the authority. This schema is the ONLY
shape a structured plan may take when crossing from intelligence to
orchestration:

- It is versioned (PLAN_SCHEMA_VERSION) and serializable/persistable.
- It contains intent, ordered steps, capability, action, parameters,
  verification requirements, and optional step dependencies.
- It does NOT contain authorization fields (scope, resource_kind, risk,
  side effects, ...). Those are resolved by the system from the capability
  registry. Any attempt by a model to set them is rejected here, before the
  plan reaches execution.
- Parsing is strict: unknown top-level or step fields are rejected (with a
  specific message when the field is a forbidden authorization field).

The PlanValidator (plan_validator.py) then validates capability/action/
parameter/resource compatibility against the registry before execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from arion.state.models import VerificationPolicy

PLAN_SCHEMA_VERSION = "1.0"

# Allowed verification policies (mirrors the engine's _verify implementation).
VERIFICATION_POLICIES = ("non_empty", "schema_keys")

# Allowed top-level keys and per-step keys.
TOP_LEVEL_KEYS = frozenset({"version", "intent", "steps"})
STEP_KEYS = frozenset({"intent", "capability", "action", "params", "verification", "depends_on"})

# Fields the model must NEVER set: the system resolves these from the
# capability registry and the authorization layer. Rejected with a clear error.
FORBIDDEN_STEP_FIELDS = frozenset(
    {
        "scope",
        "resource_kind",
        "resource_param",
        "risk",
        "side_effects",
        "idempotent",
        "retry_safe",
        "reversible",
        "permissions",
        "actor",
        "approve",
        "grant",
        "authorization",
        "boundary",
        "allowed",
    }
)

# Parameter keys a model must never inject (they would collide with metadata
# that is resolved by the system).
RESERVED_PARAM_KEYS = FORBIDDEN_STEP_FIELDS


class PlanValidationError(ValueError):
    """Raised when a plan is structurally invalid or incompatible with the
    capability registry. Never grants permissions."""


def validate_verification_spec(policy: Any, args: Any, where: str = "verification") -> VerificationPolicy:
    """Validate a verification specification, returning a VerificationPolicy."""
    if not isinstance(policy, str) or policy not in VERIFICATION_POLICIES:
        raise PlanValidationError(f"{where}: invalid verification policy {policy!r} (allowed: {VERIFICATION_POLICIES})")
    if not isinstance(args, dict):
        raise PlanValidationError(f"{where}: verification args must be an object")
    if policy == "schema_keys":
        keys = args.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise PlanValidationError(f"{where}: policy 'schema_keys' requires args.keys as a list of strings")
    return VerificationPolicy(policy=policy, args=dict(args))


@dataclass
class StructuredStep:
    """One step of a structured plan (model-proposed, system-validated)."""

    intent: str
    capability: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    verification: VerificationPolicy = field(default_factory=lambda: VerificationPolicy("non_empty"))
    depends_on: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "intent": self.intent,
            "capability": self.capability,
            "action": self.action,
            "params": self.params,
            "verification": {"policy": self.verification.policy, "args": self.verification.args},
        }
        if self.depends_on:
            d["depends_on"] = self.depends_on
        return d

    @classmethod
    def from_dict(cls, d: Any, index: int) -> "StructuredStep":
        if not isinstance(d, dict):
            raise PlanValidationError(f"step {index}: must be a JSON object")
        unknown = set(d) - STEP_KEYS
        if unknown:
            forbidden = sorted(unknown & FORBIDDEN_STEP_FIELDS)
            if forbidden:
                raise PlanValidationError(
                    f"step {index}: model cannot set field(s) {forbidden} - "
                    "resolved by the system from the capability registry"
                )
            raise PlanValidationError(f"step {index}: unknown field(s) {sorted(unknown)}")

        intent = d.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise PlanValidationError(f"step {index}: 'intent' must be a non-empty string")
        capability = d.get("capability")
        if not isinstance(capability, str) or not capability.strip():
            raise PlanValidationError(f"step {index}: 'capability' must be a non-empty string")
        action = d.get("action")
        if not isinstance(action, str) or not action.strip():
            raise PlanValidationError(f"step {index}: 'action' must be a non-empty string")

        params = d.get("params", {})
        if not isinstance(params, dict):
            raise PlanValidationError(f"step {index}: 'params' must be a JSON object")
        reserved = sorted(set(params) & RESERVED_PARAM_KEYS)
        if reserved:
            raise PlanValidationError(
                f"step {index}: params cannot contain reserved key(s) {reserved} - resolved by the system"
            )

        verification_raw = d.get("verification")
        if not isinstance(verification_raw, dict):
            raise PlanValidationError(f"step {index}: 'verification' is required and must be an object")
        verification = validate_verification_spec(
            verification_raw.get("policy"), verification_raw.get("args", {}), where=f"step {index} verification"
        )

        depends_on = d.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in depends_on
        ):
            raise PlanValidationError(f"step {index}: 'depends_on' must be a list of integers")
        if len(set(depends_on)) != len(depends_on):
            raise PlanValidationError(f"step {index}: 'depends_on' contains duplicates")
        for ref in depends_on:
            if ref < 0 or ref >= index:
                raise PlanValidationError(f"step {index}: 'depends_on' may only reference earlier steps (got {ref})")

        return cls(
            intent=intent,
            capability=capability,
            action=action,
            params=dict(params),
            verification=verification,
            depends_on=list(depends_on),
        )


@dataclass
class PlanSchema:
    """A versioned, validated structured plan (model-proposed, system-approved)."""

    version: str
    intent: str
    steps: list[StructuredStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "intent": self.intent,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: Any) -> "PlanSchema":
        if not isinstance(d, dict):
            raise PlanValidationError("plan must be a JSON object")
        unknown = set(d) - TOP_LEVEL_KEYS
        if unknown:
            raise PlanValidationError(f"plan: unknown top-level field(s) {sorted(unknown)}")

        version = d.get("version")
        if version != PLAN_SCHEMA_VERSION:
            raise PlanValidationError(
                f"plan: unsupported schema version {version!r} (expected {PLAN_SCHEMA_VERSION!r})"
            )
        intent = d.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise PlanValidationError("plan: 'intent' must be a non-empty string")

        steps_raw = d.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise PlanValidationError("plan: 'steps' must be a non-empty array")

        steps = [StructuredStep.from_dict(s, i) for i, s in enumerate(steps_raw)]
        return cls(version=version, intent=intent, steps=steps)

    @classmethod
    def from_json(cls, text: str) -> "PlanSchema":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanValidationError(f"plan: malformed JSON: {exc}") from exc
        return cls.from_dict(obj)
