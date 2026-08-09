"""Resource-aware authorization layer (ADR-009, hardened).

Authorization and capability containment are SEPARATE concepts:

- The policy decides, from a structured request, whether an action may run:
  `Capability -> Action -> Resource -> Parameters -> Policy Decision`.
- The capability (e.g. the filesystem sandbox) enforces its own containment.
  Neither substitutes for the other.

FAIL-CLOSED RESOURCE SEMANTICS: a resource-sensitive action (one whose
ActionSpec declares a resource_kind) is denied when no explicit resource
boundary is configured for that kind. Absence of a boundary never means
unrestricted access. Three states are distinguished:

  - actions with NO resource (resource_kind is None)  -> no boundary needed;
  - actions whose resource is EXPLICITLY constrained  -> boundary enforces it;
  - actions REQUIRING a boundary but lacking one      -> DENY (fail closed).

Boundaries are keyed by resource KIND (e.g. "filesystem:path", future "url",
"queue:name"), not by capability name - a new capability reuses the boundary
of its resource kind, and a new resource kind needs an explicit boundary
before any action targeting it can run.

IDENTITY: policies evaluate against an Actor, which carries a delegation
chain `user -> agent -> delegated agent` so `agent="system"` is never treated
as the final authorization model. Policies/approvals can match the direct
actor or any ancestor in the chain.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Identity abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Identity context for authorization.

    - kind: "user" | "agent" | "service"
    - name: identifier within the kind (e.g. "alice", "arion", "delegate-7")
    - hierarchy: delegation chain, OUTERMOST first - the full chain is
      `hierarchy + (kind:name,)`.

    Example: user alice delegates to agent arion, which delegates to a
    sub-agent:
        Actor.user("alice").delegated("arion").delegated("delegate-7")
        -> id "agent:delegate-7", chain ("user:alice", "agent:arion", "agent:delegate-7")

    Policies and approval flows can match any element of the chain.
    """

    kind: str
    name: str
    hierarchy: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def user(cls, name: str) -> "Actor":
        return cls(kind="user", name=name)

    @classmethod
    def agent(cls, name: str) -> "Actor":
        return cls(kind="agent", name=name)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def chain(self) -> tuple[str, ...]:
        return self.hierarchy + (self.id,)

    def delegated(self, name: str, kind: str = "agent") -> "Actor":
        """Return a new actor acting on behalf of this one (delegation)."""
        return Actor(kind=kind, name=name, hierarchy=self.chain)


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

    actor: Actor                       # identity context (user -> agent -> delegated agent)
    task_id: str
    step_index: int
    capability: str
    action: str
    scope: str                         # resolved from the capability's ActionSpec (never the plan's claim)
    params: dict[str, Any]
    resource: str | None = None        # resource the action targets (extracted via ActionSpec.resource_param)
    resource_kind: str | None = None   # kind of resource (ActionSpec.resource_kind); None = no resource
    risk: str = "low"                  # none | low | medium | high (from action metadata)
    side_effects: str = "read_only"    # none | read_only | mutating | irreversible (from metadata)
    idempotent: bool = True
    retry_safe: bool = True

    @property
    def agent(self) -> str:
        """Direct actor id - kept as a convenience alias."""
        return self.actor.id


@dataclass
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    scope: str
    resource: str | None = None
    resource_kind: str | None = None
    risk: str = "low"
    side_effects: str = "read_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "scope": self.scope,
            "resource": self.resource,
            "resource_kind": self.resource_kind,
            "risk": self.risk,
            "side_effects": self.side_effects,
        }


class PermissionPolicy(Protocol):
    """Decides whether a structured authorization request may run."""

    def decide(self, request: AuthorizationRequest) -> PolicyDecision: ...


# ---------------------------------------------------------------------------
# Resource boundaries (extensible, keyed by resource kind)
# ---------------------------------------------------------------------------


class ResourceBoundary(Protocol):
    """A boundary for one resource kind.

    Implementations decide whether a concrete resource identifier is inside
    the allowed region. New kinds (url, queue, bucket, ...) bring their own
    boundary types; the policy engine stays kind-agnostic.
    """

    def allows(self, resource: str) -> bool: ...


