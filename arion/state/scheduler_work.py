"""Durable scheduler/work registry (ADR-025, Phase A).

A SchedulerWork row is the durable record of ONE unit of scheduler work (one
task step admitted to the shared in-process scheduler). It exists so that a
scheduler process can die and restart without losing sight of what was
queued / running / done, and so a restarted scheduler can reclaim stale
leases instead of believing in immortal RUNNING workers.

Authority model (unchanged from ADR-024, now made durable):

- The scheduler/work registry is COORDINATION, never authorization. A row
  says nothing about whether the underlying step may touch the world; every
  dispatched step still passes live authorization -> approval -> durable
  mutation lock -> FIFO queue before its capability executes.
- The registry is the only source of durable WORKER LIFECYCLE state. Nothing
  in memory, cognition, strategy, guidance, model output, approval metadata,
  recovery metadata, queue position, or worker identity can transition a row
  or manufacture concurrency.
- Rows carry bounded metadata only: ids, task/goal/step references,
  scheduler + worker identity, timestamps, a lease deadline, and bounded
  error text. NEVER threads, callables, stack traces, capability outputs,
  model output, prompts, file contents, or secrets.

States are explicit and fail closed:

    QUEUED -> RUNNING -> COMPLETED | FAILED
    QUEUED -> CANCELLED | ABANDONED
    RUNNING -> ABANDONED            (stale lease reclaim)

Terminal states (COMPLETED/FAILED/CANCELLED/ABANDONED) are final; every
invalid transition raises the typed SchedulerStateError.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from arion.state.models import new_id, utcnow


class SchedulerWorkStatus(str, Enum):
    """Durable lifecycle of one scheduler work row (ADR-025)."""

    QUEUED = "queued"          # admitted, waiting for a worker
    RUNNING = "running"        # picked up by a worker (lease held)
    COMPLETED = "completed"    # worker finished successfully
    FAILED = "failed"          # worker reported failure (bounded error)
    CANCELLED = "cancelled"    # cancelled while queued (advisory, pre-execution)
    ABANDONED = "abandoned"    # stale lease reclaimed / dead scheduler's queue


# Legal transitions (terminal states are final). Fail closed: anything else
# raises SchedulerStateError.
_LEGAL_TRANSITIONS: dict[SchedulerWorkStatus, set[SchedulerWorkStatus]] = {
    SchedulerWorkStatus.QUEUED: {
        SchedulerWorkStatus.RUNNING,
        SchedulerWorkStatus.CANCELLED,
        SchedulerWorkStatus.ABANDONED,
    },
    SchedulerWorkStatus.RUNNING: {
        SchedulerWorkStatus.COMPLETED,
        SchedulerWorkStatus.FAILED,
        SchedulerWorkStatus.ABANDONED,
    },
    SchedulerWorkStatus.COMPLETED: set(),
    SchedulerWorkStatus.FAILED: set(),
    SchedulerWorkStatus.CANCELLED: set(),
    SchedulerWorkStatus.ABANDONED: set(),
}


def legal_transition(current: SchedulerWorkStatus, target: SchedulerWorkStatus) -> bool:
    return target in _LEGAL_TRANSITIONS.get(current, set())


class SchedulerStateError(Exception):
    """Typed invalid scheduler-state transition (fail closed)."""


class SchedulerRegistryError(Exception):
    """Typed registry-level failure (e.g. invalid metadata)."""


@dataclass
class SchedulerWork:
    """One durable scheduler work row.

    Bounded metadata only - identifiers, references, timestamps, a lease
    deadline and truncated error text. Never persisted: threads, callables,
    stack traces, capability outputs, model output, prompts, file contents,
    or secrets.
    """

    work_id: str
    task_id: str
    goal_id: str | None
    step_index: int
    scheduler_id: str
    status: SchedulerWorkStatus = SchedulerWorkStatus.QUEUED
    worker_id: str | None = None
    attempts: int = 0
    error: str | None = None
    created_at: str = field(default_factory=utcnow)
    started_at: str | None = None
    completed_at: str | None = None
    lease_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "step_index": self.step_index,
            "scheduler_id": self.scheduler_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "error": (self.error or "")[:500],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SchedulerWork":
        return cls(
            work_id=d["work_id"],
            task_id=d["task_id"],
            goal_id=d.get("goal_id"),
            step_index=int(d.get("step_index", 0)),
            scheduler_id=d["scheduler_id"],
            status=SchedulerWorkStatus(d.get("status", SchedulerWorkStatus.QUEUED.value)),
            worker_id=d.get("worker_id"),
            attempts=int(d.get("attempts", 0)),
            error=d.get("error"),
            created_at=d.get("created_at", utcnow()),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            lease_expires_at=d.get("lease_expires_at"),
        )


def new_work_id() -> str:
    return new_id("sw")


class SchedulerRegistry(Protocol):
    """Store protocol for the durable scheduler registry (ADR-025/026).

    The engine and CLI talk to this protocol only - never to SQLite
    directly. Implemented by SQLiteStorage (the default), exactly like the
    approval/recovery/lock stores.

    ADR-026 adds real OWNERSHIP: claims are atomic (BEGIN IMMEDIATE),
    leases are bounded + monotonic, heartbeats are ownership-checked, and
    terminal transitions from RUNNING require the current owner.
    """

    def create(self, *, task_id: str, goal_id: str | None, step_index: int,
               scheduler_id: str, now: str | None = None) -> SchedulerWork:
        """Admit one unit of work in QUEUED state. Returns the durable row."""
        ...

    def mark_running(self, work_id: str, worker_id: str, lease_seconds: float,
                     now: str | None = None,
                     max_lease_seconds: float | None = None) -> SchedulerWork:
        """QUEUED -> RUNNING with a bounded initial lease deadline."""
        ...

    def mark_terminal(self, work_id: str, status: SchedulerWorkStatus,
                      error: str | None = None, now: str | None = None,
                      owner_worker_id: str | None = None) -> SchedulerWork:
        """Transition to a legal terminal state. RUNNING -> COMPLETED/FAILED
        requires the exact owner. RUNNING -> ABANDONED additionally requires
        lease expiry at ``now``; use ``reclaim_work`` for explicit reclaim.
        Typed error on live, illegal, or unknown transitions."""
        ...

    def get_work(self, work_id: str) -> SchedulerWork | None:
        ...

    def list_work(self, status: SchedulerWorkStatus | None = None,
                  scheduler_id: str | None = None,
                  task_id: str | None = None,
                  goal_id: str | None = None,
                  step_index: int | None = None) -> list[SchedulerWork]:
        ...

    def reclaim_work(self, work_id: str,
                     now: str | None = None) -> SchedulerWork:
        """Atomically reclaim one expired RUNNING row. A renewed/live row,
        queued row, terminal row, or unknown id fails closed."""
        ...

    def reclaim_stale(self, now: str | None = None) -> list[str]:
        """RUNNING rows whose lease expired -> ABANDONED. Returns reclaimed
        work ids. Idempotent: terminal rows are never touched."""
        ...

    def abandon_foreign_queued(self, scheduler_id: str,
                               now: str | None = None) -> int:
        """QUEUED rows whose scheduler has NO live registration (dead
        process) -> ABANDONED. A LIVE peer's queue is never touched.
        Idempotent."""
        ...

    # ---- ADR-026 ownership primitives ----

    def register_scheduler(self, scheduler_id: str, pid: int,
                           lease_seconds: float, now: str | None = None) -> None:
        """Durable scheduler registration (unique id; process-lifetime)."""
        ...

    def heartbeat_scheduler(self, scheduler_id: str, lease_seconds: float,
                            now: str | None = None,
                            max_lease_seconds: float | None = None) -> bool:
        """Extend a live registration with a monotonic sliding-bounded lease.
        Returns False for an unknown or expired scheduler."""
        ...

    def unregister_scheduler(self, scheduler_id: str) -> None:
        """Remove a scheduler registration (clean shutdown). Idempotent."""
        ...

    def scheduler_registration_live(self, scheduler_id: str,
                                    now: str | None = None) -> bool:
        ...

    def set_scheduler_global_max(self, n: int) -> None:
        """Configure the durable cross-process capacity (>= 1)."""
        ...

    def get_scheduler_global_max(self) -> int | None:
        ...

    def claim(self, work_id: str, worker_id: str, lease_seconds: float,
              now: str | None = None,
              max_lease_seconds: float | None = None,
              scheduler_id: str | None = None) -> SchedulerWork:
        """Atomically claim ONE specific QUEUED row: expired RUNNING rows are
        reclaimed, the cross-process capacity AND fair share (if configured)
        are enforced, and the row transitions QUEUED -> RUNNING with this
        owner + lease. Exactly one owner under any race. Typed error if not
        claimable."""
        ...

    def claim_next(self, scheduler_id: str, worker_id: str,
                   lease_seconds: float, now: str | None = None,
                   max_lease_seconds: float | None = None) -> SchedulerWork | None:
        """Atomically claim the oldest QUEUED row for this scheduler under
        the same transaction rules as claim(). None when empty / capacity
        full."""
        ...

    def heartbeat(self, work_id: str, worker_id: str, lease_seconds: float,
                  now: str | None = None,
                  max_lease_seconds: float | None = None) -> SchedulerWork:
        """Ownership-checked sliding lease extension: monotonic, bounded per
        renewal, and unable to resurrect an already-expired row."""
        ...

    def release_and_claim_next(self, work_id: str, owner_worker_id: str,
                               status: SchedulerWorkStatus,
                               error: str | None, scheduler_id: str,
                               worker_id: str, lease_seconds: float,
                               now: str | None = None,
                               max_lease_seconds: float | None = None,
                               ) -> tuple[SchedulerWork, SchedulerWork | None]:
        """Atomic handoff: live-lease and ownership-checked terminal
        transition of one row plus claim of the next QUEUED row in one
        transaction. Expired owners cannot complete or claim next."""
        ...

    # ---- ADR-027: durable per-goal scheduling weights ----

    def set_goal_weight(self, goal_id: str, weight: int, *,
                        enabled: bool = True, by: str = "operator",
                        now: str | None = None) -> None:
        """Set/update a goal's durable scheduling weight (>= 1, bounded).
        Typed error on invalid weights (fail closed)."""
        ...

    def get_goal_weight(self, goal_id: str) -> int:
        """Configured weight, or the deterministic default 1."""
        ...

    def get_goal_weight_config(self, goal_id: str) -> dict | None:
        """Bounded config row (weight/enabled/updated_at/updated_by) or
        None when unconfigured."""
        ...

    def list_goal_weights(self) -> list[dict]:
        """Bounded, ordered list of configured weights."""
        ...

    def remove_goal_weight(self, goal_id: str) -> bool:
        """Delete the config (back to default behavior). True when a row
        was removed."""
        ...

    def set_goal_weight_enabled(self, goal_id: str, enabled: bool) -> dict | None:
        """Enable/disable a goal's weight config (None when unconfigured)."""
        ...


def _iso_plus(iso: str, seconds: float) -> str:
    """iso + seconds (naive/aware-safe, mirrors engine helpers)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()
