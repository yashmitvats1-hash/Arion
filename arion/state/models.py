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
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
            verification=VerificationPolicy(policy=v.get("policy", "non_empty"), args=v.get("args", {}) or {}),
            status=StepStatus(d.get("status", StepStatus.PENDING.value)),
            attempts=d.get("attempts", 0),
            max_attempts=d.get("max_attempts", 2),
            result=d.get("result"),
            error=d.get("error"),
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
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    completed_at: str | None = None

    @property
    def active_step(self) -> PlanStep | None:
        if 0 <= self.current_step < len(self.steps):
            return self.steps[self.current_step]
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "description": self.description,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "current_step": self.current_step,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

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
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
            completed_at=d.get("completed_at"),
        )


@dataclass
class Goal:
    id: str
    description: str
    source: str = "cli"
    status: str = "active"
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Goal":
        return cls(
            id=d["id"],
            description=d["description"],
            source=d.get("source", "cli"),
            status=d.get("status", "active"),
            created_at=d.get("created_at", utcnow()),
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