class RelativePathBoundary:
    """Filesystem-style boundary: any relative, non-traversal path.

    Used as the default boundary for kind "filesystem:path": the capability
    enforces the real sandbox root, while the policy guarantees it never
    blesses an absolute or upward-traversing path.
    """

    def allows(self, resource: str) -> bool:
        norm = os.path.normpath(resource)
        if norm == "":
            return False  # empty resource is never a valid path
        if norm.startswith("/") or norm.startswith("../") or norm == "..":
            return False  # absolute or upward-traversing: never allowed
        return True  # "." and any relative, non-traversal path are allowed


class PathPrefixBoundary:
    """Filesystem-style boundary restricted to one or more path prefixes."""

    def __init__(self, allowed_prefixes: list[str] | tuple[str, ...]):
        self.allowed_prefixes = tuple(allowed_prefixes)

    def allows(self, resource: str) -> bool:
        norm = os.path.normpath(resource)
        if norm == "":
            return False
        if norm.startswith("/") or norm.startswith("../") or norm == "..":
            return False
        for prefix in self.allowed_prefixes:
            np = os.path.normpath(prefix)
            if norm == np or norm.startswith(np.rstrip("/") + "/"):
                return True
        return False


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
    """Configurable, deterministic, fail-closed policy.

    Decision pipeline (first matching rule wins):
      1. identity not permitted (actor id or any ancestor in the chain) -> DENY
      2. scope not in allowlist                                       -> DENY
      3. scope in denylist                                            -> DENY
      4. resource-sensitive action without a configured boundary      -> DENY (fail closed)
      5. resource missing or outside its boundary                     -> DENY
      6. risk in deny set                                             -> DENY
      7. risk in approval set                                         -> REQUIRE_APPROVAL
      8. otherwise                                                    -> ALLOW
    """

    def __init__(
        self,
        allowed_scopes: set[str] | None = None,
        denied_scopes: set[str] | None = None,
        risk_deny: set[str] | None = None,
        risk_approve: set[str] | None = None,
        boundaries: dict[str, ResourceBoundary] | None = None,
        allowed_agents: set[str] | None = None,
    ):
        self.allowed_scopes = allowed_scopes or {"filesystem:read"}
        self.denied_scopes = denied_scopes or set()
        # read-only slice: high risk is never allowed, medium risk needs approval
        self.risk_deny = risk_deny or {"high"}
        self.risk_approve = risk_approve or {"medium"}
        self.boundaries: dict[str, ResourceBoundary] = boundaries or {}
        self.allowed_agents = allowed_agents  # None = any identity allowed

    def decide(self, request: AuthorizationRequest) -> PolicyDecision:
        base = dict(
            scope=request.scope,
            resource=request.resource,
            resource_kind=request.resource_kind,
            risk=request.risk,
            side_effects=request.side_effects,
        )

        if self.allowed_agents is not None and not any(a in self.allowed_agents for a in request.actor.chain):
            return PolicyDecision(
                PolicyOutcome.DENY,
                f"identity {request.actor.id!r} (chain {list(request.actor.chain)}) not permitted",
                **base,
            )

        if request.scope not in self.allowed_scopes:
            return PolicyDecision(PolicyOutcome.DENY, f"scope {request.scope!r} not permitted by policy", **base)

        if request.scope in self.denied_scopes:
            return PolicyDecision(PolicyOutcome.DENY, f"scope {request.scope!r} explicitly denied", **base)

        ok, reason = self._resource_allowed(request)
        if not ok:
            return PolicyDecision(PolicyOutcome.DENY, reason, **base)

        if request.risk in self.risk_deny:
            return PolicyDecision(PolicyOutcome.DENY, f"risk level {request.risk!r} denied by policy", **base)

        if request.risk in self.risk_approve:
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                f"risk level {request.risk!r} requires approval",
                **base,
            )

        return PolicyDecision(PolicyOutcome.ALLOW, "allowed", **base)

    def _resource_allowed(self, request: AuthorizationRequest) -> tuple[bool, str | None]:
        """Fail-closed resource check, keyed by resource kind (no filesystem I/O)."""
        kind = request.resource_kind
        if kind is None:
            # Action has no resource: nothing to constrain, never denied on resource grounds.
            return True, None

        boundary = self.boundaries.get(kind)
        if boundary is None:
            # Resource-sensitive action with NO configured boundary: fail closed.
            return False, f"no resource boundary configured for resource kind {kind!r} (fail closed)"

        resource = request.resource
        if not isinstance(resource, str) or not resource:
            return False, f"resource-sensitive action {request.capability}/{request.action} missing resource"

        if not boundary.allows(resource):
            return False, f"resource {resource!r} outside boundary for kind {kind!r}"

        return True, None
