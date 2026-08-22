"""Typed runtime lifecycle and owned-resource cleanup (ADR-032).

Arion constructs components eagerly today.  This module therefore does not
invent asynchronous setup/start hooks: it makes the lifecycle Arion actually
has explicit.  The composition root registers resources that it owns; cleanup
runs once in reverse construction order and records a structured health
result.  Components injected by an embedding caller are borrowed unless that
caller explicitly registers them here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, Callable, Protocol, runtime_checkable


class LifecycleError(RuntimeError):
    """Raised when lifecycle ownership is invalid."""


class LifecycleState(str, Enum):
    """Stable states for an eagerly initialized Arion runtime."""

    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class HealthStatus(str, Enum):
    """Small, transport-safe health vocabulary."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ComponentHealth:
    """Health of one named runtime component, without sensitive internals."""

    name: str
    status: HealthStatus
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HealthReport:
    """Structured aggregate lifecycle/health result."""

    state: LifecycleState
    status: HealthStatus
    components: tuple[ComponentHealth, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "status": self.status.value,
            "components": [component.to_dict() for component in self.components],
        }


@runtime_checkable
class Lifecycle(Protocol):
    """Public lifecycle boundary for a composed runtime."""

    def shutdown(self, timeout: float = 30.0) -> HealthReport:
        """Stop work and release owned resources, idempotently."""
        ...

    def health(self) -> HealthReport:
        """Return a bounded, structured health snapshot."""
        ...


@dataclass
class _OwnedResource:
    name: str
    resource: Any
    close: Callable[[], None]


class ResourceLifecycle:
    """Own and close composition-root resources.

    Registration order is construction order.  Shutdown uses the inverse order
    so dependants close before the resources they depend upon.  All close
    callbacks are attempted even when one fails; failures become typed health
    state rather than preventing the remaining cleanup.
    """

    def __init__(self) -> None:
        self._state = LifecycleState.RUNNING
        self._resources: list[_OwnedResource] = []
        self._component_status: dict[str, ComponentHealth] = {}
        self._lock = RLock()
        self._final_report: HealthReport | None = None

    @property
    def state(self) -> LifecycleState:
        with self._lock:
            return self._state

    def register(
        self,
        name: str,
        resource: Any,
        close: Callable[[], None] | None = None,
    ) -> Any:
        """Register one resource owned by this composition.

        The resource is returned to keep construction concise.  Names and
        object identities are unique: aliasing the same closeable through two
        component names would risk a double close and is rejected.
        """
        if not isinstance(name, str) or not name.strip():
            raise LifecycleError("owned resource name must be a non-empty string")
        with self._lock:
            if self._state is not LifecycleState.RUNNING:
                raise LifecycleError(
                    f"cannot register resource {name!r} while lifecycle is "
                    f"{self._state.value}"
                )
            if any(owned.name == name for owned in self._resources):
                raise LifecycleError(f"owned resource name {name!r} is already registered")
            if any(owned.resource is resource for owned in self._resources):
                raise LifecycleError(f"resource {name!r} is already owned under another name")
            closer = close
            if closer is None:
                closer = getattr(resource, "close", None)
            if not callable(closer):
                raise LifecycleError(
                    f"owned resource {name!r} has no callable close operation"
                )
            self._resources.append(_OwnedResource(name, resource, closer))
            self._component_status[name] = ComponentHealth(
                name=name,
                status=HealthStatus.HEALTHY,
                detail="owned resource is active",
            )
        return resource

    def health(self) -> HealthReport:
        with self._lock:
            if self._final_report is not None:
                return self._final_report
            if self._state is LifecycleState.RUNNING:
                status = HealthStatus.HEALTHY
            elif self._state is LifecycleState.STOPPING:
                status = HealthStatus.DEGRADED
            elif self._state is LifecycleState.STOPPED:
                status = HealthStatus.STOPPED
            else:
                status = HealthStatus.UNHEALTHY
            return HealthReport(
                state=self._state,
                status=status,
                components=tuple(
                    self._component_status[owned.name] for owned in self._resources
                ),
            )

    def shutdown(self, timeout: float = 30.0) -> HealthReport:
        """Close every owned resource exactly once, in reverse order.

        ``timeout`` is accepted for the shared Lifecycle contract.  Closeable
        stores are synchronous and bounded by their own implementations, so it
        is intentionally not applied here.
        """
        del timeout
        # Keep the lock for the close sequence.  A concurrent shutdown waits
        # for the first one and then receives the same final report; it cannot
        # return while the other thread is still releasing resources.
        with self._lock:
            if self._final_report is not None:
                return self._final_report
            self._state = LifecycleState.STOPPING
            failed = False
            for owned in reversed(self._resources):
                try:
                    owned.close()
                except Exception as exc:  # cleanup must continue for all owners
                    failed = True
                    self._component_status[owned.name] = ComponentHealth(
                        name=owned.name,
                        status=HealthStatus.UNHEALTHY,
                        detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                else:
                    self._component_status[owned.name] = ComponentHealth(
                        name=owned.name,
                        status=HealthStatus.STOPPED,
                        detail="owned resource closed",
                    )
            self._state = LifecycleState.FAILED if failed else LifecycleState.STOPPED
            self._final_report = HealthReport(
                state=self._state,
                status=HealthStatus.UNHEALTHY if failed else HealthStatus.STOPPED,
                components=tuple(
                    self._component_status[owned.name] for owned in self._resources
                ),
            )
            return self._final_report
