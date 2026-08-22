"""Durable mutation recovery registry (ADR-020).

A failed non-retry-safe mutation (one that may have partially applied) creates
a `MutationRecovery` record in `RECOVERY_REQUIRED` state. The record is a
DURABLE, restart-safe condition: the goal loop is gated on it (a
`recovery_required` goal blocker) until an operator explicitly acknowledges
the recovery via the engine (`acknowledge_recovery`), which transitions the
record to `RECOVERY_ACKNOWLEDGED` and unblocks the goal.

Security model (ADR-020):

- Recovery is NOT an authorization mechanism. Acknowledging recovery only
  records "this previous mutation failed and requires explicit handling".
  Every new mutation still passes through the live authorization layer
  (scope/boundary/risk/approval queue) independently.
- Recovery cannot mutate or resurrect approval records. Expired/denied
  approvals stay expired/denied; an approved record stays approved.
- The record carries bounded identifiers and reasons ONLY - never file
  contents, secrets, or raw model output.
- Nothing in memory/reflection/guidance/strategy/model output can create,
  clear, or acknowledge a recovery; only the engine API can.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from arion.state.models import new_id, utcnow


class RecoveryStatus(str, Enum):
    REQUIRED = "required"          # the failed mutation needs explicit handling
    ACKNOWLEDGED = "acknowledged"  # an operator explicitly handled it


class RecoveryError(Exception):
    """Typed failure for the recovery registry (fail closed)."""


@dataclass
class MutationRecovery:
    """One durable, restart-safe record of a failed non-retry-safe mutation.

    Metadata is bounded by design: identifiers (task/goal/step/capability/
    action/resource), a truncated reason, timestamps and actor - NEVER file
    contents or secrets.
    """

    recovery_id: str
    task_id: str
    goal_id: str | None
    step_index: int
    capability: str
    action: str
    resource: str | None
    reason: str               # bounded explainable reason (no content)
    status: RecoveryStatus = RecoveryStatus.REQUIRED
    created_at: str = field(default_factory=utcnow)
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MutationRecovery":
        return cls(
            recovery_id=d["recovery_id"],
            task_id=d["task_id"],
            goal_id=d.get("goal_id"),
            step_index=int(d.get("step_index", 0)),
            capability=d["capability"],
            action=d["action"],
            resource=d.get("resource"),
            reason=d.get("reason", ""),
            status=RecoveryStatus(d.get("status", RecoveryStatus.REQUIRED.value)),
            created_at=d.get("created_at", utcnow()),
            acknowledged_at=d.get("acknowledged_at"),
            acknowledged_by=d.get("acknowledged_by"),
        )


class RecoveryStore(Protocol):
    """Persistence contract for the durable mutation recovery registry."""

    def create_recovery(self, recovery: MutationRecovery) -> MutationRecovery: ...
    def commit_recovery_requirement(
        self, recovery: MutationRecovery, task: Any,
        expected_task_revision: int,
    ) -> tuple[MutationRecovery, bool, bool]: ...
    def get_recovery(self, recovery_id: str) -> MutationRecovery | None: ...
    def list_recoveries(self, status: str | None = None,
                        goal_id: str | None = None,
                        task_id: str | None = None) -> list[MutationRecovery]: ...
    def update_recovery(self, recovery: MutationRecovery) -> None: ...
