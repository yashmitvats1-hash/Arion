"""Durable approval queue (ADR-018, Phase A).

ApprovalRequest is the persistent, restart-safe record of a REQUIRE_APPROVAL
pause. The queue is the source of truth for pending approvals: the engine
creates exactly one request per task/step/authorization-fingerprint, and an
approval decision (CLI, future UI, automation) resolves the durable record -
never an in-memory object.

ApprovalStore is the storage protocol; SQLiteStorage implements it. The CLI
talks to the queue through the engine (engine.approval_store +
engine.resolve_approval_request), never directly to SQLite.

Privacy: the record stores bounded metadata only (capability/action/scope/
risk/side effects/resource kind/resource, param KEY names, the canonical
authorization fingerprint). No raw prompts, no secrets, no model output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from arion.state.models import new_id, utcnow


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # stale PENDING (ADR-019): cannot be resolved; auditable


class ApprovalError(Exception):
    """Typed failure for the approval queue (fail closed)."""


@dataclass
class ApprovalRequest:
    """One durable, restart-safe approval decision request."""

    approval_id: str
    task_id: str
    step_index: int
    goal_id: str | None
    capability: str
    action: str
    scope: str                 # resolved required_scope from the ActionSpec
    risk: str
    side_effects: str
    resource_kind: str | None
    resource: str | None
    summary: str               # bounded human-readable summary
    status: ApprovalStatus = ApprovalStatus.PENDING
    requester_actor: str = "system"
    actor_chain: list[str] = field(default_factory=list)
    params_keys: list[str] = field(default_factory=list)  # param NAMES only
    fingerprint: dict[str, Any] = field(default_factory=dict)  # canonical authz fingerprint
    decision_actor: str | None = None
    decided_at: str | None = None
    expired_at: str | None = None  # when the request was marked EXPIRED (ADR-019)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalRequest":
        return cls(
            approval_id=d["approval_id"],
            task_id=d["task_id"],
            step_index=int(d.get("step_index", 0)),
            goal_id=d.get("goal_id"),
            capability=d["capability"],
            action=d["action"],
            scope=d["scope"],
            risk=d["risk"],
            side_effects=d["side_effects"],
            resource_kind=d.get("resource_kind"),
            resource=d.get("resource"),
            summary=d.get("summary", ""),
            status=ApprovalStatus(d.get("status", ApprovalStatus.PENDING.value)),
            requester_actor=d.get("requester_actor", "system"),
            actor_chain=list(d.get("actor_chain", []) or []),
            params_keys=list(d.get("params_keys", []) or []),
            fingerprint=dict(d.get("fingerprint", {}) or {}),
            decision_actor=d.get("decision_actor"),
            decided_at=d.get("decided_at"),
            expired_at=d.get("expired_at"),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


class ApprovalStore(Protocol):
    """Persistence contract for the durable approval queue."""

    def create_request(self, request: ApprovalRequest) -> None: ...
    def get_request(self, approval_id: str) -> ApprovalRequest | None: ...
    def list_requests(self, status: str | None = None) -> list[ApprovalRequest]: ...
    def update_request(self, request: ApprovalRequest) -> None: ...
    def latest_request_for_step(self, task_id: str, step_index: int) -> ApprovalRequest | None: ...
