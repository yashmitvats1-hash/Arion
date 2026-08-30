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
- Parsing is bounded (ADR-057 D2, M2): raw response size, JSON nesting
  depth, step count, params count, and step-level string lengths are capped
  before a plan can progress (see MAX_* constants below).

The PlanValidator (plan_validator.py) then validates capability/action/
parameter/resource compatibility against the registry before execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from arion.intelligence.errors import (
    PlanSchemaValidationError,
    PlanValidationError,  # re-exported for compatibility (base class)
)
from arion.state.models import VerificationPolicy

PLAN_SCHEMA_VERSION = "1.0"

# Deterministic resource bounds for model-produced plans (ADR-057 D2; M2).
# Provider output is UNTRUSTED input: these caps bound the shape of any plan
# BEFORE it can progress toward validation, authorization, or persistence.
# Enforced at parse time (from_dict) and at the provider router before
# parsing (raw response size / raw nesting depth). Values are the ADR-057
# proposed defaults, made effective by M2.
MAX_MODEL_RESPONSE_BYTES = 262_144   # raw provider response body, bytes
MAX_JSON_DEPTH = 10                  # maximum container nesting depth
MAX_PLAN_STEPS = 100                 # maximum steps per plan
MAX_PARAMS_PER_STEP = 32             # maximum params keys per step
MAX_STEP_STRING = 2000               # maximum chars per step-level string

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


def validate_verification_spec(policy: Any, args: Any, where: str = "verification") -> VerificationPolicy:
    """Validate a verification specification, returning a VerificationPolicy."""
    if not isinstance(policy, str) or policy not in VERIFICATION_POLICIES:
        raise PlanSchemaValidationError(f"{where}: invalid verification policy {policy!r} (allowed: {VERIFICATION_POLICIES})")
    if not isinstance(args, dict):
        raise PlanSchemaValidationError(f"{where}: verification args must be an object")
    if policy == "schema_keys":
        keys = args.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise PlanSchemaValidationError(f"{where}: policy 'schema_keys' requires args.keys as a list of strings")
    return VerificationPolicy(policy=policy, args=dict(args))


def json_depth(value: Any) -> int:
    """Maximum container nesting depth of a parsed JSON value (iterative).

    Top-level containers count as depth 1; scalar values yield 0. Iterative
    (explicit stack, no recursion) so pathologically nested values cannot
    overflow the Python stack.
    """
    max_depth = 0
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if depth > max_depth:
                max_depth = depth
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            if depth > max_depth:
                max_depth = depth
            stack.extend((child, depth + 1) for child in node)
    return max_depth


