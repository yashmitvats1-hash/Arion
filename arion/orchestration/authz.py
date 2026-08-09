"""Resource-aware authorization layer (ADR-009).

Authorization and capability containment are SEPARATE concepts:

- The policy decides, from a structured request, whether an action may run:
  `Capability -> Action -> Resource -> Parameters -> Policy Decision`.
- The capability (e.g. the filesystem sandbox) enforces its own containment.
  Neither substitutes for the other.

Policy outcomes are `ALLOW | DENY | REQUIRE_APPROVAL`. When the policy requires
approval, the engine routes through an `ApprovalHandler` seam - a future human
approval interface plugs in there without touching the engine or the policy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Policy outcome model
# ---------------------------------------------------------------------------


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class AuthorizationRequest:
    """Everything the policy needs to decide one step."""

    agent: str                       # identity/agent context, e.g. "system", "user:alice"
    task_id: str
    step_index: int
    capability: str
    action: str
    scope: str                       # resolved from the capability's ActionSpec (never the plan's claim)
    params: dict[str, Any]
    resource: str | None             # resource the action targets (e.g. filesystem path), if any
    risk: str = "low"                # none | low | medium | high (from action metadata)
    side_effects: str = "read_only"  # none | read_only | mutating | irreversible (from metadata)
    idempotent: bool = True
    retry_safe: bool = True


@dataclass
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    scope: str
    resource: str | None = None
    risk: str = "low"
    side_effects: str = "read_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "scope": self.scope,
            "resource": self.resource,
            "risk": self.risk,
            "side_effects": self.side_effects,
        }


class PermissionPolicy(Protocol):
    """Decides whether a structured authorization request may run."""

    def decide(self, request: AuthorizationRequest) -> PolicyDecision: ...


# ---------------------------------------------------------------------------
# Approval seam
# ---------------------------------------------------------------------------


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"  # no answer yet: the task pauses and can be resumed


class ApprovalHandler(Protocol):
    """Seam for human/automated approval.

    A future approval interface (notification, GUI, queue) implements this
    protocol; the engine never changes.
    """

    def request(self, request: AuthorizationRequest, decision: PolicyDecision) -> ApprovalOutcome: ...


class PendingApprovalHandler:
    """Default: no approval interface attached - requests stay pending."""

    def request(self, request: AuthorizationRequest, decision: PolicyDecision) -> ApprovalOutcome:
        return ApprovalOutcome.PENDING


class AutoApproveHandler:
    """Test/automation helper: approve everything asked."""

    def request(self, request: AuthorizationRequest, decision: PolicyDecision) -> ApprovalOutcome:
        return ApprovalOutcome.APPROVED


class AutoDenyHandler:
    """Test/automation helper: deny everything asked."""

    def request(self, request: AuthorizationRequest, decision: PolicyDecision) -> ApprovalOutcome:
        return ApprovalOutcome.DENIED


# ---------------------------------------------------------------------------
# Default resource-aware policy
# ---------------------------------------------------------------------------


class ResourcePolicy:
    """Configurable, deterministic policy.

    Decision pipeline (first matching rule wins):
      1. agent not permitted            -> DENY
      2. scope not in allowlist         -> DENY
      3. scope in denylist              -> DENY
      4. resource outside path rules    -> DENY
      5. risk in deny set               -> DENY
      6. risk in approval set           -> REQUIRE_APPROVAL
      7. otherwise                      -> ALLOW
    """

    def __init__(
        self,
        allowed_scopes: set[str] | None = None,
        denied_scopes: set[str] | None = None,
        risk_deny: set[str] | None = None,
        risk_approve: set[str] | None = None,
        path_constraints: dict[tuple[str, str], list[str]] | None = None,
        allowed_agents: set[str] | None = None,
    ):
        self.allowed_scopes = allowed_scopes or {"filesystem:read"}
        self.denied_scopes = denied_scopes or set()
        # read-only slice: high risk is never allowed, medium risk needs approval
        self.risk_deny = risk_deny or {"high"}
        self.risk_approve = risk_approve or {"medium"}
        self.path_constraints = path_constraints or {}  # (capability, action) -> allowed path prefixes
        self.allowed_agents = allowed_agents  # None = any agent

    def decide(self, request: AuthorizationRequest) -> PolicyDecision:
        base = dict(
            scope=request.scope,
            resource=request.resource,
            risk=request.risk,
            side_effects=request.side_effects,
        )

        if self.allowed_agents is not None and request.agent not in self.allowed_agents:
            return PolicyDecision(PolicyOutcome.DENY, f"agent {request.agent!r} not permitted", **base)

        if request.scope not in self.allowed_scopes:
            return PolicyDecision(PolicyOutcome.DENY, f"scope {request.scope!r} not permitted by policy", **base)

        if request.scope in self.denied_scopes:
            return PolicyDecision(PolicyOutcome.DENY, f"scope {request.scope!r} explicitly denied", **base)

        if not self._resource_allowed(request):
            return PolicyDecision(PolicyOutcome.DENY, f"resource {request.resource!r} not permitted by policy", **base)

        if request.risk in self.risk_deny:
            return PolicyDecision(PolicyOutcome.DENY, f"risk level {request.risk!r} denied by policy", **base)

        if request.risk in self.risk_approve:
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                f"risk level {request.risk!r} requires approval",
                **base,
            )

        return PolicyDecision(PolicyOutcome.ALLOW, "allowed", **base)

    def _resource_allowed(self, request: AuthorizationRequest) -> bool:
        """Pure path check (no filesystem access) against configured prefixes."""
        prefixes = self.path_constraints.get((request.capability, request.action))
        if not prefixes:
            return True  # no constraint configured for this action
        p = request.params.get("path")
        if not isinstance(p, str) or not p:
            return False
        norm = os.path.normpath(p)
        # absolute paths and upward traversal can never be inside a relative prefix
        if norm.startswith("/") or norm.startswith("../") or norm == "..":
            return False
        for prefix in prefixes:
            np = os.path.normpath(prefix)
            if norm == np or norm.startswith(np.rstrip("/") + "/"):
                return True
        return False
