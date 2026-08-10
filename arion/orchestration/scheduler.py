"""Bounded in-process step scheduler (ADR-024).

A small, explicit scheduler that dispatches independent WORK ITEMS (steps)
onto a bounded worker pool with deterministic test hooks and explicit
shutdown/join semantics.

Authority model:

- The scheduler is the ONLY source of worker lifecycle state (runnable /
  running / completed / failed / cancelled). Nothing in memory, cognition,
  strategy, model output, approval metadata, recovery metadata, queue
  position, or worker identity can manufacture concurrency authority or
  change a worker's lifecycle.
- The scheduler is NOT an authorization authority: it never decides whether
  a step may run against the world. Every dispatched step still passes
  through the engine's live authorization (policy + approval) and the
  durable mutation lock + FIFO queue before its capability executes.
- The scheduler does not hold any SQLite transaction while a work item runs;
  the engine's lock store commits before the capability executes.

Concurrency contract:

- configurable max_concurrency (default 1 = fully sequential);
- read-only independent items may run concurrently;
- mutating items serialize through the existing durable lock authority (the
  scheduler never re-implements locking);
- a blocked item (approval/lock/recovery) must NOT stall other ready items:
  the scheduler only dispatches items the caller marks ready, and the caller
  is free to keep dispatching ready items while another is blocked.
- cancellation is advisory BEFORE capability execution: once an item's
  capability has begun, cancellation cannot pretend it did not happen.

Implementation uses threads (reference), bounded by max_concurrency; no
subprocess, no shell, no daemon that survives shutdown().
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from arion.state.models import new_id, utcnow


class WorkStatus(str, Enum):
    RUNNABLE = "runnable"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SchedulerError(Exception):
    """Typed scheduler failure (fail closed)."""


@dataclass
class WorkItem:
    """One schedulable unit of work (a step or an arbitrary callable).

    Durable metadata is bounded: identifiers + timestamps only. Thread
    objects, stack traces, capability outputs, file contents, and model
    output are NEVER persisted.
    """

    id: str
    label: str                       # bounded identifier (e.g. "task:step")
    task_id: str
    step_index: int
    fn: Callable[[], Any]            # never persisted
    status: WorkStatus = WorkStatus.RUNNABLE
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    cancelled: bool = False
    created_at: str = field(default_factory=utcnow)
    # cancellation flag consumed by the worker when it picks the item up
    _cancel_evt: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label[:200],
            "task_id": self.task_id,
            "step_index": self.step_index,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": (self.error or "")[:500],
            "cancelled": self.cancelled,
            "created_at": self.created_at,
        }


class StepScheduler:
    """Bounded worker pool dispatching WorkItems.

    Deterministic test hooks: `clock` (callable -> ISO str) and `sleeper`
    (callable(seconds)) can be injected. `sleep_before_poll` lets a work
    item's fn yield the worker between dispatches (used by the engine to
    poll a waiting lock without busy-spinning).
    """

    def __init__(self, max_concurrency: int = 1, clock: Callable[[], str] | None = None,
                 sleeper: Callable[[float], None] | None = None):
        self.max_concurrency = max(1, int(max_concurrency))
        self.clock = clock or utcnow
        self.sleeper = sleeper or (lambda s: __import__("time").sleep(s))
        self._queue: list[WorkItem] = []
        self._running: dict[str, WorkItem] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._workers: list[threading.Thread] = []
        self._shutdown = False
        # Worker failures outside the item's own error handling (e.g. the
        # engine's injectable sleeper raising to simulate a crash) are
        # recorded and RE-RAISED by run_until_done in the caller thread so
        # the ADR-019..023 crash/restart contract is preserved: the durable
        # task state is left exactly as the worker last persisted it.
        self._errors: list[BaseException] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def enqueue(self, label: str, task_id: str, step_index: int,
                fn: Callable[[], Any]) -> WorkItem:
        """Enqueue a work item; returns the item (id stable)."""
        with self._lock:
            if self._shutdown:
                raise SchedulerError("scheduler is shut down")
            item = WorkItem(id=new_id("work"), label=label, task_id=task_id,
                            step_index=step_index, fn=fn)
            self._queue.append(item)
            self._cond.notify()
            return item

    def cancel(self, work_id: str) -> bool:
        """Advisory cancellation of a QUEUED (not yet running) item.
        Returns True if a queued item was cancelled. A running item cannot
        be cancelled (the engine's mutation-lock/recovery semantics own it)."""
        with self._lock:
            for item in self._queue:
                if item.id == work_id:
                    item.status = WorkStatus.CANCELLED
                    item.cancelled = True
                    item.completed_at = self.clock()
                    self._queue.remove(item)
                    self._cond.notify()
                    return True
            return False

    def cancel_all(self, predicate: Callable[[WorkItem], bool] | None = None) -> int:
        with self._lock:
            kept = []
            cancelled = 0
            for item in self._queue:
                if predicate is not None and not predicate(item):
                    kept.append(item)
                    continue
                item.status = WorkStatus.CANCELLED
                item.cancelled = True
                item.completed_at = self.clock()
                cancelled += 1
            self._queue = kept
            self._cond.notify()
            return cancelled

    def run_until_done(self, timeout: float | None = None) -> None:
        """Dispatch queued items onto bounded workers and wait until the
        queue is empty AND no item is running (or timeout).

        If a worker failed outside the item's own error handling, queued
        work is cancelled, running items are drained (no orphan work), and
        the FIRST recorded exception is re-raised in the caller thread."""
        deadline = None
        if timeout is not None:
            import time as _time

            deadline = _time.monotonic() + timeout
        self._spawn_workers()
        with self._lock:
            while True:
                if self._shutdown:
                    return
                if self._errors:
                    # stop dispatching: cancel queued items, then drain the
                    # running ones below before re-raising
                    for item in self._queue:
                        item.status = WorkStatus.CANCELLED
                        item.cancelled = True
                        item.completed_at = self.clock()
                    self._queue = []
                    self._cond.notify_all()
                if not self._queue and not self._running:
                    break
                if deadline is not None:
                    import time as _time

                    if _time.monotonic() >= deadline:
                        return
                    self._cond.wait(timeout=max(0.05, deadline - _time.monotonic()))
                else:
                    self._cond.wait(timeout=0.1)
        if self._errors:
            exc = self._errors[0]
            self._errors.clear()  # a re-raised crash is consumed by the caller
            raise exc

    def _spawn_workers(self) -> None:
        with self._lock:
            need = self.max_concurrency - len(self._workers)
        for _ in range(max(0, need)):
            t = threading.Thread(target=self._worker_loop, daemon=True,
                                 name=f"arion-step-worker-{len(self._workers)}")
            self._workers.append(t)
            t.start()

    def _next_item_locked(self) -> "WorkItem | None":
        """Pick the next runnable queued item (caller holds the lock). Uses a
        dedicated method so no loop variable in the worker retains the last
        examined WorkItem (whose fn closure captures the engine + task)."""
        for candidate in self._queue:
            if candidate.status == WorkStatus.RUNNABLE and not candidate.cancelled:
                item = candidate
                self._queue.remove(item)
                self._running[item.id] = item
                item.status = WorkStatus.RUNNING
                item.started_at = self.clock()
                return item
        return None

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    return
                item = self._next_item_locked()
                if item is None:
                    self._cond.wait(timeout=0.1)
                    continue
            try:
                if item._cancel_evt.is_set():
                    item.status = WorkStatus.CANCELLED
                    item.cancelled = True
                else:
                    item.fn()
                    item.status = WorkStatus.COMPLETED
            except BaseException as exc:  # item's fn handles its own errors;
                item.status = WorkStatus.FAILED  # this is a scheduler-level failure
                item.error = str(exc)[:500]
                with self._lock:
                    self._errors.append(exc)
                    self._cond.notify_all()
            finally:
                item.completed_at = self.clock()
                with self._lock:
                    self._running.pop(item.id, None)
                    self._cond.notify_all()
                # Release the WorkItem (and any engine/task it closes over)
                # promptly: an idle worker must not keep engines alive.
                item = None

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop accepting new work, join bounded workers. No orphan worker
        may continue mutating after shutdown returns."""
        with self._lock:
            self._shutdown = True
            self._cond.notify_all()
            # cancel queued (not running) work
            for item in self._queue:
                item.status = WorkStatus.CANCELLED
                item.cancelled = True
                item.completed_at = self.clock()
            self._queue = []
        for t in list(self._workers):
            t.join(timeout=timeout)
        self._workers = []

    # ------------------------------------------------------------------ #
    # observability (bounded)
    # ------------------------------------------------------------------ #

    def ready_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_concurrency": self.max_concurrency,
                "shutdown": self._shutdown,
                "queued": [i.to_dict() for i in self._queue],
                "running": [i.to_dict() for i in self._running.values()],
                "workers": len(self._workers),
            }
