"""Domain models: goals, tasks, plans, steps, checkpoints.

These are pure data structures shared across the state, orchestration and
observability layers. No I/O and no business logic live here - serialization
helpers exist so any storage backend can persist a faithful snapshot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


TASK_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})

# Persisted task lifecycle edges.  CREATED remains intentionally permissive:
# hand-built/stored plans may first become durable at PLANNED, RUNNING,
# AWAITING_APPROVAL, or a terminal validation outcome.  Once execution begins,
# earlier planning states cannot be restored.  Same-state writes persist step
# and coordination progress; terminal rows are separately immutable.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(TaskStatus),
    TaskStatus.PLANNING: frozenset({
        TaskStatus.PLANNING,
        TaskStatus.PLANNED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    }),
    TaskStatus.PLANNED: frozenset({
        TaskStatus.PLANNED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    }),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    }),
    TaskStatus.AWAITING_APPROVAL: frozenset({
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
    }),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


class TaskStateError(RuntimeError):
    """A task snapshot lost its revision race or attempted resurrection."""


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # deliberately not executed (e.g. memory-driven guidance)


@dataclass
class VerificationPolicy:
    """How the orchestrator decides a step's observation is acceptable."""

    policy: str  # one of: non_empty | exists | schema_keys
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanStep:
    index: int
    intent: str
    capability: str  # registered capability name, e.g. "filesystem.read"
    action: str      # capability action, e.g. "read"
    scope: str       # permission scope required, e.g. "filesystem:read"
    params: dict[str, Any] = field(default_factory=dict)
    verification: VerificationPolicy = field(default_factory=lambda: VerificationPolicy("non_empty"))
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    max_attempts: int = 2
    result: dict[str, Any] | None = None
    error: str | None = None
    depends_on: list[int] = field(default_factory=list)  # indices of prerequisite steps
    guidance: list[dict[str, Any]] = field(default_factory=list)  # memory-driven transformation provenance (informational)
    skipped_reason: str | None = None  # why this step was deliberately skipped (guidance)
    # ADR-060 D5: True when this step was rehydrated from a stored plan that
    # carried NO verification at all, as opposed to one that explicitly asked
    # for `non_empty`. The two are indistinguishable once the historical
    # default has been applied, but they must NOT be treated alike for a
    # MUTATING action: an absent policy fails closed, an explicit known policy
    # is honoured. Deliberately NOT serialized by `to_dict` - it is a property
    # of one rehydration, not of the plan, so no schema change is implied.
    verification_absent: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = {
            "index": self.index,
            "intent": self.intent,
            "capability": self.capability,
            "action": self.action,
            "scope": self.scope,
            "params": self.params,
            "verification": {"policy": self.verification.policy, "args": self.verification.args},
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "result": self.result,
            "error": self.error,
        }
        if self.depends_on:
            d["depends_on"] = self.depends_on
        if self.guidance:
            d["guidance"] = self.guidance
        if self.skipped_reason:
            d["skipped_reason"] = self.skipped_reason
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanStep":
        v = d.get("verification", {}) or {}
        return cls(
            index=d["index"],
            intent=d.get("intent", ""),
            capability=d["capability"],
            action=d["action"],
            scope=d.get("scope", ""),
            params=d.get("params", {}) or {},
            # The historical `non_empty` fallback is retained for READ-ONLY
            # rehydration (ADR-060 §3 established no Arion-written plan
            # actually omits verification, so it is defensive). Whether it was
            # absent is recorded so a MUTATING step can fail closed instead of
            # silently inheriting shape-only verification.
            verification=VerificationPolicy(policy=v.get("policy", "non_empty"), args=v.get("args", {}) or {}),
            verification_absent=not isinstance(v.get("policy"), str) or not v.get("policy"),
            status=StepStatus(d.get("status", StepStatus.PENDING.value)),
            attempts=d.get("attempts", 0),
            max_attempts=d.get("max_attempts", 2),
            result=d.get("result"),
            error=d.get("error"),
            depends_on=list(d.get("depends_on", []) or []),
            guidance=list(d.get("guidance", []) or []),
            skipped_reason=d.get("skipped_reason"),
        )


