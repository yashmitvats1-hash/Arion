"""Observability layer: structured audit events and sinks.

Every meaningful transition in the orchestration layer emits an AuditEvent
(kind, task_id, step_id, actor, success, structured detail). Events are
persisted via the storage backend and optionally mirrored to a JSONL file,
giving Arion a full replayable audit trail (ADR-007).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from arion.observability.error_boundary import sanitize_error_text
from arion.resource_identifiers import present_resource, present_resource_reason
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
    "goal.run.claimed",
    "goal.run.contended",
    "goal.run.released",
    "goal.run.ownership_lost",
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
    "memory.learning.catchup",
    "memory.pruned",
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
    "strategy.outcome",
    "capability.discovered",
    "capability.executed",
    "observation.recorded",
    "mutation.attempted",
    "mutation.succeeded",
    "mutation.failed",
    "mutation.requires_recovery",
    "recovery.required",
    "recovery.acknowledged",
    "planning.recovery.advisory",
    "mutation.lock.requested",
    "mutation.lock.queued",
    "mutation.lock.acquired",
    "mutation.lock.contended",
    "mutation.lock.waiting",
    "mutation.lock.retry",
    "mutation.lock.timeout",
    "mutation.lock.reclaimed",
    "mutation.lock.released",
    "verification.passed",
    "verification.failed",
    "step.retrying",
    "task.recovered",
    "checkpoint.persisted",
    "task.completed",
    "task.failed",
    # Scheduler telemetry (ADR-028) - observational only, never authority.
    "scheduler.registered",
    "scheduler.heartbeat",
    "scheduler.shutdown",
    "scheduler.abandoned",
    "scheduler.config_changed",
    "work.queued",
    "work.claimed",
    "work.claim_denied",
    "work.heartbeat",
    "work.reclaimed",
    "work.handoff",
    "work.completed",
    "work.failed",
    "capacity.denied",
    "scheduler_share.denied",
    "goal_weight.denied",
    "goal_weight.refill",
    "goal_reservation_changed",
    "reservation.denied",
    "reservation.satisfied",
    "goal_ceiling_changed",
    "ceiling.denied",
    "error",
)


class EventContractError(ValueError):
    """Raised when an event envelope or detail payload violates its contract."""


@runtime_checkable
class EventDetails(Protocol):
    """Adapter implemented by typed event-detail models.

    The unique method name avoids accidentally treating arbitrary domain
    objects with a generic ``to_dict`` method as event payloads.
    """

    def to_event_detail(self) -> Mapping[str, Any]: ...


def normalize_event_detail(
    detail: Mapping[str, Any] | EventDetails | None,
) -> dict[str, Any]:
    """Normalize compatible raw/typed details to a durable JSON snapshot.

    Dictionary producers remain supported.  A deep copy makes the event a
    snapshot at construction time instead of retaining mutable producer state;
    JSON validation uses the same serialization rules as SQLite and JSONL.
    """
    if detail is None:
        mapping: Mapping[str, Any] = {}
    elif isinstance(detail, Mapping):
        mapping = detail
    elif isinstance(detail, EventDetails):
        mapping = detail.to_event_detail()
        if not isinstance(mapping, Mapping):
            raise EventContractError(
                "EventDetails.to_event_detail() must return a mapping"
            )
    else:
        raise EventContractError(
            "event detail must be a mapping or EventDetails instance"
        )

    if any(not isinstance(key, str) for key in mapping):
        raise EventContractError("event detail must use string keys")
    try:
        snapshot = deepcopy(dict(mapping))
    except Exception as exc:
        raise EventContractError(
            f"event detail could not be snapshotted: {exc}"
        ) from exc
    try:
        json.dumps(snapshot)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EventContractError(
            f"event detail must be JSON serializable: {exc}"
        ) from exc
    return snapshot


_AUTHORIZATION_OUTCOMES = frozenset({"allow", "deny", "require_approval"})
_AUTHORIZATION_FIELDS = frozenset({
    "outcome",
    "reason",
    "scope",
    "resource",
    "resource_kind",
    "risk",
    "side_effects",
})


@dataclass(frozen=True)
class AuthorizationEventDetails:
    """Versioned policy-decision event details (ADR-033).

    The seven policy fields are stable.  Optional context is deliberately
    bounded to identifiers, scope metadata, and parameter *names*; arbitrary
    parameter values are never part of this typed contract.
    """

    outcome: str
    reason: str
    scope: str
    resource: str | None = None
    resource_kind: str | None = None
    risk: str = "low"
    side_effects: str = "read_only"
    actor: str | None = None
    actor_chain: tuple[str, ...] = ()
    param_keys: tuple[str, ...] = ()
    step_declared_scope: str | None = None
    revalidated_after_lock_wait: bool | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _AUTHORIZATION_OUTCOMES:
            raise EventContractError(
                f"invalid authorization outcome: {self.outcome!r}"
            )
        for name in ("reason", "scope", "risk", "side_effects"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise EventContractError(
                    f"authorization detail {name!r} must be a non-empty string"
                )
        for name in ("resource", "resource_kind", "actor", "step_declared_scope"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise EventContractError(
                    f"authorization detail {name!r} must be a string or None"
                )
        if any(not isinstance(value, str) or not value for value in self.actor_chain):
            raise EventContractError(
                "authorization actor_chain must contain non-empty strings"
            )
        if any(not isinstance(value, str) or not value for value in self.param_keys):
            raise EventContractError(
                "authorization param_keys must contain non-empty strings"
            )
        if (self.revalidated_after_lock_wait is not None
                and not isinstance(self.revalidated_after_lock_wait, bool)):
            raise EventContractError(
                "revalidated_after_lock_wait must be a boolean or None"
            )
        # Stable ordering and duplicate elimination make persisted details
        # deterministic without retaining mutable caller-owned sequences.
        object.__setattr__(self, "actor_chain", tuple(self.actor_chain))
        object.__setattr__(self, "param_keys", tuple(sorted(set(self.param_keys))))

    @classmethod
    def from_mapping(
        cls,
        decision: Mapping[str, Any],
        **context: Any,
    ) -> "AuthorizationEventDetails":
        """Adapt a PolicyDecision-style mapping without importing authz.

        Unknown policy fields fail rather than being silently persisted as an
        undocumented extension.  Legacy dictionaries bypass this typed model
        and remain readable through ``AuditEvent`` directly.
        """
        if not isinstance(decision, Mapping):
            raise EventContractError("authorization decision must be a mapping")
        unknown = set(decision) - _AUTHORIZATION_FIELDS
        if unknown:
            raise EventContractError(
                f"unknown authorization decision fields: {sorted(unknown)!r}"
            )
        missing = {"outcome", "reason", "scope"} - set(decision)
        if missing:
            raise EventContractError(
                f"missing authorization decision fields: {sorted(missing)!r}"
            )
        return cls(
            outcome=decision["outcome"],
            reason=decision["reason"],
            scope=decision["scope"],
            resource=decision.get("resource"),
            resource_kind=decision.get("resource_kind"),
            risk=decision.get("risk", "low"),
            side_effects=decision.get("side_effects", "read_only"),
            **context,
        )

    def to_event_detail(self) -> dict[str, Any]:
        presentation = present_resource(self.resource_kind, self.resource)
        detail: dict[str, Any] = {
            "schema_version": 2,
            "outcome": self.outcome,
            "reason": present_resource_reason(
                self.reason, self.resource_kind, self.resource
            ),
            "scope": self.scope,
            "resource": presentation.display,
            "resource_kind": self.resource_kind,
            "resource_fingerprint": presentation.fingerprint,
            "resource_redacted": presentation.redacted,
            "risk": self.risk,
            "side_effects": self.side_effects,
        }
        if self.actor is not None:
            detail["actor"] = self.actor
        if self.actor_chain:
            detail["actor_chain"] = list(self.actor_chain)
        if self.param_keys:
            detail["param_keys"] = list(self.param_keys)
        if self.step_declared_scope is not None:
            detail["step_declared_scope"] = self.step_declared_scope
        if self.revalidated_after_lock_wait is not None:
            detail["revalidated_after_lock_wait"] = (
                self.revalidated_after_lock_wait
            )
        return detail


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
    work_id: str | None = None  # scheduler telemetry convenience (ADR-028)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown audit event kind: {self.kind!r}")
        # The public post-construction shape stays dict[str, Any], regardless
        # of whether the producer supplied a legacy mapping or typed details.
        self.detail = normalize_event_detail(self.detail)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuditEvent":
        """Load a current or legacy JSONL-compatible envelope mapping."""
        if not isinstance(value, Mapping):
            raise EventContractError("audit event must be loaded from a mapping")
        if "kind" not in value:
            raise EventContractError("audit event mapping is missing 'kind'")
        return cls(
            id=value.get("id") or new_id("evt"),
            ts=value.get("ts") or utcnow(),
            task_id=value.get("task_id"),
            step_id=value.get("step_id"),
            kind=value["kind"],
            actor=value.get("actor", "system"),
            success=bool(value.get("success", True)),
            detail=value.get("detail", {}),
            work_id=value.get("work_id"),
        )

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


@dataclass(frozen=True)
class SinkFailure:
    """Bounded diagnostic snapshot for one failed sink delivery."""

    sink: str
    required: bool
    error_type: str
    message: str
    event_id: str


@dataclass(frozen=True)
class _SinkBinding:
    sink: EventSink
    required: bool


class EventLogger:
    """Synchronous fan-out with explicit required/best-effort sinks.

    Existing positional and ``add_sink(sink)`` registrations are required and
    retain fail-fast behavior.  A best-effort failure is isolated and delivery
    continues; the bounded failure metadata is available via ``last_failures``.
    """

    def __init__(self, sinks: list[EventSink] | None = None):
        self._lock = RLock()
        self._sinks: list[_SinkBinding] = [
            _SinkBinding(sink=sink, required=True) for sink in (sinks or [])
        ]
        self._last_failures: tuple[SinkFailure, ...] = ()

    @property
    def last_failures(self) -> tuple[SinkFailure, ...]:
        """Failures from the most recently completed/failed emit attempt."""
        with self._lock:
            return self._last_failures

    def add_sink(self, sink: EventSink, *, required: bool = True) -> None:
        if not isinstance(required, bool):
            raise TypeError("required must be a bool")
        with self._lock:
            self._sinks.append(_SinkBinding(sink=sink, required=required))

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            bindings = tuple(self._sinks)
            self._last_failures = ()
        failures: list[SinkFailure] = []
        for binding in bindings:
            try:
                binding.sink.emit(event)
            except Exception as exc:
                failures.append(SinkFailure(
                    sink=type(binding.sink).__name__,
                    required=binding.required,
                    error_type=type(exc).__name__,
                    message=sanitize_error_text(exc, max_length=300),
                    event_id=event.id,
                ))
                with self._lock:
                    self._last_failures = tuple(failures)
                if binding.required:
                    # Required durability remains fail closed and preserves the
                    # original exception type for existing callers.
                    raise
                # Mirrors/diagnostics may be unavailable without breaking the
                # authoritative engine or preventing a later required sink.
                continue
        with self._lock:
            self._last_failures = tuple(failures)


class JsonlFileSink:
    """Appends events as newline-delimited JSON (for log tooling)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: AuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict()) + "\n")