def json_text_depth(text: str) -> int:
    """Maximum container nesting depth of raw JSON text (iterative, no
    recursion; string-literal aware).

    Matches `json_depth` for any document that parses. Used to reject
    pathological nesting BEFORE `json.loads`, which would otherwise raise
    RecursionError on very deep documents. '{' and '[' each increase the
    depth, '}' and ']' decrease it; string literals (with backslash escapes)
    are skipped entirely so brackets inside strings do not count. A single
    pass, bounded by the raw response size cap.
    """
    max_depth = 0
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch == "}" or ch == "]":
            depth -= 1
    return max_depth


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
    def from_dict(
        cls,
        d: Any,
        index: int,
        *,
        max_params_per_step: int = MAX_PARAMS_PER_STEP,
        max_step_string: int = MAX_STEP_STRING,
    ) -> "StructuredStep":
        if not isinstance(d, dict):
            raise PlanSchemaValidationError(f"step {index}: must be a JSON object")
        unknown = set(d) - STEP_KEYS
        if unknown:
            forbidden = sorted(unknown & FORBIDDEN_STEP_FIELDS)
            if forbidden:
                raise PlanSchemaValidationError(
                    f"step {index}: model cannot set field(s) {forbidden} - "
                    "resolved by the system from the capability registry"
                )
            raise PlanSchemaValidationError(f"step {index}: unknown field(s) {sorted(unknown)}")

        intent = d.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise PlanSchemaValidationError(f"step {index}: 'intent' must be a non-empty string")
        if len(intent) > max_step_string:
            raise PlanSchemaValidationError(
                f"step {index}: 'intent' exceeds maximum length ({len(intent)} > {max_step_string})"
            )
        capability = d.get("capability")
        if not isinstance(capability, str) or not capability.strip():
            raise PlanSchemaValidationError(f"step {index}: 'capability' must be a non-empty string")
        if len(capability) > max_step_string:
            raise PlanSchemaValidationError(
                f"step {index}: 'capability' exceeds maximum length ({len(capability)} > {max_step_string})"
            )
        action = d.get("action")
        if not isinstance(action, str) or not action.strip():
            raise PlanSchemaValidationError(f"step {index}: 'action' must be a non-empty string")
        if len(action) > max_step_string:
            raise PlanSchemaValidationError(
                f"step {index}: 'action' exceeds maximum length ({len(action)} > {max_step_string})"
            )

        params = d.get("params", {})
        if not isinstance(params, dict):
            raise PlanSchemaValidationError(f"step {index}: 'params' must be a JSON object")
        if len(params) > max_params_per_step:
            raise PlanSchemaValidationError(
                f"step {index}: 'params' exceeds maximum size ({len(params)} > {max_params_per_step})"
            )
        reserved = sorted(set(params) & RESERVED_PARAM_KEYS)
        if reserved:
            raise PlanSchemaValidationError(
                f"step {index}: params cannot contain reserved key(s) {reserved} - resolved by the system"
            )
        # Individual param keys and string values are bounded too: a
        # step-shaped plan must not smuggle arbitrarily large strings
        # (ADR-057 D2; MAX_STEP_STRING).
        for key, value in params.items():
            if len(key) > max_step_string:
                raise PlanSchemaValidationError(
                    f"step {index}: param key exceeds maximum length ({len(key)} > {max_step_string})"
                )
            if isinstance(value, str) and len(value) > max_step_string:
                raise PlanSchemaValidationError(
                    f"step {index}: param {key!r} exceeds maximum length ({len(value)} > {max_step_string})"
                )

        verification_raw = d.get("verification")
        if not isinstance(verification_raw, dict):
            raise PlanSchemaValidationError(f"step {index}: 'verification' is required and must be an object")
        verification = validate_verification_spec(
            verification_raw.get("policy"), verification_raw.get("args", {}), where=f"step {index} verification"
        )
        if verification.policy == "schema_keys":
            for key in verification.args.get("keys", []):
                if isinstance(key, str) and len(key) > max_step_string:
                    raise PlanSchemaValidationError(
                        f"step {index}: verification key exceeds maximum length ({len(key)} > {max_step_string})"
                    )

        depends_on = d.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in depends_on
        ):
            raise PlanSchemaValidationError(f"step {index}: 'depends_on' must be a list of integers")
        if len(set(depends_on)) != len(depends_on):
            raise PlanSchemaValidationError(f"step {index}: 'depends_on' contains duplicates")
        for ref in depends_on:
            if ref < 0 or ref >= index:
                raise PlanSchemaValidationError(f"step {index}: 'depends_on' may only reference earlier steps (got {ref})")

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
    def from_dict(
        cls,
        d: Any,
        *,
        max_steps: int = MAX_PLAN_STEPS,
        max_params_per_step: int = MAX_PARAMS_PER_STEP,
        max_step_string: int = MAX_STEP_STRING,
        max_json_depth: int = MAX_JSON_DEPTH,
    ) -> "PlanSchema":
        if not isinstance(d, dict):
            raise PlanSchemaValidationError("plan must be a JSON object")
        depth = json_depth(d)
        if depth > max_json_depth:
            raise PlanSchemaValidationError(
                f"plan: JSON nesting depth exceeds maximum ({depth} > {max_json_depth})"
            )
        unknown = set(d) - TOP_LEVEL_KEYS
        if unknown:
            raise PlanSchemaValidationError(f"plan: unknown top-level field(s) {sorted(unknown)}")

        version = d.get("version")
        if version != PLAN_SCHEMA_VERSION:
            raise PlanSchemaValidationError(
                f"plan: unsupported schema version {version!r} (expected {PLAN_SCHEMA_VERSION!r})"
            )
        intent = d.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise PlanSchemaValidationError("plan: 'intent' must be a non-empty string")
        if len(intent) > max_step_string:
            raise PlanSchemaValidationError(
                f"plan: 'intent' exceeds maximum length ({len(intent)} > {max_step_string})"
            )

        steps_raw = d.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise PlanSchemaValidationError("plan: 'steps' must be a non-empty array")
        if len(steps_raw) > max_steps:
            raise PlanSchemaValidationError(
                f"plan: 'steps' exceeds maximum size ({len(steps_raw)} > {max_steps})"
            )

        steps = [
            StructuredStep.from_dict(
                s,
                i,
                max_params_per_step=max_params_per_step,
                max_step_string=max_step_string,
            )
            for i, s in enumerate(steps_raw)
        ]
        return cls(version=version, intent=intent, steps=steps)

    @classmethod
    def from_json(cls, text: str) -> "PlanSchema":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanSchemaValidationError(f"plan: malformed JSON: {exc}") from exc
        return cls.from_dict(obj)