@dataclass
class Task:
    id: str
    goal_id: str
    description: str
    status: TaskStatus = TaskStatus.CREATED
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    error: str | None = None
    plan_version: int | None = None  # goal plan version this task implements (ADR-016)
    revision: int = 0  # durable CAS token; incremented on every committed task write
    approvals: list[dict[str, Any]] = field(default_factory=list)  # approval records per step (ADR-017)
    lock_wait: dict[str, Any] | None = None  # ADR-022: durable lock-contention wait metadata
                                              # {resource_kind, resource, deadline, attempts,
                                              #  next_retry}; coordination-only, never authorization
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    completed_at: str | None = None

    @property
    def active_step(self) -> PlanStep | None:
        if 0 <= self.current_step < len(self.steps):
            return self.steps[self.current_step]
        return None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "goal_id": self.goal_id,
            "description": self.description,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "current_step": self.current_step,
            "error": self.error,
            "revision": self.revision,
            "approvals": self.approvals,
            "lock_wait": self.lock_wait,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }
        if self.plan_version is not None:
            d["plan_version"] = self.plan_version
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(
            id=d["id"],
            goal_id=d["goal_id"],
            description=d["description"],
            status=TaskStatus(d.get("status", TaskStatus.CREATED.value)),
            steps=[PlanStep.from_dict(s) for s in d.get("steps", [])],
            current_step=d.get("current_step", 0),
            error=d.get("error"),
            plan_version=d.get("plan_version"),
            revision=int(d.get("revision", 0)),
            approvals=list(d.get("approvals", []) or []),
            lock_wait=dict(d["lock_wait"]) if d.get("lock_wait") else None,
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
            completed_at=d.get("completed_at"),
        )


class GoalStatus(str, Enum):
    """Explicit goal lifecycle states (ADR-016).

    Transitions are validated by GoalManager; invalid transitions fail closed.
    Terminal states: COMPLETED, FAILED, CANCELLED.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowed goal state transitions (src -> set of allowed destinations).
GOAL_TRANSITIONS: dict[str, set[str]] = {
    GoalStatus.ACTIVE.value: {GoalStatus.PAUSED.value, GoalStatus.BLOCKED.value,
                              GoalStatus.COMPLETED.value, GoalStatus.FAILED.value,
                              GoalStatus.CANCELLED.value},
    GoalStatus.PAUSED.value: {GoalStatus.ACTIVE.value, GoalStatus.CANCELLED.value},
    GoalStatus.BLOCKED.value: {GoalStatus.ACTIVE.value, GoalStatus.FAILED.value,
                               GoalStatus.CANCELLED.value},
    GoalStatus.COMPLETED.value: set(),
    GoalStatus.FAILED.value: {GoalStatus.ACTIVE.value, GoalStatus.CANCELLED.value},
    GoalStatus.CANCELLED.value: set(),
}


class GoalStateError(ValueError):
    """Raised on invalid goal state transitions (fail closed)."""


@dataclass
class Goal:
    """A persistent, versioned, long-horizon goal (ADR-016).

    Backward compatible: `status` accepts a plain string ("active") and old
    persisted rows parse into GoalStatus.
    """

    id: str
    description: str
    source: str = "cli"
    status: GoalStatus | str = GoalStatus.ACTIVE
    version: int = 1                      # CAS token; increments on every committed goal-row write
    strategy: str | None = None           # current strategy name
    blockers: list[dict[str, Any]] = field(default_factory=list)  # structured blocker records
    progress_metadata: dict[str, Any] = field(default_factory=dict)  # last evaluation summary
    last_evaluated_at: str | None = None
    last_replan_reason: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = GoalStatus(self.status)

    @property
    def status_value(self) -> str:
        return self.status.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "status": self.status.value,
            "version": self.version,
            "strategy": self.strategy,
            "blockers": self.blockers,
            "progress_metadata": self.progress_metadata,
            "last_evaluated_at": self.last_evaluated_at,
            "last_replan_reason": self.last_replan_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Goal":
        return cls(
            id=d["id"],
            description=d["description"],
            source=d.get("source", "cli"),
            status=d.get("status", "active"),
            version=int(d.get("version", 1)),
            strategy=d.get("strategy"),
            blockers=list(d.get("blockers", []) or []),
            progress_metadata=dict(d.get("progress_metadata", {}) or {}),
            last_evaluated_at=d.get("last_evaluated_at"),
            last_replan_reason=d.get("last_replan_reason"),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
        )


@dataclass
class Checkpoint:
    task_id: str
    status: str
    step_index: int
    snapshot: dict[str, Any]
    reason: str
    id: str = field(default_factory=lambda: new_id("ckpt"))
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "step_index": self.step_index,
            "snapshot": self.snapshot,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Checkpoint":
        snapshot = d["snapshot"]
        if isinstance(snapshot, str):  # stored as JSON text by the storage backend
            import json

            snapshot = json.loads(snapshot)
        return cls(
            id=d.get("id", new_id("ckpt")),
            task_id=d["task_id"],
            status=d["status"],
            step_index=d["step_index"],
            snapshot=snapshot,
            reason=d.get("reason", ""),
            created_at=d.get("created_at", utcnow()),
        )
