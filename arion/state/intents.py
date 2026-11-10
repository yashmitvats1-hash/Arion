"""Durable mutation-intent journal (ADR-020 family; issue #23 / F10).

Before a REPLAY-UNSAFE (non-retry-safe) mutating dispatch reaches a
capability, its intent is persisted here.  The journal closes the hard-crash
window between "side effect applied" and "outcome persisted": after a crash,
an intent row that never settled means the mutation is IN DOUBT - it may or
may not have applied - and must be fenced into the recovery registry instead
of being replayed blindly.

Security model:

- The journal is crash-integrity state, NOT authorization.  Recording an
  intent never approves anything; every mutation still passes the live
  authorization/approval layer independently.
- A non-retry-safe mutation dispatches AT MOST ONCE per (task, step) for the
  lifetime of that step: a duplicate journal entry fails closed and the
  capability is not invoked.  Retry-safe actions are deliberately outside
  the journal - replay after a crash is their declared contract, and the
  fence never applies to them.
- Settlement is tied to durable outcome state (the task snapshot's terminal
  step status or a recovery record).  A settled row is bookkeeping; the
  durable task snapshot remains the restart authority.
- Rows carry bounded identifiers and classification metadata only - never
  file contents, secrets, or capability output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from arion.state.models import utcnow


class MutationIntentError(RuntimeError):
    """Typed failure for the mutation-intent journal (fail closed)."""


# Journal lifecycle.  ``IN_FLIGHT`` rows for replay-unsafe work mean the
# dispatch is unresolved; ``SETTLED`` rows carry the durable outcome.
INTENT_IN_FLIGHT = "in_flight"
INTENT_SETTLED = "settled"

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_RECOVERY = "recovery"


@dataclass
class MutationIntent:
    """One durable record of a replay-unsafe mutation about to dispatch.

    Metadata is bounded by design: identifiers (intent/task/step/capability/
    action/resource), the attempt counter, timestamps and the settled
    outcome - NEVER payload contents or secrets.
    """

    intent_id: str
    task_id: str
    step_index: int
    attempt: int
    capability: str
    action: str
    resource: str | None
    status: str = INTENT_IN_FLIGHT
    outcome: str | None = None
    created_at: str = ""
    settled_at: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utcnow()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MutationIntent":
        return cls(
            intent_id=d["intent_id"],
            task_id=d["task_id"],
            step_index=int(d.get("step_index", 0)),
            attempt=int(d.get("attempt", 0)),
            capability=d["capability"],
            action=d["action"],
            resource=d.get("resource"),
            status=d.get("status", INTENT_IN_FLIGHT),
            outcome=d.get("outcome"),
            created_at=d.get("created_at", ""),
            settled_at=d.get("settled_at"),
        )


class MutationIntentStore(Protocol):
    """Persistence contract for the durable mutation-intent journal."""

    def record_mutation_intent(self, intent: MutationIntent) -> MutationIntent:
        """Durably journal one dispatch intent.  A second row for the same
        (task_id, step_index) is refused (fail closed): replay-unsafe work
        dispatches at most once per step lifetime."""
        ...

    def settle_mutation_intent(
        self, task_id: str, step_index: int, outcome: str,
        now: str | None = None,
    ) -> int:
        """Close open intent rows for one (task, step) with a durable
        outcome.  Idempotent; returns the number of rows settled."""
        ...

    def list_mutation_intents(
        self, task_id: str | None = None, open_only: bool = False,
    ) -> list[MutationIntent]:
        """List journal rows, newest first; ``open_only`` restricts to
        unsettled (in-flight) intents."""
        ...
