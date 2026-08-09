"""Observability layer: structured audit events and sinks.

Every meaningful transition in the orchestration layer emits an AuditEvent
(kind, task_id, step_id, actor, success, structured detail). Events are
persisted via the storage backend and optionally mirrored to a JSONL file,
giving Arion a full replayable audit trail (ADR-007).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arion.state.models import new_id, utcnow

# Canonical event kinds - the vocabulary of the audit trail.
EVENT_KINDS = (
    "goal.submitted",
    "goal.created",
    "goal.state.changed",
    "goal.evaluated",
    "goal.replanned",
    "goal.blocked",
    "goal.unblocked",
    "goal.approval.pending",
    "goal.approval.granted",
    "goal.approval.denied",
    "goal.approval.expired",
    "capability.unavailable",
    "capability.available",
    "task.approval.resumed",
    "progress.evaluated",
    "plan.versioned",
    "task.created",
    "task.planning",
    "plan.produced",
    "task.resumed",
    "step.started",
    "permission.checked",
    "permission.denied",
    "approval.requested",
    "approval.queued",
    "approval.granted",
    "approval.denied",
    "approval.expired",
    "planning.requested",
    "model.response.received",
    "plan.validation.passed",
    "plan.validation.failed",
    "memory.episode.recorded",
    "memory.retrieval.completed",
    "memory.consolidated",
    "reflection.requested",
    "reflection.validation.passed",
    "reflection.validation.failed",
    "reflection.created",
    "planning.context.created",
    "planning.memory.influence",
    "planning.memory.transformation",
    "belief.derived",
    "step.skipped",
    "world.state.changed",
    "strategy.selected",
    "capability.discovered",
    "capability.executed",
    "observation.recorded",
    "mutation.attempted",
    "mutation.succeeded",
    "mutation.failed",
    "mutation.requires_recovery",
    "verification.passed",
    "verification.failed",
    "step.retrying",
    "task.recovered",
    "checkpoint.persisted",
    "task.completed",
    "task.failed",
    "error",
)


@dataclass
class AuditEvent:
    kind: str
    task_id: str | None = None
    step_id: str | None = None
    success: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    ts: str = field(default_factory=utcnow)
    id: str = field(default_factory=lambda: new_id("evt"))

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown audit event kind: {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "AuditEvent":
        """Reconstruct from a sqlite row: (id, ts, task_id, step_id, kind, actor, success, detail)."""
        return cls(
            id=row[0],
            ts=row[1],
            task_id=row[2],
            step_id=row[3],
            kind=row[4],
            actor=row[5],
            success=bool(row[6]),
            detail=json.loads(row[7]),
        )


class EventSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class EventLogger:
    """Fan-out sink: forwards every event to all registered sinks."""

    def __init__(self, sinks: list[EventSink] | None = None):
        self._sinks: list[EventSink] = list(sinks or [])

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(self, event: AuditEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)


class JsonlFileSink:
    """Appends events as newline-delimited JSON (for log tooling)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: AuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict()) + "\n")
