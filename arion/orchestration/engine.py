"""Orchestration layer: the agent loop engine.

Ownership: the engine owns task lifecycle, state transitions, permissions,
execution, verification, checkpoints, recovery and completion. The LLM/model is
only ever an intelligence component the engine calls (via the ModelRouter) -
it never drives the loop (architectural rule, ADR-004).

Authorization (ADR-009): every step is authorized against the capability's
declared ActionSpec metadata (scope, risk, side effects) and the step's
parameters - never against a scope the plan merely claims. The policy returns
ALLOW | DENY | REQUIRE_APPROVAL; approval routes through an ApprovalHandler
seam so a human approval interface can be attached without touching the engine.

Execution semantics (ADR-010): step execution is AT-LEAST-ONCE - after a
crash, a resumed task re-executes the interrupted step. Automatic retries
within a step are permitted only for actions whose metadata marks them
retry-safe; non-retry-safe actions fail immediately so a partially applied
side effect is never blindly re-run.

Conceptual flow per task:
  Goal -> Task -> Plan -> Authorization -> Capability -> Observation
        -> Verification -> Checkpoint -> Complete/Recover
"""

from __future__ import annotations

import os
import threading as _threading
from dataclasses import dataclass
from typing import Any

from arion.capabilities.observations import normalize_observation
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.cognition.goals import GoalPlanLineageError
from arion.state.approvals import ApprovalError, ApprovalRequest, ApprovalStatus, ApprovalStore
from arion.state.locks import GOAL_RUN_RESOURCE_KIND
from arion.state.recovery import MutationRecovery, RecoveryError, RecoveryStatus
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.intelligence.plan_schema import PlanValidationError
from arion.intelligence.plan_validator import topo_sort_steps
from arion.intelligence.planner import Planner
from arion.intelligence.router import ModelRouter
from arion.observability.error_boundary import (
    ErrorSource,
    classify_error_source,
    sanitize_error_text,
    summarize_error,
)
from arion.observability.events import (
    AuditEvent,
    AuthorizationEventDetails,
    EventDetails,
    EventLogger,
)
from arion.resource_identifiers import (
    present_resource,
    present_resource_reason,
)
from arion.runtime.lifecycle import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    LifecycleState,
    ResourceLifecycle,
)
from arion.orchestration.authz import (
    Actor,
    ApprovalHandler,
    ApprovalOutcome,
    AuthorizationRequest,
    PermissionPolicy,
    PendingApprovalHandler,
    PolicyDecision,
    PolicyOutcome,
    ResourcePolicy,
)
from arion.state.models import (
    Checkpoint,
    Goal,
    GoalStateError,
    GoalStatus,
    PlanStep,
    StepStatus,
    TASK_TERMINAL_STATUSES,
    Task,
    TaskStateError,
    TaskStatus,
    new_id,
    utcnow,
)
from arion.state.store import DEFAULT_CHECKPOINT_RETENTION, Storage


def _iso_plus(iso: str, seconds: float) -> str:
    """ISO timestamp + seconds (lock wait deadlines; deterministic clock)."""
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


_RECOVERY_PERSISTENCE_MARKER = "mutation recovery persistence failed"


@dataclass
class _GoalRunLease:
    """Exact per-goal orchestration owner carried through an owned call.

    ``lock_id`` plus ``owner_id`` are the authority.  The heartbeat keeps that
    authority live; synchronous validation at execution boundaries decides
    whether the invocation may continue (ADR-052).
    """

    goal_id: str
    lock: Any | None
    heartbeat: Any = None
    lost: bool = False
    loss_event_emitted: bool = False

    # Preserve the private tuple-like seam used by existing deterministic
    # lease tests while production code carries the explicit guard object.
    def __getitem__(self, index: int) -> Any:
        return (self.lock, self.heartbeat)[index]

    def __iter__(self):
        return iter((self.lock, self.heartbeat))


class ArionEngine:
    """Drives goals through the full task lifecycle with checkpointing."""

    def __init__(
        self,
        storage: Storage,
        registry: CapabilityRegistry,
        planner: Planner,
        router: ModelRouter,
        events: EventLogger,
        policy: PermissionPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        actor: Actor | None = None,
        memory: Any | None = None,
        reflector: Any | None = None,
        cognition: Any | None = None,
        belief_deriver: Any | None = None,
        world_monitor: Any | None = None,
        strategy_selector: Any | None = None,
        goal_manager: Any | None = None,
        approval_store: ApprovalStore | None = None,
        approval_ttl_seconds: float | None = None,
        recovery_store: Any | None = None,
        mutation_lock_store: Any | None = None,
        mutation_lock_lease_seconds: float = 300.0,
        lock_clock: Any | None = None,
        lock_wait_max_seconds: float = 5.0,
        lock_wait_backoff_base: float = 0.25,
        lock_wait_backoff_max: float = 2.0,
        lock_sleeper: Any | None = None,
        lock_wait_observer: Any | None = None,
        max_concurrency: int = 1,
        scheduler: Any | None = None,
        scheduler_registry: Any | None = None,
        scheduler_lease_seconds: float = 300.0,
        scheduler_reclaim_on_start: bool = True,
        scheduler_global_max_concurrency: int | None = None,
        scheduler_max_lease_seconds: float | None = None,
        lifecycle: ResourceLifecycle | None = None,
    ):
        # Runtime ownership (ADR-032): dependencies passed directly to the
        # engine are borrowed by default.  The composition root supplies a
        # ResourceLifecycle containing only the resources it constructed and
        # therefore owns.  This keeps shutdown complete without unexpectedly
        # closing shared stores in manually assembled engines.
        self._resource_lifecycle = lifecycle or ResourceLifecycle()
        self._shutdown_lock = _threading.RLock()
        self._shutdown_complete = False
        self.storage = storage
        self.registry = registry
        self.planner = planner
        self.router = router
        self.events = events
        self.policy = policy or ResourcePolicy()
        self.approval_handler = approval_handler or PendingApprovalHandler()
        self.actor = actor or Actor.agent("system")
        self.memory = memory  # optional MemoryStore (ADR-012); None disables memory
        self.reflector = reflector  # optional Reflector; deterministic by default
        self.cognition = cognition  # optional CognitiveState facade (ADR-014)
        self.belief_deriver = belief_deriver  # optional BeliefDeriver; deterministic by default
        self.world_monitor = world_monitor  # optional WorldStateMonitor (ADR-015)
        self.strategy_selector = strategy_selector  # optional StrategySelector (ADR-015)
        self.goal_manager = goal_manager  # optional GoalManager (ADR-015)
        # Durable approval queue (ADR-018): the storage backend is the default
        # implementation; an explicit store can be injected (e.g. in tests).
        self.approval_store = approval_store
        if self.approval_store is None and hasattr(storage, "create_request"):
            self.approval_store = storage  # type: ignore[assignment]
        # Stale-PENDING retention (ADR-019): None = never expire; a positive
        # value expires PENDING requests older than now-ttl on demand.
        self.approval_ttl_seconds = approval_ttl_seconds
        # Durable mutation recovery registry (ADR-020): the storage backend is
        # the default implementation; an explicit store can be injected.
        self.recovery_store = recovery_store
        if self.recovery_store is None and hasattr(storage, "create_recovery"):
            self.recovery_store = storage  # type: ignore[assignment]
        # Advisory cross-process mutation locks (ADR-021): the storage backend
        # is the default implementation; an explicit store can be injected.
        # A lock is COORDINATION, never authorization - it is acquired only
        # AFTER live authorization succeeds and released on every terminal
        # path of the mutation.
        self.mutation_lock_store = mutation_lock_store
        if self.mutation_lock_store is None and hasattr(storage, "acquire"):
            self.mutation_lock_store = storage  # type: ignore[assignment]
        self.mutation_lock_lease_seconds = max(0.0, float(mutation_lock_lease_seconds))
        self.lock_clock = lock_clock  # injectable clock for deterministic lease tests
        # Bounded lock-contention waiting (ADR-022): coordination ONLY.
        # max<=0 disables waiting (contention fails immediately, ADR-021
        # semantics); otherwise the engine retries lock acquisition with
        # deterministic exponential backoff until the deadline.
        self.lock_wait_max_seconds = max(0.0, float(lock_wait_max_seconds))
        self.lock_wait_backoff_base = max(0.0, float(lock_wait_backoff_base))
        self.lock_wait_backoff_max = max(0.0, float(lock_wait_backoff_max))
        self.lock_sleeper = lock_sleeper  # injectable sleeper for deterministic tests
        self.lock_wait_observer = lock_wait_observer  # observability-only callback (ADR-023)
        # Bounded in-process step scheduler (ADR-024): default max_concurrency=1
        # reproduces the historical sequential behavior. The scheduler is the
        # ONLY source of worker lifecycle state; it is NOT an authorization
        # authority - every dispatched step still passes live authorization +
        # the durable mutation lock + FIFO queue before its capability runs.
        from arion.orchestration.scheduler import StepScheduler

        self.max_concurrency = max(1, int(max_concurrency))
        self.scheduler = scheduler or StepScheduler(
            max_concurrency=self.max_concurrency,
            clock=(lock_clock if callable(lock_clock) else None),
            sleeper=lock_sleeper,
        )
        # In-flight mutation locks held by THIS engine's running steps
        # (ADR-024 dispatch: a step whose resource is already held by a
        # running step of this task is not dispatched in the same round when
        # waiting is disabled). Coordination only - never authorization.
        self._inflight_locks: set[tuple[str, str]] = set()
        self._inflight_lock = _threading.RLock()
        # Durable scheduler/work registry (ADR-025): the storage backend is
        # the default implementation; an explicit store can be injected. The
        # registry is the only source of durable worker-lifecycle state and
        # is COORDINATION - never authorization (every dispatched step still
        # passes live authorization -> approval -> durable lock -> FIFO).
        self.scheduler_registry = scheduler_registry
        if self.scheduler_registry is None and hasattr(storage, "create"):
            self.scheduler_registry = storage  # type: ignore[assignment]
        self.scheduler_lease_seconds = max(0.0, float(scheduler_lease_seconds))
        self.scheduler_max_lease_seconds = (
            max(0.0, float(scheduler_max_lease_seconds))
            if scheduler_max_lease_seconds is not None
            else self.scheduler_lease_seconds * 10.0)
        # ADR-026: optional durable CROSS-PROCESS capacity. When configured
        # (in the shared registry), every atomic claim enforces it, so N
        # engine processes cannot turn the per-engine limit into N x limit.
        if (scheduler_global_max_concurrency is not None
                and self.scheduler_registry is not None
                and hasattr(self.scheduler_registry, "set_scheduler_global_max")):
            try:
                self.scheduler_registry.set_scheduler_global_max(
                    int(scheduler_global_max_concurrency))
            except Exception:
                pass
        # This engine's durable scheduler identity: on (re)start, QUEUED rows
        # whose scheduler has NO live registration (presumed dead) are
        # abandoned and stale RUNNING leases are reclaimed, so no immortal
        # RUNNING worker and no dead process's queue can ever be mistaken
        # for live work - while a LIVE peer's queue is never touched
        # (ADR-026 registration liveness).
        self.scheduler_id = new_id("sched")
        self._registered = False
        if self.scheduler_registry is not None and scheduler_reclaim_on_start:
            try:
                self.scheduler_registry.reclaim_stale(now=self._lock_now())
                self.scheduler_registry.abandon_foreign_queued(self.scheduler_id)
                if hasattr(self.scheduler_registry, "register_scheduler"):
                    self.scheduler_registry.register_scheduler(
                        self.scheduler_id, pid=os.getpid(),
                        lease_seconds=self.scheduler_lease_seconds,
                        now=self._lock_now())
                    self._registered = True
            except Exception:
                pass
        # In-flight claimed work of THIS engine (work_id -> (task_id,
        # step_index)): lets _admit_step distinguish its own RUNNING rows
        # from foreign/forged ones. Coordination only - never authorization.
        self._claimed_work: dict[str, tuple[str, int]] = {}
        # Round-progress flag for cross-process capacity clean stops.
        self._last_run_progress = True
        # The goal manager's lock_contention recheck resolves via the engine's
        # live lock store (the lock store is the only lock authority).
        if self.goal_manager is not None:
            try:
                self.goal_manager.lock_contention_resolver = self._lock_contention_resolver
                self.goal_manager.recovery_required_resolver = self._recovery_blocker_resolver
            except Exception:
                pass

    # ---------- mutation recovery (ADR-020) ----------

    def _has_open_recovery(self, goal_id: str | None) -> bool:
        """True when the goal has any unacknowledged mutation-recovery record.

        Recovery is a durable gate, NOT an authorization decision: it only
        means 'a previous non-retry-safe mutation failed and needs explicit
        handling'. It never authorizes anything.
        """
        if self.recovery_store is None or not goal_id:
            return False
        return any(
            r.status == RecoveryStatus.REQUIRED
            for r in self.recovery_store.list_recoveries(goal_id=goal_id)
        )

    def _recovery_blocker_resolver(self, blocker: dict) -> bool:
        """A recovery blocker clears only after all REQUIRED rows are gone."""
        if self.recovery_store is None:
            return False
        try:
            recovery_id = blocker.get("recovery_id")
            record = (
                self.recovery_store.get_recovery(recovery_id)
                if recovery_id else None
            )
            if record is not None and record.status == RecoveryStatus.REQUIRED:
                return False
            goal_id = record.goal_id if record is not None else None
            if not goal_id:
                task_id = blocker.get("task_id")
                task = self.storage.load_task(task_id) if task_id else None
                goal_id = task.goal_id if task is not None else None
            return bool(goal_id) and not self._has_open_recovery(goal_id)
        except Exception:
            return False

    def _reconcile_missing_recovery_records(self, goal_id: str) -> None:
        """Repair a task-only fallback after recovery-table unavailability."""
        if self.recovery_store is None:
            return
        for task in self.storage.list_tasks(status=TaskStatus.FAILED.value):
            if task.goal_id != goal_id:
                continue
            step = task.active_step or next(
                (candidate for candidate in task.steps
                 if candidate.status == StepStatus.FAILED),
                None,
            )
            text = " ".join(filter(None, [task.error,
                                           step.error if step else None]))
            if _RECOVERY_PERSISTENCE_MARKER not in text or step is None:
                continue
            spec = self.registry.action_spec(step.capability, step.action)
            if spec is None or getattr(spec, "side_effects", "") != "mutating":
                continue
            self._record_recovery_required(task, step, spec, step.error or task.error or text)

    def _fence_task_on_open_recovery(self, task: Task) -> bool:
        """Fail a non-terminal task closed when REQUIRED recovery exists.

        ``run_goal`` already gates fresh work by goal.  This task-level check
        closes the recovery-row-before-task-snapshot crash window and protects
        callers that resume a task directly (ADR-040).
        """
        if task.status in TASK_TERMINAL_STATUSES or self.recovery_store is None:
            return False
        try:
            own = [
                record for record in self.recovery_store.list_recoveries(
                    task_id=task.id
                )
                if record.status == RecoveryStatus.REQUIRED
            ]
            blocked = bool(own) or self._has_open_recovery(task.goal_id)
        except Exception:
            blocked = True  # inability to verify recovery state fails closed
        if not blocked:
            return False
        reason = "mutation recovery required; task execution is fenced"
        step = task.active_step
        if step is not None and step.status in (
                StepStatus.PENDING, StepStatus.RUNNING):
            step.status = StepStatus.FAILED
            step.error = reason
        task.status = TaskStatus.FAILED
        task.error = reason
        task.completed_at = utcnow()
        try:
            self.storage.save_task(task)
        except TaskStateError:
            canonical = self.storage.load_task(task.id)
            if canonical is not None:
                task.__dict__.update(canonical.__dict__)
        self._cancel_waiters_for_task(task)
        self._emit("task.failed", task_id=task.id, detail={
            "step_index": step.index if step is not None else None,
            "error": reason,
            "recovery_fenced": True,
        })
        return True

    def _record_recovery_required(self, task: Task, step: PlanStep, spec,
                                  reason: str) -> MutationRecovery | None:
        """Durably commit recovery authority and its failed-task mirror.

        The default SQLite store performs the recovery create/adopt and task
        revision transition in one transaction.  A stale task CAS never rolls
        back recovery authority. Alternate stores retain create-then-save
        compatibility; the durable recovery record still wins that split.
        """
        if self.recovery_store is None:
            return None
        bounded_reason = (reason or "mutation failed; recovery required")[:500]
        if step.status != StepStatus.FAILED:
            step.status = StepStatus.FAILED
            step.result = None
        step.error = step.error or bounded_reason
        task.status = TaskStatus.FAILED
        task.error = step.error
        task.completed_at = task.completed_at or utcnow()
        resource = (
            step.params.get(spec.resource_param)
            if getattr(spec, "resource_param", None) else None
        )
        candidate = MutationRecovery(
            recovery_id=new_id("recovery"),
            task_id=task.id,
            goal_id=task.goal_id,
            step_index=step.index,
            capability=step.capability,
            action=step.action,
            resource=resource if isinstance(resource, str) else None,
            reason=bounded_reason,
        )
        created = False
        expected_revision = task.revision
        commit = getattr(
            self.recovery_store, "commit_recovery_requirement", None
        )
        try:
            if callable(commit):
                rec, created, task_committed = commit(
                    candidate, task, expected_revision
                )
                if not task_committed:
                    canonical = self.storage.load_task(task.id)
                    if canonical is not None:
                        task.__dict__.update(canonical.__dict__)
            else:
                existing = [
                    record for record in self.recovery_store.list_recoveries(
                        task_id=task.id
                    )
                    if (record.step_index == step.index
                        and record.status == RecoveryStatus.REQUIRED)
                ]
                if existing:
                    rec = existing[0]
                else:
                    rec = self.recovery_store.create_recovery(candidate) or candidate
                    created = rec.recovery_id == candidate.recovery_id
                try:
                    self.storage.save_task(task)
                except TaskStateError:
                    canonical = self.storage.load_task(task.id)
                    if canonical is not None:
                        task.__dict__.update(canonical.__dict__)
        except Exception as atomic_error:
            # If the combined write failed because the task companion could
            # not commit, retain recovery authority independently. This is the
            # Phase 32 fail-safe direction: recovery may exist without its task
            # mirror, never the reverse.
            try:
                rec = self.recovery_store.create_recovery(candidate) or candidate
                created = rec.recovery_id == candidate.recovery_id
            except Exception:
                # Last durable fallback when the recovery table itself is
                # unavailable: terminalize the task with a repair marker. On a
                # later run_goal, reconciliation recreates the REQUIRED row
                # before any replan can execute.
                task.status = TaskStatus.FAILED
                task.error = sanitize_error_text(
                    f"{_RECOVERY_PERSISTENCE_MARKER}; recovery required: "
                    f"{bounded_reason}",
                    max_length=500,
                )
                step.status = StepStatus.FAILED
                step.error = task.error
                task.completed_at = task.completed_at or utcnow()
                try:
                    self.storage.save_task(task)
                except TaskStateError:
                    canonical = self.storage.load_task(task.id)
                    if canonical is not None:
                        task.__dict__.update(canonical.__dict__)
                raise atomic_error
            try:
                self.storage.save_task(task)
            except Exception:
                canonical = self.storage.load_task(task.id)
                if canonical is not None:
                    task.__dict__.update(canonical.__dict__)

        if created:
            self._emit("recovery.required", task_id=task.id, step_id=_step_id(step),
                       success=False, detail={
                           "recovery_id": rec.recovery_id,
                           "task_id": rec.task_id,
                           "step_index": rec.step_index,
                           "goal_id": rec.goal_id,
                           "capability": rec.capability,
                           "action": rec.action,
                           "resource": rec.resource,
                           "reason": rec.reason[:200],
                       })
        # Durable goal gate: no fresh plan/task until this recovery is
        # explicitly acknowledged. Idempotent by blocker key. A crash before
        # this mirror is repaired by recovery_required_resolver/_block_on_open.
        if self.goal_manager is not None and task.goal_id:
            try:
                self.goal_manager.set_blocked(task.goal_id, {
                    "type": "recovery_required",
                    "task_id": task.id,
                    "step_index": step.index,
                    "capability": step.capability,
                    "action": step.action,
                    "resource": resource,
                    "recovery_id": rec.recovery_id,
                    "reason": "mutation failed; recovery required (non-retry-safe)",
                }, reason="recovery_required")
            except Exception:
                pass
        if task.status == TaskStatus.FAILED:
            self._emit("task.failed", task_id=task.id, detail={
                "step_index": step.index,
                "error": task.error,
                "recovery_required": True,
            })
            self._record_memory(task)
        return rec

    def acknowledge_recovery(self, recovery_id: str, actor: str = "operator") -> MutationRecovery:
        """Explicit, durable recovery transition (ADR-020).

        `RECOVERY_REQUIRED -> RECOVERY_ACKNOWLEDGED` recorded by an explicit
        caller. It ONLY records 'the previous failed mutation has been handled
        and the goal may plan again' - it cannot execute a capability, cannot
        grant authorization, cannot reuse or resurrect approvals, and cannot
        erase the mutation-failure history (the record + audit trail persist).
        """
        if self.recovery_store is None:
            raise RecoveryError("recovery registry is not available on this engine")
        rec = self.recovery_store.get_recovery(recovery_id)
        if rec is None:
            raise RecoveryError(f"unknown recovery id: {recovery_id}")
        if rec.status != RecoveryStatus.REQUIRED:
            raise RecoveryError(f"recovery {recovery_id} is already {rec.status.value}")
        rec.status = RecoveryStatus.ACKNOWLEDGED
        rec.acknowledged_at = utcnow()
        rec.acknowledged_by = actor
        transition = getattr(self.recovery_store, "transition_recovery", None)
        if not callable(transition):
            raise RecoveryError(
                "recovery store lacks conditional acknowledgement support "
                "(fail closed)"
            )
        if not transition(rec, RecoveryStatus.REQUIRED):
            actual = self.recovery_store.get_recovery(recovery_id)
            state = actual.status.value if actual is not None else "missing"
            if actual is not None and actual.status == RecoveryStatus.ACKNOWLEDGED:
                raise RecoveryError(
                    f"recovery {recovery_id} is already acknowledged"
                )
            raise RecoveryError(
                f"recovery {recovery_id} acknowledgement conflicts with "
                f"durable state {state}"
            )
        self._emit("recovery.acknowledged", task_id=rec.task_id,
                   step_id=f"{rec.task_id}:{rec.step_index}", detail={
                       "recovery_id": rec.recovery_id,
                       "goal_id": rec.goal_id,
                       "task_id": rec.task_id,
                       "step_index": rec.step_index,
                       "capability": rec.capability,
                       "action": rec.action,
                       "resource": rec.resource,
                       "acknowledged_by": actor,
                   })
        # Clear the goal's recovery gate once NO open recoveries remain.
        if self.goal_manager is not None and rec.goal_id:
            try:
                if not self._has_open_recovery(rec.goal_id):
                    self.goal_manager.clear_blocker(rec.goal_id, "recovery_required",
                                                    reason="recovery_acknowledged")
            except Exception:
                pass
        return self.recovery_store.get_recovery(recovery_id)

    # ---------- per-goal run ownership (ADR-045/052) ----------

    @staticmethod
    def _goal_run_claim_parts(claim) -> tuple[Any | None, Any]:
        if isinstance(claim, _GoalRunLease):
            return claim.lock, claim.heartbeat
        return claim[0], claim[1]

    def _emit_goal_run_ownership_lost(
        self,
        goal_id: str,
        claim,
        phase: str,
        error: BaseException | None = None,
    ) -> None:
        """Record one bounded loss event without changing domain state."""
        lock, _heartbeat = self._goal_run_claim_parts(claim)
        if isinstance(claim, _GoalRunLease):
            claim.lost = True
            if claim.loss_event_emitted:
                return
            claim.loss_event_emitted = True
        try:
            self._emit("goal.run.ownership_lost", task_id=None,
                       success=False, detail={
                           "goal_id": goal_id,
                           "lock_id": getattr(lock, "lock_id", None),
                           "owner_id": getattr(lock, "owner_id", None),
                           "phase": phase[:100],
                           "error_type": (
                               type(error).__name__ if error is not None
                               else "GoalRunOwnershipLost"
                           ),
                       })
        except Exception:
            pass

    def _goal_run_lease_current(
        self,
        goal_id: str | None,
        claim,
        phase: str,
    ) -> bool:
        """Synchronously validate the exact live goal-run owner.

        The check renews the same ``lock_id``/``owner_id`` acquired by this
        invocation.  A different current lease for the goal never satisfies
        it.  ``None`` retains compatibility for private/standalone paths that
        do not participate in default SQLite goal-run coordination.
        """
        if claim is None:
            return True
        lock, _heartbeat = self._goal_run_claim_parts(claim)
        if lock is None:
            return True  # alternate minimal stores retain single-process use
        if isinstance(claim, _GoalRunLease):
            if claim.lost:
                return False
            if claim.goal_id != goal_id:
                self._emit_goal_run_ownership_lost(
                    str(goal_id or ""), claim, phase,
                    ValueError("goal-run claim belongs to another goal"),
                )
                return False
        if (not goal_id
                or getattr(lock, "resource_kind", None) != GOAL_RUN_RESOURCE_KIND
                or getattr(lock, "resource", None) != goal_id):
            self._emit_goal_run_ownership_lost(
                str(goal_id or ""), claim, phase,
                ValueError("goal-run claim identity mismatch"),
            )
            return False
        try:
            renewed = self._renew_mutation_lock(
                lock, lease_seconds=self.scheduler_lease_seconds
            )
            lock.expires_at = renewed.expires_at
            return True
        except Exception as exc:
            self._emit_goal_run_ownership_lost(goal_id, claim, phase, exc)
            return False

    def _goal_run_allows_task(self, task: Task, claim, phase: str) -> bool:
        current = self._goal_run_lease_current(task.goal_id, claim, phase)
        if not current:
            # Coordination-only marker; Task serialization ignores it. Workers
            # use it to avoid advertising a skipped invocation as completed.
            setattr(task, "_goal_run_ownership_lost", True)
        return current

    def _acquire_goal_run_lease(self, goal_id: str):
        """Claim one durable goal-run owner, or return None on contention.

        This internal namespace reuses the proven SQLite advisory-lease
        primitive. It is coordination only and never substitutes for task,
        scheduler, approval, recovery, or mutation-resource authority.
        """
        if self.mutation_lock_store is None:
            # Compatibility guard for alternate minimal stores: no durable
            # lease exists, so historical single-process behavior remains.
            return _GoalRunLease(goal_id=goal_id, lock=None)
        from arion.state.locks import MutationLockError

        owner = self._lock_owner()
        try:
            lock = self.mutation_lock_store.acquire(
                GOAL_RUN_RESOURCE_KIND,
                goal_id,
                "orchestration.goal",
                "run",
                owner,
                lease_seconds=self.scheduler_lease_seconds,
                now=self._lock_now(),
            )
        except MutationLockError:
            try:
                self._emit("goal.run.contended", task_id=None, success=False,
                           detail={"goal_id": goal_id})
            except Exception:
                pass
            return None
        heartbeat = self._start_lock_heartbeat(
            lock, lease_seconds=self.scheduler_lease_seconds
        )
        try:
            self._emit("goal.run.claimed", task_id=None, detail={
                "goal_id": goal_id,
                "lock_id": lock.lock_id,
                "owner_id": lock.owner_id,
            })
        except Exception:
            self._stop_lock_heartbeat(heartbeat)
            try:
                self.mutation_lock_store.release(lock.lock_id, lock.owner_id)
            except Exception:
                pass
            raise
        return _GoalRunLease(
            goal_id=goal_id, lock=lock, heartbeat=heartbeat
        )

    def _release_goal_run_lease(self, goal_id: str, claim) -> None:
        if claim is None:
            return
        lock, heartbeat = self._goal_run_claim_parts(claim)
        if lock is None:
            return
        state = self._stop_lock_heartbeat(heartbeat)
        if state.get("error") is not None:
            self._emit_goal_run_ownership_lost(
                goal_id, claim, "heartbeat", state["error"]
            )
        try:
            released = self.mutation_lock_store.release(
                lock.lock_id, lock.owner_id
            )
            if released:
                self._emit(
                    "goal.run.released", task_id=None, success=True,
                    detail={
                        "goal_id": goal_id,
                        "lock_id": lock.lock_id,
                        "owner_id": lock.owner_id,
                    },
                )
            else:
                self._emit_goal_run_ownership_lost(
                    goal_id, claim, "release"
                )
        except Exception as exc:
            try:
                self._emit("error", task_id=None, success=False, detail={
                    "category": "goal_run_release",
                    "error_type": type(exc).__name__,
                    "error": "goal run lease release failed",
                    "goal_id": goal_id,
                })
            except Exception:
                pass

    # ---------- advisory mutation locks (ADR-021) ----------

    def _lock_now(self) -> str:
        """Clock for lock lease timestamps (injectable for deterministic
        tests; defaults to the real clock)."""
        if self.lock_clock is not None:
            return str(self.lock_clock())
        return utcnow()

    def _lock_sleep(self, seconds: float) -> None:
        """Sleep between lock-retry attempts (injectable for deterministic
        tests; defaults to the real clock). Never holds a SQLite transaction
        while sleeping - the lock store commits before this is called."""
        if self.lock_sleeper is not None:
            self.lock_sleeper(seconds)
        else:
            import time

            time.sleep(seconds)

    def _lock_owner(self) -> str:
        """Explicit, unique owner/process identity for this engine's locks."""
        return f"proc:{os.getpid()}:{new_id('owner')}"

    def _renew_mutation_lock(self, lock, lease_seconds: float | None = None):
        renew = getattr(self.mutation_lock_store, "renew", None)
        if not callable(renew):
            return lock  # compatibility for alternate legacy stores
        lease = (
            self.mutation_lock_lease_seconds
            if lease_seconds is None else max(0.0, float(lease_seconds))
        )
        renewed = renew(
            lock.lock_id, lock.owner_id,
            lease_seconds=lease,
            now=self._lock_now(),
        )
        lock.expires_at = renewed.expires_at
        return renewed

    def _start_lock_heartbeat(self, lock,
                              lease_seconds: float | None = None):
        """Renew one live advisory owner while blocking code runs."""
        lease = (
            self.mutation_lock_lease_seconds
            if lease_seconds is None else max(0.0, float(lease_seconds))
        )
        if (lock is None or self.mutation_lock_store is None
                or not callable(getattr(self.mutation_lock_store, "renew", None))
                or lease <= 0):
            return None
        stop = _threading.Event()
        state: dict[str, Any] = {"error": None}
        interval = max(0.01, min(5.0, lease / 3.0))

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    self._renew_mutation_lock(lock, lease_seconds=lease)
                except Exception as exc:
                    state["error"] = exc
                    return

        thread = _threading.Thread(
            target=heartbeat,
            daemon=True,
            name=f"arion-lock-heartbeat-{lock.lock_id}",
        )
        thread.start()
        return stop, thread, state

    @staticmethod
    def _stop_lock_heartbeat(heartbeat) -> dict[str, Any]:
        if heartbeat is None:
            return {"error": None}
        stop, thread, state = heartbeat
        stop.set()
        thread.join(timeout=5.0)
        return state

    def _lock_wait_cancelled(self, task: Task) -> bool:
        if self._goal_is_terminal_for_approval(task):
            return True
        try:
            durable = self.storage.load_task(task.id)
            return durable is not None and durable.status in (
                TaskStatus.COMPLETED, TaskStatus.FAILED,
            )
        except Exception:
            return True

    def _lock_canonical(self, spec, step: PlanStep) -> tuple[str | None, str | None]:
        """Canonical lock identity: (resource_kind, canonical resource).

        Keyed by the canonical security-relevant resource - never an
        arbitrary display string - so write/append (and any path spelling)
        contend on the same underlying resource.
        """
        kind = getattr(spec, "resource_kind", None)
        param = getattr(spec, "resource_param", None)
        if not kind or not param:
            return None, None
        resource = step.params.get(param)
        if not isinstance(resource, str) or not resource:
            return kind, None
        from arion.state.locks import canonical_resource

        return kind, canonical_resource(kind, resource)

    def _lock_is_active(self, resource_kind: str | None, resource: str | None) -> bool:
        """True when a non-expired lock exists for the canonical resource."""
        if self.mutation_lock_store is None or not resource_kind or not resource:
            return False
        now = self._lock_now()
        try:
            return any(
                lock.resource_kind == resource_kind
                and lock.resource == resource
                and lock.expires_at > now
                for lock in self.mutation_lock_store.list(resource_kind=resource_kind,
                                                          resource=resource)
            )
        except Exception:
            return True  # fail closed: cannot verify -> assume contended

    def _lock_contention_resolver(self, blocker: dict) -> bool:
        """GoalManager recheck hook: a lock_contention blocker clears when the
        resource it names is no longer actively locked."""
        kind = blocker.get("resource_kind")
        resource = blocker.get("resource")
        if not kind or not resource:
            return False
        return not self._lock_is_active(kind, resource)

    def _acquire_mutation_lock(
        self, task: Task, step: PlanStep, spec, goal_run_claim=None
    ) -> Any | None:
        """Acquire the advisory mutation lock for a step's canonical resource.

        Called ONLY after authorization succeeded (live policy + approval).
        A lock is coordination, not permission: acquiring it never grants
        anything, it only prevents a concurrent mutation of the same resource.

        ADR-022: when bounded waiting is configured (lock_wait_max_seconds>0),
        a contention is retried with deterministic exponential backoff until
        the deadline instead of failing immediately. Waiting retries the
        COORDINATION ONLY - never the mutation, never the plan, never the
        approval. The wait state (deadline/attempts/next_retry) is persisted
        durably on the task and survives restarts without resetting the retry
        budget. Deadline expiry raises MutationLockTimeoutError (typed).

        ADR-023: with bounded waiting, the task first joins a DURABLE FIFO
        queue for the canonical resource. The queue decides who gets the
        OPPORTUNITY to acquire (oldest eligible waiter first) - it can never
        grant authorization. Acquisition is head-gated atomically inside the
        store, so a newer waiter can never overtake an older one, and the
        queue position survives restarts.

        Returns (lock, waited): `waited` is True when this session contended
        at least once before acquiring (callers must re-validate live
        authorization in that case). Raises MutationLockError on contention
        (when waiting is disabled) / MutationLockTimeoutError on deadline
        expiry.
        """
        from arion.state.locks import MutationLockError, MutationLockTimeoutError

        if not self._goal_run_allows_task(
                task, goal_run_claim, "mutation lock acquisition"):
            return None, False
        kind, resource = self._lock_canonical(spec, step)
        if kind is None or resource is None:
            return None, False  # no lockable resource (e.g. non-resource mutation)

        self._emit("mutation.lock.requested", task_id=task.id, step_id=_step_id(step), detail={
            "resource_kind": kind,
            "resource": resource,
            "capability": step.capability,
            "action": step.action,
        })
        owner = self._lock_owner()

        # Waiting disabled: immediate, durable contention failure (ADR-021
        # semantics preserved - no queue, no waiter rows).
        if self.lock_wait_max_seconds <= 0:
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "immediate mutation lock"):
                return None, False
            try:
                lock = self.mutation_lock_store.acquire(
                    kind, resource, step.capability, step.action, owner,
                    lease_seconds=self.mutation_lock_lease_seconds,
                    now=self._lock_now())
            except MutationLockError:
                self._emit("mutation.lock.contended", task_id=task.id,
                           step_id=_step_id(step), success=False, detail={
                               "resource_kind": kind, "resource": resource,
                               "capability": step.capability, "action": step.action,
                           })
                raise
            self._emit("mutation.lock.acquired", task_id=task.id, step_id=_step_id(step), detail={
                "lock_id": lock.lock_id, "resource_kind": kind, "resource": resource,
                "capability": step.capability, "action": step.action, "owner_id": owner,
                "waited": False,
            })
            self._track_inflight_lock(kind, resource, True)
            return lock, False

        # ---- bounded waiting + durable FIFO queue (ADR-022/023) ----

        # Waiting budget: preserve across restarts (never reset). A fresh wait
        # session (different resource, a DIFFERENT step of the same task, or
        # no prior metadata) starts a new one. task.lock_wait is a single
        # task-level slot: under ADR-024 concurrent dispatch two steps of the
        # same task may wait on the same resource at once, so waiter identity
        # is scoped per STEP (step_index in the metadata) - a step can never
        # reuse a sibling's waiter row.
        prior = None
        if (task.lock_wait or {}).get("resource") == resource \
                and (task.lock_wait or {}).get("step_index") == step.index:
            prior = task.lock_wait
        deadline = prior.get("deadline") if prior else None
        attempts = int(prior.get("attempts", 0)) if prior else 0
        now0 = self._lock_now()
        if deadline is None:
            deadline = _iso_plus(now0, self.lock_wait_max_seconds)

        # Durable queue membership: reuse the persisted waiter on restart
        # (position preserved); otherwise enqueue a new one atomically.
        waiter_id = prior.get("waiter_id") if prior else None
        waiter = None
        if waiter_id is not None:
            waiter = self.mutation_lock_store.get_waiter(waiter_id)
            if waiter is None or waiter.status.value != "queued":
                waiter = None  # stale/terminal: enqueue fresh
        # The DURABLE WAITER ROW is the deadline authority (ADR-025 Phase H):
        # a task-level lock_wait deadline that was forged (e.g. far in the
        # future) can never extend the wait - the waiter row's deadline,
        # written by the store with this engine's clock, wins. A legitimate
        # preserved wait keeps its exact budget (never reset).
        if waiter is not None:
            deadline = waiter.deadline
        if waiter is None:
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "mutation waiter publication"):
                return None, False
            waiter = self.mutation_lock_store.enqueue_waiter(
                kind, resource, task.id, task.goal_id, step.index, deadline, now=now0)
            waiter_id = waiter.waiter_id
            deadline = waiter.deadline
            self._emit("mutation.lock.queued", task_id=task.id, step_id=_step_id(step),
                       success=False, detail={
                           "waiter_id": waiter.waiter_id,
                           "resource_kind": kind, "resource": resource,
                           "capability": step.capability, "action": step.action,
                           "position": waiter.seq, "deadline": deadline,
                       })

        while True:
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "mutation lock wait"):
                return None, False
            now = self._lock_now()
            if self._lock_wait_cancelled(task):
                self.mutation_lock_store.dequeue_waiter(
                    waiter_id, "cancelled"
                )
                task.lock_wait = None
                step.status = StepStatus.FAILED
                step.error = "mutation lock wait cancelled by terminal task/goal"
                task.status = TaskStatus.FAILED
                task.error = step.error
                task.completed_at = utcnow()
                self.storage.save_task(task)
                raise MutationLockTimeoutError(step.error)
            if now >= deadline:
                # durable, typed, explainable timeout - coordination only
                self.mutation_lock_store.dequeue_waiter(waiter_id, "timed_out")
                self._fail_lock_wait_timeout(task, step, kind, resource, deadline,
                                             attempts, owner, waiter_id)
                raise MutationLockTimeoutError(
                    f"mutation lock wait timed out for {kind!r} {resource!r} "
                    f"(deadline {deadline}, attempts {attempts})"
                )
            try:
                lock = self.mutation_lock_store.acquire(
                    kind, resource, step.capability, step.action, owner,
                    lease_seconds=self.mutation_lock_lease_seconds,
                    now=now, waiter_id=waiter_id)
                if not self._goal_run_allows_task(
                        task, goal_run_claim,
                        "post-wait coordination cleanup"):
                    # Store acquire already removed this waiter from the queue.
                    # Release only our exact mutation owner and leave goal/task
                    # mirrors untouched for the current goal runner.
                    if hasattr(
                            self.mutation_lock_store,
                            "release_and_select_next"):
                        self.mutation_lock_store.release_and_select_next(
                            lock.lock_id, lock.owner_id,
                            now=self._lock_now(),
                        )
                    else:
                        self.mutation_lock_store.release(
                            lock.lock_id, lock.owner_id
                        )
                    return None, False
                # success: leave the queue, clear the durable wait state +
                # goal blocker
                self.mutation_lock_store.dequeue_waiter(waiter_id, "acquired")
                task.lock_wait = None
                if self.goal_manager is not None and task.goal_id:
                    try:
                        self.goal_manager.clear_blocker(task.goal_id, "lock_contention",
                                                        reason="lock_acquired")
                    except Exception:
                        pass
                self._emit("mutation.lock.acquired", task_id=task.id, step_id=_step_id(step), detail={
                    "lock_id": lock.lock_id,
                    "resource_kind": kind,
                    "resource": resource,
                    "capability": step.capability,
                    "action": step.action,
                    "owner_id": owner,
                    "waiter_id": waiter_id,
                    "waited": attempts > 0,
                })
                self._track_inflight_lock(kind, resource, True)
                return lock, attempts > 0
            except MutationLockError:
                # bounded waiting: persist + backoff, then retry coordination
                # (never the mutation/plan/approval)
                attempts += 1
                backoff = min(
                    self.lock_wait_backoff_base * (2 ** min(attempts - 1, 20)),
                    self.lock_wait_backoff_max,
                )
                next_retry = _iso_plus(now, backoff)
                task.lock_wait = {
                    "step_index": step.index,
                    "resource_kind": kind,
                    "resource": resource,
                    "waiter_id": waiter_id,
                    "position": waiter.seq,
                    "deadline": deadline,
                    "attempts": attempts,
                    "next_retry": next_retry,
                }
                self.mutation_lock_store.update_waiter(waiter_id,
                                                       attempts=attempts,
                                                       next_retry=next_retry)
                self._persist_lock_wait(task, step, kind, resource, deadline,
                                        attempts, next_retry, waiter_id, waiter.seq)
                if attempts == 1:
                    self._emit("mutation.lock.waiting", task_id=task.id,
                               step_id=_step_id(step), success=False, detail={
                                   "resource_kind": kind, "resource": resource,
                                   "capability": step.capability, "action": step.action,
                                   "waiter_id": waiter_id, "position": waiter.seq,
                                   "deadline": deadline, "next_retry": next_retry,
                               })
                else:
                    self._emit("mutation.lock.retry", task_id=task.id,
                               step_id=_step_id(step), success=False, detail={
                                   "resource_kind": kind, "resource": resource,
                                   "capability": step.capability, "action": step.action,
                                   "waiter_id": waiter_id, "position": waiter.seq,
                                   "attempt": attempts, "deadline": deadline,
                                   "backoff_seconds": backoff, "next_retry": next_retry,
                               })
                self._notify_lock_wait_observer(waiter_id, waiter.seq, kind, resource,
                                                task.id, task.goal_id, attempts, deadline,
                                                next_retry)
                # NEVER sleep inside a SQLite transaction: the store's acquire
                # already committed/rolled back before we got here.
                self._lock_sleep(backoff)

    def _cancel_waiters_for_task(self, task: Task) -> None:
        """Best-effort: cancel any queued FIFO waiters owned by a terminal
        task so a finished task can never block the queue (ADR-023)."""
        if self.mutation_lock_store is None or not hasattr(self.mutation_lock_store, "cancel_waiter_for_task"):
            return
        try:
            self.mutation_lock_store.cancel_waiter_for_task(task.id, "cancelled")
        except Exception:
            pass

    def _notify_lock_wait_observer(self, waiter_id: str, position: int, kind: str,
                                   resource: str, task_id: str, goal_id: str | None,
                                   attempts: int, deadline: str, next_retry: str) -> None:
        """Observability-only callback (ADR-023): lets a host process (e.g. a
        demo subprocess) observe that a task is waiting, with bounded queue
        metadata. Never affects coordination or authorization."""
        if self.lock_wait_observer is None:
            return
        try:
            self.lock_wait_observer({
                "waiter_id": waiter_id,
                "position": position,
                "resource_kind": kind,
                "resource": resource,
                "task_id": task_id,
                "goal_id": goal_id,
                "attempts": attempts,
                "deadline": deadline,
                "next_retry": next_retry,
            })
        except Exception:
            pass

    def _persist_lock_wait(self, task: Task, step: PlanStep, kind: str, resource: str,
                           deadline: str, attempts: int, next_retry: str,
                           waiter_id: str | None = None,
                           position: int | None = None) -> None:
        """Durably persist the waiting state (task row + goal blocker + a
        checkpoint) so a restart resumes with the SAME budget/deadline and
        FIFO queue position."""
        try:
            self.storage.save_task(task)
            self._checkpoint(task, reason="waiting for mutation lock")
        except Exception as exc:
            self._emit("error", task_id=task.id, step_id=_step_id(step), success=False,
                       detail={"error": f"persist lock wait failed: {exc}"})
        blocker = {
            "task_id": task.id,
            "step_index": step.index,
            "capability": step.capability,
            "action": step.action,
            "resource_kind": kind,
            "resource": resource,
            "deadline": deadline,
            "attempts": attempts,
            "next_retry": next_retry,
            "reason": "mutation resource is locked by another owner; waiting (bounded)",
        }
        if waiter_id:
            blocker["waiter_id"] = waiter_id
        if position is not None:
            blocker["position"] = position
        self._set_lock_blocker(task.goal_id or "", blocker)

    def _fail_lock_wait_timeout(self, task: Task, step: PlanStep, kind: str,
                                resource: str, deadline: str, attempts: int,
                                owner: str, waiter_id: str | None = None) -> None:
        """Durable, explainable timeout state: the step/task FAIL with a typed
        reason, the goal keeps an explainable lock_contention blocker (cleared
        when the lock is eventually released so the goal can replan), and NO
        recovery record is created (lock contention != mutation failure)."""
        step.status = StepStatus.FAILED
        step.error = (f"mutation lock wait timed out for {kind!r} {resource!r} "
                      f"(deadline {deadline}, attempts {attempts})")
        task.error = step.error
        task.lock_wait = None
        try:
            self.storage.save_task(task)
        except Exception:
            pass
        self._emit("mutation.lock.timeout", task_id=task.id, step_id=_step_id(step),
                   success=False, detail={
                       "resource_kind": kind, "resource": resource,
                       "capability": step.capability, "action": step.action,
                       "deadline": deadline, "attempts": attempts,
                   })
        blocker = {
            "task_id": task.id,
            "step_index": step.index,
            "capability": step.capability,
            "action": step.action,
            "resource_kind": kind,
            "resource": resource,
            "deadline": deadline,
            "attempts": attempts,
            "reason": "mutation lock wait timed out (deadline "
                      f"{deadline}, attempts {attempts})",
        }
        if waiter_id:
            blocker["waiter_id"] = waiter_id
        self._set_lock_blocker(task.goal_id or "", blocker)

    def _track_inflight_lock(self, resource_kind: str | None, resource: str | None,
                             acquired: bool) -> None:
        """Track which canonical resources THIS engine's running steps hold
        (ADR-024 dispatch). Coordination only - never authorization; the
        durable lock store remains the sole lock authority."""
        if not resource_kind or not resource:
            return
        with self._inflight_lock:
            key = (resource_kind, resource)
            if acquired:
                self._inflight_locks.add(key)
            else:
                self._inflight_locks.discard(key)

    def _inflight_lock_held(self, resource_kind: str | None, resource: str | None) -> bool:
        with self._inflight_lock:
            return (resource_kind, resource) in self._inflight_locks

    def _revalidate_before_mutation(
        self, task: Task, step: PlanStep, spec, waited: bool,
        goal_run_claim=None,
    ) -> bool:
        """Re-check LIVE authorization immediately before mutating (ADR-022).

        Only meaningful after the task actually WAITED for the lock: the
        approval/authorization window may have gone stale while waiting. We
        rebuild the authorization request from the CURRENT ActionSpec and
        policy, and re-run the approval seam (which compares the canonical
        fingerprint and forces a FRESH approval when anything security-
        relevant changed). Returns True when the step may proceed; False when
        the step was paused (fresh approval queued) or denied - the caller
        must NOT execute the capability."""
        if not self._goal_run_allows_task(
                task, goal_run_claim, "post-lock revalidation"):
            return False
        if self._fence_task_for_superseded_plan(task):
            return False
        if self._observe_goal_pause(task):
            if waited:
                # Lock acquisition cleared durable wait coordination in memory.
                # Persist that cleanup before stopping on PAUSED, otherwise a
                # restart can remain parked behind an already-acquired waiter.
                try:
                    self.storage.save_task(task)
                except TaskStateError:
                    canonical = self.storage.load_task(task.id)
                    if canonical is not None:
                        task.__dict__.update(canonical.__dict__)
            return False
        if not waited:
            return True  # no contention: worker/authz pause checks validated
        if (self._fail_task_for_terminal_goal(
                task, step, phase="post-lock-wait")
                or self._fence_task_on_open_recovery(task)
                or not self._task_step_is_current(task, step)):
            return False
        from arion.orchestration.authz import PolicyOutcome

        # LIVE spec, not the stale one captured before the wait: the
        # ActionSpec/policy may have changed while we waited.
        live_spec = self.registry.action_spec(step.capability, step.action) or spec
        request = self._build_authz_request(task, step, live_spec)
        decision = self.policy.decide(request)
        self._emit(
            "permission.checked",
            task_id=task.id,
            step_id=_step_id(step),
            detail=AuthorizationEventDetails.from_mapping(
                decision.to_dict(),
                actor=request.actor.id,
                param_keys=tuple(request.params),
                revalidated_after_lock_wait=True,
            ),
        )
        if decision.outcome == PolicyOutcome.DENY:
            step.status = StepStatus.FAILED
            step.error = present_resource_reason(
                decision.reason, request.resource_kind, request.resource
            )
            self._emit(
                "permission.denied",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail=AuthorizationEventDetails.from_mapping(
                    decision.to_dict()
                ),
            )
            return False
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            if not self._handle_approval(task, step, request, decision):
                return False  # paused (fresh approval queued) or denied
        return True

    def _build_authz_request(self, task: Task, step: PlanStep, spec) -> "AuthorizationRequest":
        """Build the authorization request from the LIVE ActionSpec (never
        from the plan's claims) - shared by the execution path and the
        post-wait revalidation."""
        return AuthorizationRequest(
            actor=self.actor,
            task_id=task.id,
            step_index=step.index,
            capability=step.capability,
            action=step.action,
            scope=spec.required_scope,
            params=dict(step.params),
            resource=self._extract_resource(spec, step.params),
            resource_kind=spec.resource_kind,
            risk=spec.risk,
            side_effects=spec.side_effects,
            idempotent=spec.idempotent,
            retry_safe=spec.retry_safe,
        )

    def _release_mutation_lock(self, lock, task: Task, step: PlanStep) -> None:
        """Release the advisory lock after the mutation window (idempotent).

        ADR-023: release + next-waiter selection is ATOMIC at the SQLite layer
        (release_and_select_next) - the next FIFO waiter is selected in the
        same transaction, so there is no check-then-act race on handoff.

        Never fails the task: a stuck release must not mask the mutation's
        own outcome, but it is audited loudly."""
        if lock is None or self.mutation_lock_store is None:
            return
        try:
            if hasattr(self.mutation_lock_store, "release_and_select_next"):
                self.mutation_lock_store.release_and_select_next(
                    lock.lock_id, lock.owner_id, now=self._lock_now())
            else:
                self.mutation_lock_store.release(lock.lock_id, lock.owner_id)
            self._emit("mutation.lock.released", task_id=task.id, step_id=_step_id(step), detail={
                "lock_id": lock.lock_id,
                "resource_kind": lock.resource_kind,
                "resource": lock.resource,
                "capability": lock.capability,
                "action": lock.action,
                "owner_id": lock.owner_id,
            })
            self._track_inflight_lock(lock.resource_kind, lock.resource, False)
        except Exception as exc:
            self._emit("error", task_id=task.id, step_id=_step_id(step), success=False,
                       detail={"error": f"lock release failed: {exc}"})

    def _set_lock_contention_blocker(self, task: Task, step: PlanStep, spec) -> None:
        """Durably block the goal while the mutation resource is locked by
        another owner (operational coordination, not authorization)."""
        kind, resource = self._lock_canonical(spec, step)
        self._set_lock_blocker(task.goal_id if task.goal_id else "", {
            "task_id": task.id,
            "step_index": step.index,
            "capability": step.capability,
            "action": step.action,
            "resource_kind": kind,
            "resource": resource,
            "reason": "mutation resource is locked by another owner",
        })

    def _set_lock_blocker(self, goal_id: str, fields: dict) -> None:
        """Add-or-UPDATE the goal's lock_contention blocker (ADR-022).

        The blocker is the durable, explainable surface of a lock-wait/timeout
        session; its metadata (deadline/attempts/next_retry/reason) must track
        the latest retry. ``GoalManager.set_blocked`` upserts by key
        (preserving ``added_at``) and CAS-retries so a concurrent lifecycle
        writer cannot be clobbered.
        """
        if not goal_id or self.goal_manager is None:
            return
        try:
            self.goal_manager.set_blocked(
                goal_id, {"type": "lock_contention", **fields},
                reason="lock_contention")
        except Exception:
            pass

    def reclaim_stale_locks(self, now: str | None = None) -> list[str]:
        """Explicitly reclaim all expired locks (leases elapsed). Returns the
        reclaimed lock ids; emits a bounded audit event per lock. Active locks
        are never touched. Deterministic with an injectable clock."""
        if self.mutation_lock_store is None:
            return []
        now = now or self._lock_now()
        ids = self.mutation_lock_store.reclaim_expired(now=now)
        for lock_id in ids:
            self._emit("mutation.lock.reclaimed", task_id=None, step_id=None, detail={
                "lock_id": lock_id, "reason": "stale lease expired (explicit reclaim)",
            })
        return ids

    def reclaim_lock(self, lock_id: str, now: str | None = None) -> dict:
        """Reclaim ONE expired lock (administrative, CLI). Fail closed: an
        unknown id or an ACTIVE lock raises a typed MutationLockError. The
        reclaim only removes the stale coordination record - it NEVER grants
        authorization to mutate."""
        from arion.state.locks import MutationLockError

        if self.mutation_lock_store is None:
            raise MutationLockError("mutation lock store is not available on this engine")
        now = now or self._lock_now()
        lock = self.mutation_lock_store.get(lock_id)
        if lock is None:
            raise MutationLockError(f"unknown lock id: {lock_id}")
        if lock.expires_at > now:
            raise MutationLockError(f"lock {lock_id} is still active (expires {lock.expires_at}); "
                                    "active locks cannot be reclaimed")
        reclaimed = self.mutation_lock_store.reclaim_expired(
            now=now, resource_kind=lock.resource_kind, resource=lock.resource)
        if lock_id not in reclaimed:
            raise MutationLockError(f"lock {lock_id} could not be reclaimed")
        self._emit("mutation.lock.reclaimed", task_id=None, step_id=None, detail={
            "lock_id": lock_id,
            "resource_kind": lock.resource_kind,
            "resource": lock.resource,
            "capability": lock.capability,
            "action": lock.action,
            "reason": "stale lease expired (administrative reclaim)",
        })
        d = lock.to_dict()
        d["status"] = "reclaimed"
        return d

    def _block_on_lock_contention(self, goal_id: str, gm) -> bool:
        """Gate the goal before planning while a lock_contention blocker's
        resource is still actively locked by another owner. Clears the blocker
        (via the goal manager's recheck hook) once the lock is gone."""
        goal = gm.get_goal(goal_id)
        if goal is None:
            return False
        for b in (goal.blockers or []):
            if (b.get("key") or b.get("type")) == "lock_contention":
                if self._lock_is_active(b.get("resource_kind"), b.get("resource")):
                    try:
                        gm.set_blocked(goal_id, {
                            "type": "lock_contention",
                            "reason": "mutation resource is still locked by another owner",
                            "resource_kind": b.get("resource_kind"),
                            "resource": b.get("resource"),
                        }, reason="lock_contention")
                    except Exception:
                        pass
                    return True
                try:
                    gm.clear_blocker(goal_id, "lock_contention", reason="lock_contention_cleared")
                except Exception:
                    pass
                return False
        return False

    def shutdown(self, timeout: float = 30.0) -> HealthReport:
        """Stop work and release resources owned by this composition.

        Scheduler ordering from ADR-024..026 is preserved: no new work is
        accepted, active workers are drained, durable ownership is removed,
        and only then are bootstrap-owned cognition, memory, and state stores
        closed in reverse construction order (ADR-032).  Dependencies injected
        directly into :class:`ArionEngine` are borrowed and remain open unless
        their creator explicitly supplied a ``ResourceLifecycle``.

        The operation is idempotent and concurrent callers receive the same
        terminal health state.  After it returns no worker may continue
        mutating an owned store.
        """
        with self._shutdown_lock:
            if self._shutdown_complete:
                return self.health()
            if getattr(self, "scheduler", None) is not None:
                self.scheduler.shutdown(timeout=timeout)
            if getattr(self, "scheduler_registry", None) is not None:
                try:
                    for row in self.scheduler_registry.list_work(
                            status=SchedulerWorkStatus.QUEUED,
                            scheduler_id=self.scheduler_id):
                        try:
                            self.scheduler_registry.mark_terminal(
                                row.work_id, SchedulerWorkStatus.CANCELLED,
                                now=self._lock_now())
                        except SchedulerStateError:
                            pass  # already transitioned by a draining worker
                except Exception:
                    pass
                if self._registered and hasattr(
                        self.scheduler_registry, "unregister_scheduler"):
                    try:
                        self.scheduler_registry.unregister_scheduler(self.scheduler_id)
                        self._registered = False
                    except Exception:
                        pass
            self._resource_lifecycle.shutdown(timeout=timeout)
            self._shutdown_complete = True
            return self.health()

    def close(self) -> HealthReport:
        """Compatibility alias for complete runtime shutdown (ADR-032)."""
        return self.shutdown()

    def __enter__(self) -> "ArionEngine":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.shutdown()

    def health(self) -> HealthReport:
        """Return bounded scheduler and owned-resource lifecycle health."""
        with self._shutdown_lock:
            owned = self._resource_lifecycle.health()
            scheduler_status = (
                HealthStatus.STOPPED if self._shutdown_complete
                else HealthStatus.HEALTHY
            )
            scheduler_detail = "scheduler active"
            try:
                snapshot = self.scheduler.snapshot()
                queued = len(snapshot.get("queued", ()))
                running = len(snapshot.get("running", ()))
                workers = int(snapshot.get("workers", 0))
                if snapshot.get("shutdown"):
                    scheduler_status = HealthStatus.STOPPED
                    scheduler_detail = "scheduler stopped"
                else:
                    scheduler_detail = (
                        f"workers={workers}, queued={queued}, running={running}"
                    )
            except Exception as exc:
                scheduler_status = HealthStatus.UNHEALTHY
                scheduler_detail = f"health check failed: {type(exc).__name__}"

            scheduler_health = ComponentHealth(
                name="orchestration.scheduler",
                status=scheduler_status,
                detail=scheduler_detail,
            )
            components = (scheduler_health, *owned.components)

            if (owned.state is LifecycleState.FAILED
                    or scheduler_status is HealthStatus.UNHEALTHY):
                state = LifecycleState.FAILED
                status = HealthStatus.UNHEALTHY
            elif self._shutdown_complete:
                state = LifecycleState.STOPPED
                status = HealthStatus.STOPPED
            elif owned.status is not HealthStatus.HEALTHY:
                state = LifecycleState.RUNNING
                status = HealthStatus.DEGRADED
            else:
                state = LifecycleState.RUNNING
                status = HealthStatus.HEALTHY
            return HealthReport(state=state, status=status, components=components)

    # ---------- approval expiry (ADR-019) ----------

    def expire_stale_approvals(self, now: str | None = None) -> list[str]:
        """Conditionally expire stale PENDING rows with their awaiting tasks."""
        now = now or utcnow()
        if self.approval_store is None or self.approval_ttl_seconds is None:
            return []
        expired: list[str] = []
        for req in self.approval_store.list_requests(
                status=ApprovalStatus.PENDING.value):
            if not self._approval_request_is_expired(req, now=now):
                continue
            try:
                committed = self._apply_approval_status(
                    req, ApprovalStatus.EXPIRED, "system:expiry",
                    "approval expired; recovery requires new authorization",
                    decision_time=now,
                )
            except ApprovalError:
                # Task already terminal/missing: close the orphan request by
                # CAS without reviving or rewriting task state.
                req.status = ApprovalStatus.EXPIRED
                req.decision_actor = "system:expiry"
                req.decided_at = now
                req.expired_at = now
                transition = getattr(self.approval_store,
                                     "transition_request", None)
                committed = bool(
                    callable(transition)
                    and transition(req, ApprovalStatus.PENDING)
                )
            if committed:
                expired.append(req.approval_id)
        return expired

    # ---------- public API ----------

    def submit_goal(self, description: str, source: str = "cli") -> Goal:
        """Create a lifecycle goal (ADR-016) via the GoalManager when wired;
        falls back to a plain Goal for backward compatibility."""
        if self.goal_manager is not None:
            goal = self.goal_manager.create_goal(description, source)
        else:
            goal = Goal(id=new_id("goal"), description=description, source=source)
            self.storage.save_goal(goal)
        self._emit("goal.submitted", task_id=None, detail={"goal_id": goal.id, "description": description})
        return goal

    def create_task(self, goal: Goal, plan_version: int | None = None) -> Task:
        task = Task(id=new_id("task"), goal_id=goal.id, description=goal.description,
                    plan_version=plan_version)
        self.storage.save_task(task)
        self._emit("task.created", task_id=task.id, detail={"goal_id": goal.id})
        return task

    def execute_goal(self, description: str, source: str = "cli") -> Task:
        """Full pipeline: submit goal, create task, plan, execute with checkpointing."""
        goal = self.submit_goal(description, source)
        task = self.create_task(goal)
        self.run_task(task.id)
        task = self.storage.load_task(task.id)
        assert task is not None
        return task

    def run_goal(self, goal_id: str, max_replans: int = 5) -> Goal:
        """Run one goal only while holding its durable run lease."""
        gm = self.goal_manager
        if gm is None:
            raise ValueError("goal manager not wired; use execute_goal instead")
        claim = self._acquire_goal_run_lease(goal_id)
        if claim is None:
            goal = gm.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            return goal
        try:
            return self._run_goal_owned(
                goal_id, max_replans=max_replans, goal_run_claim=claim
            )
        finally:
            self._release_goal_run_lease(goal_id, claim)

    def _run_goal_owned(
        self,
        goal_id: str,
        max_replans: int = 5,
        goal_run_claim=None,
    ) -> Goal:
        """Long-horizon goal loop (ADR-016/017), with ownership preclaimed:

          Goal -> Goal State -> Strategy -> Plan -> Execute -> Observe ->
          Learn -> Replan

        Semantics per call:
        - evaluates the goal; completes it when all plan steps are handled;
        - continues through SUCCESSFUL planning+execution cycles;
        - returns as soon as a task FAILS (goal stays ACTIVE with the failure
          persisted) so the caller can seed/decide before the next advance -
          each call is one long-horizon cycle;
        - a task reaching AWAITING_APPROVAL stops the loop CLEANLY (never
          spins, never re-executes the awaiting task): the goal becomes
          durably BLOCKED with an approval_pending blocker and is returned;
        - a BLOCKED goal (missing capability / approval) is returned without
          planning; blockers are re-checked against the CURRENT world state so
          a newly available capability or resolved approval unblocks it;
        - a goal whose planner-required capability is not registered becomes
          durably BLOCKED (missing_capability) instead of repeatedly
          replanning;
        - replanning produces a NEW (immutable) plan version (never mutates
          the previous plan), bounded across calls by max_replans;
        - replay-safe: pending tasks for the latest plan version are resumed,
          never duplicated.

        Returns the Goal state after this cycle.
        """
        gm = self.goal_manager
        if gm is None:
            raise ValueError("goal manager not wired; use execute_goal instead")
        while True:
            if not self._goal_run_lease_current(
                    goal_id, goal_run_claim, "goal evaluation"):
                return gm.get_goal(goal_id)
            # ADR-053: converge superseded coordination state (stale queued
            # approvals, parked mutation-lock waiters) BEFORE evaluating, so
            # current progress derives only from latest-plan authority.
            self._reconcile_superseded_coordination(
                goal_id, goal_run_claim)
            result, _goal = gm.evaluate(goal_id)
            if not self._goal_run_lease_current(
                    goal_id, goal_run_claim, "post-evaluation"):
                return gm.get_goal(goal_id)
            action = result.next_action
            if action in ("none", "paused"):
                return gm.get_goal(goal_id)
            if action == "await_approval":
                # approval-pending: stop cleanly; never spin on the awaiting
                # task. The goal is durably BLOCKED (approval_pending blocker).
                return gm.get_goal(goal_id)
            if action == "await_lock":
                # waiting for a mutation lock (ADR-022): the task has durable
                # wait metadata + a lock_contention blocker. While the
                # resource is still locked, stop cleanly (no spin, no sleep)
                # and return the goal; the blocker clears when the live lock
                # store shows the resource free, and only THEN does the
                # waiting task resume with its preserved deadline/retry
                # budget (the wait loop acquires - or times out durably -
                # under the original deadline).
                if gm.recheck_blockers(goal_id):
                    pending = gm.pending_task(goal_id)
                    if pending is not None:
                        pending = self._run_task_owned(
                            pending.id, goal_run_claim=goal_run_claim
                        )
                        if pending.status in (
                                TaskStatus.FAILED,
                                TaskStatus.AWAITING_APPROVAL,
                                TaskStatus.RUNNING,
                        ):
                            return gm.get_goal(goal_id)
                        continue
                    continue
                return gm.get_goal(goal_id)
            if action == "resolve_blocker":
                # durably BLOCKED: re-check blockers against the CURRENT world
                # state (capability appeared / approval resolved); if nothing
                # changed, return without planning (no replan loop).
                if gm.recheck_blockers(goal_id):
                    continue
                return gm.get_goal(goal_id)
            if action == "complete":
                if not self._goal_run_lease_current(
                        goal_id, goal_run_claim, "goal completion"):
                    return gm.get_goal(goal_id)
                try:
                    gm.complete_goal(
                        goal_id,
                        reason="all_work_complete",
                        expect_plan_version=(
                            result.evidence.get("latest_plan_version")
                            if isinstance(result.evidence, dict) else None
                        ),
                    )
                except GoalPlanLineageError:
                    # ADR-054: the completion decision's plan authority is
                    # stale - a newer immutable plan became latest inside the
                    # evaluate -> transition window. Fail closed: no
                    # completion, no failure, no new authority; re-evaluate
                    # against current durable state.
                    self._emit("goal.completion.fenced", task_id=None, detail={
                        "goal_id": goal_id,
                        "expected_plan_version": (
                            result.evidence.get("latest_plan_version")
                            if isinstance(result.evidence, dict) else None
                        ),
                        "reason": "plan lineage advanced before completion",
                    })
                    continue
                return gm.get_goal(goal_id)

            if action == "replan":
                if result.evidence.get("reason") == "capability_available":
                    # unblock via recheck (emits capability.available + goal
                    # state change); fall back to a blanket clear if needed
                    if gm.recheck_blockers(goal_id):
                        continue
                    gm.clear_blockers(goal_id, reason="capability_available")
                # bounded across calls (prevents runaway caller loops)
                replan_count = sum(
                    1 for p in gm.plan_history(goal_id)
                    if str(p.get("reason", "")).startswith("replan")
                )
                if replan_count >= max_replans:
                    if not self._goal_run_lease_current(
                            goal_id, goal_run_claim, "goal failure"):
                        return gm.get_goal(goal_id)
                    try:
                        gm.fail_goal(
                            goal_id,
                            reason="max_replans_exceeded",
                            expect_plan_version=(
                                result.evidence.get("latest_plan_version")
                                if isinstance(result.evidence, dict) else None
                            ),
                        )
                    except GoalPlanLineageError:
                        # ADR-055: the failure decision's plan authority is
                        # stale - a newer immutable plan became latest inside
                        # the evaluate -> transition window. Fail closed: no
                        # failure, no new authority; re-evaluate so the fresh
                        # replan count decides against the current lineage.
                        self._emit("goal.failure.fenced", task_id=None, detail={
                            "goal_id": goal_id,
                            "expected_plan_version": (
                                result.evidence.get("latest_plan_version")
                                if isinstance(result.evidence, dict) else None
                            ),
                            "reason": "plan lineage advanced before failure",
                        })
                        continue
                    return gm.get_goal(goal_id)
                if self._block_on_missing_capability(goal_id, gm):
                    return gm.get_goal(goal_id)
                if self._block_on_open_recovery(goal_id, gm):
                    return gm.get_goal(goal_id)
                if self._block_on_lock_contention(goal_id, gm):
                    return gm.get_goal(goal_id)
                task = self._plan_for_goal(
                    goal_id,
                    replan_reason=result.evidence.get("reason"),
                    goal_run_claim=goal_run_claim,
                )
                if task is not None:
                    task = self._run_task_owned(
                        task.id, goal_run_claim=goal_run_claim
                    )
                    if task.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL,
                           TaskStatus.RUNNING):  # RUNNING = clean stop
                           # (cross-process capacity exhausted)
                        return gm.get_goal(goal_id)  # caller decides next step
                continue

            # continue / initial_plan: resume a pending task for the latest
            # plan version if one exists (replay safety), else plan + execute.
            pending = gm.pending_task(goal_id)
            if pending is not None:
                pending = self._run_task_owned(
                    pending.id, goal_run_claim=goal_run_claim
                )
                if pending.status in (
                        TaskStatus.FAILED,
                        TaskStatus.AWAITING_APPROVAL,
                        TaskStatus.RUNNING,
                ):
                    return gm.get_goal(goal_id)
                continue
            if self._block_on_missing_capability(goal_id, gm):
                return gm.get_goal(goal_id)
            if self._block_on_open_recovery(goal_id, gm):
                return gm.get_goal(goal_id)
            if self._block_on_lock_contention(goal_id, gm):
                return gm.get_goal(goal_id)
            task = self._plan_for_goal(
                goal_id, replan_reason=None, goal_run_claim=goal_run_claim
            )
            if task is not None:
                task = self._run_task_owned(
                    task.id, goal_run_claim=goal_run_claim
                )
                if task.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL,
                           TaskStatus.RUNNING):  # RUNNING = clean stop
                           # (cross-process capacity exhausted)
                    return gm.get_goal(goal_id)

    def _block_on_open_recovery(self, goal_id: str, gm) -> bool:
        """Gate the goal before planning while a mutation recovery is open (ADR-020).

        A failed non-retry-safe mutation leaves a durable REQUIRED recovery
        record; while ANY such record exists for the goal, fresh planning is
        durably blocked (recovery_required blocker) until an operator
        explicitly acknowledges the recovery. Recovery is a GATE, never an
        authorization - a fresh plan still needs its own approval.
        """
        # A prior recovery-table outage may have left only the terminal task
        # repair marker. Recreate the REQUIRED authority before deciding that
        # the goal is clear to replan.
        try:
            self._reconcile_missing_recovery_records(goal_id)
        except Exception:
            return True  # persistence could not be verified; fail closed
        if not self._has_open_recovery(goal_id):
            return False
        goal = gm.get_goal(goal_id)
        if goal is None:
            return False
        try:
            gm.set_blocked(goal_id, {
                "type": "recovery_required",
                "reason": "mutation recovery required (non-retry-safe failure)",
            }, reason="recovery_required")
        except Exception:
            pass
        return True

    def _block_on_missing_capability(self, goal_id: str, gm) -> bool:
        """Gate the goal before planning (ADR-017/018).

        - If the planner cannot declare its required capabilities (contract
          missing), FAIL CLOSED: the goal is durably BLOCKED
          (planner_contract) - never planned, never executed.
        - If the planner requires a capability absent from the live registry,
          durably BLOCK the goal (missing_capability) instead of
          planning/failing in a loop.

        Returns True when the goal was blocked."""
        goal = gm.get_goal(goal_id)
        if goal is None:
            return False
        requirements = self._planner_requirements(goal.description)
        if requirements is None:
            gm.set_blocked(goal_id, {
                "type": "planner_contract",
                "detail": "planner cannot declare required capabilities; refusing to plan (fail closed)",
            }, reason="planner_contract_fail_closed")
            self._emit("error", task_id=None, success=False, detail={
                "goal_id": goal_id,
                "error": "planner does not implement the required_capabilities contract",
                "error_type": "PlannerContractError",
                "category": "planning",
            })
            return True
        missing = [c for c in requirements if not self.registry.has(c)]
        if not missing:
            return False
        gm.set_blocked(goal_id, {
            "type": "missing_capability",
            "capabilities": sorted(missing),
            "strategy": "blocked_missing_capability",
            "detail": f"goal needs capabilities {sorted(missing)} not registered",
        }, reason="blocked_missing_capability")
        self._emit("capability.unavailable", task_id=None, detail={
            "goal_id": goal_id, "capabilities": sorted(missing), "reason": "not_registered",
        })
        return True

    def _planner_requirements(self, goal_description: str) -> list[str] | None:
        """Required capabilities per the planner contract.

        Returns a list of required capability names, or None when the planner
        does NOT implement the contract (fail closed: do not bypass the gate).
        """
        planner = getattr(self, "planner", None)
        required = getattr(planner, "required_capabilities", None)
        if required is None:
            return None
        try:
            need = required(goal_description)
        except Exception:
            return None  # a planner that errors on requirements also fails closed
        if not isinstance(need, (set, list, tuple)):
            return None
        return sorted(str(c) for c in need)

    def resolve_approval(self, task_id: str, outcome: "ApprovalOutcome", actor: str = "approver") -> Task:
        """Backward-compatible seam: resolve the durable approval request for
        an awaiting task's active step (ADR-017/018). Delegates to the queue.

        APPROVED: the task becomes resumable (RUNNING) and the goal's
        approval_pending blocker is cleared; the next run resumes the EXACT
        pending step - no re-planning, no re-request of the same approval
        (the live metadata is re-verified at resume time).

        DENIED: the step + task fail durably with reason 'approval denied'
        (goal unblocked; a later run_goal replans around it).

        Returns the updated Task. Fail-closed on wrong states.
        """
        if outcome not in (ApprovalOutcome.APPROVED, ApprovalOutcome.DENIED):
            raise ValueError(f"resolve_approval accepts APPROVED or DENIED, got {outcome!r}")
        task = self.storage.load_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise GoalStateError(f"task {task_id} is not awaiting approval (status={task.status.value})")
        step = task.active_step
        if step is None:
            raise GoalStateError(f"task {task_id} has no active step to resolve")
        if self.approval_store is not None:
            req = self._pending_request_for_step(task.id, step.index)
            if req is None:
                raise GoalStateError(f"task {task_id} has no pending approval for step {step.index}")
            self.resolve_approval_request(req.approval_id, outcome, actor)
            return self.storage.load_task(task_id)
        # No durable queue (legacy wiring): fall back to the in-memory mirror.
        recs = [r for r in task.approvals
                if r.get("step_index") == step.index and r.get("outcome") == "pending"]
        if not recs:
            raise GoalStateError(f"task {task_id} has no pending approval for step {step.index}")
        return self._resolve_legacy(task, recs[-1], outcome, actor)

    def _approval_request_is_expired(
        self, req: "ApprovalRequest", now: str | None = None,
    ) -> bool:
        ttl = self.approval_ttl_seconds
        if ttl is None:
            return False
        from datetime import datetime, timedelta, timezone

        now_value = now or utcnow()
        try:
            current = datetime.fromisoformat(now_value)
            created = datetime.fromisoformat(req.created_at)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return created < current - timedelta(seconds=max(0.0, float(ttl)))

    def _terminal_goal_status(self, task: Task) -> GoalStatus | None:
        if self.goal_manager is None or not task.goal_id:
            return None
        try:
            goal = self.goal_manager.get_goal(task.goal_id)
            if goal is not None and goal.status in (
                    GoalStatus.COMPLETED, GoalStatus.FAILED,
                    GoalStatus.CANCELLED):
                return goal.status
            return None
        except Exception:
            # A configured goal authority that cannot be read fails closed.
            return GoalStatus.FAILED

    def _goal_is_terminal_for_approval(self, task: Task) -> bool:
        return self._terminal_goal_status(task) is not None

    @staticmethod
    def _step_execution_definition(step: PlanStep | dict[str, Any]) -> dict[str, Any]:
        candidate = step if isinstance(step, PlanStep) else PlanStep.from_dict(step)
        return {
            "index": candidate.index,
            "intent": candidate.intent,
            "capability": candidate.capability,
            "action": candidate.action,
            "scope": candidate.scope,
            "params": candidate.params,
            "verification": {
                "policy": candidate.verification.policy,
                "args": candidate.verification.args,
            },
            "depends_on": list(candidate.depends_on),
            "guidance": list(candidate.guidance),
            "skipped_reason": candidate.skipped_reason,
            "max_attempts": candidate.max_attempts,
            "planned_status": (
                StepStatus.SKIPPED.value
                if candidate.status == StepStatus.SKIPPED
                else StepStatus.PENDING.value
            ),
        }

    def _task_matches_latest_plan(self, task: Task, latest: dict[str, Any]) -> bool:
        try:
            summary = latest.get("plan_summary")
            if not isinstance(summary, list) or not summary:
                return False
            stored = sorted(
                (self._step_execution_definition(item) for item in summary),
                key=lambda item: item["index"],
            )
            executable = sorted(
                (self._step_execution_definition(step) for step in task.steps),
                key=lambda item: item["index"],
            )
            return stored == executable
        except Exception:
            return False

    def _task_implements_latest_plan(self, task: Task) -> bool:
        """Whether this is the canonical task matching the current plan."""
        if self.goal_manager is None or not task.goal_id:
            return True
        try:
            latest = self.goal_manager.latest_plan(task.goal_id)
            if latest is None:
                return True
            latest_version = latest.get("plan_version")
            goal_tasks = [
                candidate for candidate in self.storage.list_tasks()
                if candidate.goal_id == task.goal_id
            ]
            exact = sorted(
                (candidate for candidate in goal_tasks
                 if candidate.plan_version == latest_version),
                key=lambda candidate: (candidate.created_at, candidate.id),
            )
            if task.plan_version == latest_version:
                return bool(exact and exact[0].id == task.id)
            if task.plan_version is not None:
                return False
            if exact:
                return False
            legacy = sorted(
                (candidate for candidate in goal_tasks
                 if candidate.plan_version is None),
                key=lambda candidate: (candidate.created_at, candidate.id),
            )
            if not legacy or legacy[0].id != task.id:
                return False
            # Legacy unversioned tasks are fallback only when they are not
            # older than the latest plan row and reproduce its definition.
            latest_created = latest.get("created_at")
            return bool(
                not latest_created or task.created_at >= latest_created
            )
        except Exception:
            return False

    def _fence_task_for_superseded_plan(self, task: Task) -> bool:
        """Terminalize non-current task work without executing a capability."""
        if (task.status in TASK_TERMINAL_STATUSES
                or self._task_implements_latest_plan(task)):
            return False
        reason = "task superseded by a newer goal plan; execution denied"
        step = task.active_step
        if (task.status == TaskStatus.AWAITING_APPROVAL
                and step is not None and self.approval_store is not None):
            pending = self._pending_request_for_step(task.id, step.index)
            if pending is not None:
                self._apply_approval_status(
                    pending,
                    ApprovalStatus.DENIED,
                    "system:superseded_plan",
                    reason,
                )
                canonical = self.storage.load_task(task.id)
                if canonical is not None:
                    task.__dict__.update(canonical.__dict__)
                return True
        if step is not None and step.status in (
                StepStatus.PENDING, StepStatus.RUNNING):
            step.status = StepStatus.FAILED
            step.error = reason
        task.status = TaskStatus.FAILED
        task.error = reason
        task.lock_wait = None
        task.completed_at = utcnow()
        try:
            self.storage.save_task(task)
        except TaskStateError:
            canonical = self.storage.load_task(task.id)
            if canonical is not None:
                task.__dict__.update(canonical.__dict__)
        self._cancel_waiters_for_task(task)
        self._emit("task.failed", task_id=task.id, detail={
            "step_index": step.index if step is not None else None,
            "error": reason,
            "superseded_plan": True,
        })
        self._record_memory(task)
        return True

    def _reconcile_superseded_coordination(self, goal_id: str,
                                           goal_run_claim=None) -> None:
        """ADR-053: superseded coordination loses current authority.

        When plan lineage advances, historical tasks keep their rows, but a
        superseded or noncanonical task that still carries COORDINATION state
        - an AWAITING_APPROVAL step (a queued approval plus the goal's
        ``approval_pending`` blocker) or durable mutation-lock wait metadata -
        must not block, park, or redirect latest-plan work. The live goal-run
        owner therefore fences such tasks through the EXISTING ADR-049
        superseded-task fence: the pending approval is denied with actor
        ``system:superseded_plan``, the task is terminally FAILED, lock-wait
        metadata is cleared, and queued waiters are cancelled. No capability
        runs and no historical row is deleted. Tasks without coordination
        state (e.g. PLANNED history) keep relying on the existing fences at
        their execution boundaries.
        """
        gm = self.goal_manager
        if gm is None or not goal_id:
            return
        try:
            goal = gm.get_goal(goal_id)
        except Exception:
            return
        if goal is None or goal.status in (
                GoalStatus.COMPLETED, GoalStatus.FAILED,
                GoalStatus.CANCELLED, GoalStatus.PAUSED):
            return
        try:
            latest = gm.latest_plan(goal_id)
        except Exception:
            return
        if latest is None:
            return  # no immutable plan lineage: nothing is superseded by it
        if not self._goal_run_lease_current(
                goal_id, goal_run_claim,
                "superseded coordination reconcile"):
            return
        try:
            candidates = [
                task for task in self.storage.list_tasks()
                if task.goal_id == goal_id
                and task.status not in TASK_TERMINAL_STATUSES
                and (task.status == TaskStatus.AWAITING_APPROVAL
                     or task.lock_wait is not None)
            ]
        except Exception:
            return
        for task in candidates:
            # The fence itself decides authority: terminal rows and tasks
            # implementing the latest plan (including the canonical exact
            # task) are left untouched. A transient failure here is safe -
            # the next owned cycle retries (the reconcile is idempotent).
            try:
                self._fence_task_for_superseded_plan(task)
            except Exception:
                continue

    def _goal_is_paused(self, task: Task) -> bool:
        """Return current PAUSED authority; read failures stop work safely."""
        if self.goal_manager is None or not task.goal_id:
            return False
        try:
            goal = self.goal_manager.get_goal(task.goal_id)
            return goal is not None and goal.status == GoalStatus.PAUSED
        except Exception:
            return True

    def _observe_goal_pause(self, task: Task) -> bool:
        if not self._goal_is_paused(task):
            return False
        # Coordination-only marker shared by this in-process execution round;
        # Task.to_dict deliberately ignores dynamic attributes.
        setattr(task, "_goal_pause_observed", True)
        return True

    @staticmethod
    def _consume_goal_pause(task: Task) -> bool:
        observed = bool(getattr(task, "_goal_pause_observed", False))
        if observed:
            try:
                delattr(task, "_goal_pause_observed")
            except AttributeError:
                pass
        return observed

    def _fail_task_for_terminal_goal(
        self,
        task: Task,
        step: PlanStep | None = None,
        *,
        phase: str,
    ) -> bool:
        """Observe terminal goal authority at a planning/execution boundary."""
        goal_status = self._terminal_goal_status(task)
        if goal_status is None or task.status in TASK_TERMINAL_STATUSES:
            return False
        reason = (
            f"task stopped during {phase}: terminal goal "
            f"({goal_status.value})"
        )
        if step is None:
            step = task.active_step
        if step is not None and step.status in (
                StepStatus.PENDING, StepStatus.RUNNING):
            step.status = StepStatus.FAILED
            step.error = reason
        task.status = TaskStatus.FAILED
        task.error = reason
        task.completed_at = utcnow()
        try:
            self.storage.save_task(task)
        except TaskStateError:
            canonical = self.storage.load_task(task.id)
            if canonical is not None:
                task.__dict__.update(canonical.__dict__)
        self._cancel_waiters_for_task(task)
        self._emit("task.failed", task_id=task.id, detail={
            "step_index": step.index if step is not None else None,
            "error": reason,
            "terminal_goal": goal_status.value,
            "phase": phase,
        })
        return True

    def resolve_approval_request(self, approval_id: str, outcome: "ApprovalOutcome",
                                 actor: str = "approver") -> "ApprovalRequest":
        """Conditionally and atomically resolve one durable approval (ADR-038)."""
        if outcome not in (ApprovalOutcome.APPROVED, ApprovalOutcome.DENIED):
            raise ValueError(
                f"resolve_approval_request accepts APPROVED or DENIED, got {outcome!r}"
            )
        if self.approval_store is None:
            raise ApprovalError("approval queue is not available on this engine")
        req = self.approval_store.get_request(approval_id)
        if req is None:
            raise ApprovalError(f"unknown approval id: {approval_id}")
        candidate_task = self.storage.load_task(req.task_id)
        if (candidate_task is not None
                and self._fence_task_for_superseded_plan(candidate_task)):
            raise ApprovalError(
                f"approval {approval_id} cannot revive a superseded plan task"
            )
        target = (
            ApprovalStatus.APPROVED
            if outcome == ApprovalOutcome.APPROVED else ApprovalStatus.DENIED
        )
        if req.status != ApprovalStatus.PENDING:
            if req.status == target:
                self._reconcile_decided_request(req)
                return self.approval_store.get_request(approval_id)
            raise ApprovalError(
                f"approval {approval_id} decision conflicts with committed "
                f"state {req.status.value}"
            )
        if self._approval_request_is_expired(req):
            self._apply_approval_status(
                req, ApprovalStatus.EXPIRED, "system:expiry",
                "approval expired; recovery requires new authorization",
            )
            raise ApprovalError(f"approval {approval_id} expired before decision")

        task = self.storage.load_task(req.task_id)
        if task is None:
            req.status = ApprovalStatus.DENIED
            req.decision_actor = "system:missing_task"
            req.decided_at = utcnow()
            transition = getattr(self.approval_store, "transition_request", None)
            if callable(transition):
                transition(req, ApprovalStatus.PENDING)
            raise ApprovalError(
                f"approval {approval_id} references a missing task {req.task_id}"
            )
        if self._goal_is_terminal_for_approval(task):
            self._apply_approval_status(
                req, ApprovalStatus.DENIED, "system:terminal_goal",
                "approval rejected: terminal goal",
            )
            raise ApprovalError(
                f"approval {approval_id} cannot revive a terminal goal"
            )
        step = task.active_step
        if (task.status != TaskStatus.AWAITING_APPROVAL or step is None
                or step.index != req.step_index):
            req.status = ApprovalStatus.DENIED
            req.decision_actor = "system:terminal_task"
            req.decided_at = utcnow()
            transition = getattr(self.approval_store, "transition_request", None)
            if callable(transition):
                transition(req, ApprovalStatus.PENDING)
            raise ApprovalError(
                f"approval {approval_id} no longer matches the task's pending "
                f"step (task={req.task_id} step={req.step_index})"
            )
        self._resolve_request(req, outcome, actor)
        return self.approval_store.get_request(approval_id)

    def _resolve_request(self, req: "ApprovalRequest", outcome: "ApprovalOutcome",
                         actor: str) -> None:
        target = (
            ApprovalStatus.APPROVED
            if outcome == ApprovalOutcome.APPROVED else ApprovalStatus.DENIED
        )
        reason = "approved" if target == ApprovalStatus.APPROVED else "approval denied"
        self._apply_approval_status(req, target, actor, reason)

    def _approval_mirror(self, task: Task, step: PlanStep,
                         req: "ApprovalRequest", actor: str) -> dict:
        mirror = [
            record for record in (task.approvals or [])
            if record.get("approval_id") == req.approval_id
        ]
        record = mirror[-1] if mirror else self._mirror_from_request(task, step, req)
        record["outcome"] = req.status.value
        record["resolved_by"] = actor
        record["resolved_at"] = req.decided_at
        return record

    def _apply_approval_status(
        self,
        req: "ApprovalRequest",
        target: ApprovalStatus,
        actor: str,
        reason: str,
        decision_time: str | None = None,
    ) -> bool:
        """Commit request + task transition before events/blocker cleanup."""
        task = self.storage.load_task(req.task_id)
        if task is None:
            raise ApprovalError(
                f"approval {req.approval_id} references missing task {req.task_id}"
            )
        step = task.active_step
        if (task.status != TaskStatus.AWAITING_APPROVAL or step is None
                or step.index != req.step_index):
            raise ApprovalError(
                f"approval {req.approval_id} task is not awaiting its step"
            )
        expected_task_updated_at = task.updated_at
        req.status = target
        req.decision_actor = actor
        req.decided_at = decision_time or utcnow()
        if target == ApprovalStatus.EXPIRED:
            req.expired_at = req.decided_at
        self._approval_mirror(task, step, req, actor)
        if target == ApprovalStatus.APPROVED:
            task.status = TaskStatus.RUNNING
        else:
            step.status = StepStatus.FAILED
            step.error = reason
            task.status = TaskStatus.FAILED
            task.error = reason
            task.completed_at = utcnow()

        commit = getattr(self.approval_store, "commit_approval_decision", None)
        if not callable(commit):
            raise ApprovalError(
                "approval store lacks atomic decision support (fail closed)"
            )
        committed = commit(req, task, expected_task_updated_at)
        if not committed:
            actual = self.approval_store.get_request(req.approval_id)
            if actual is not None and actual.status == target:
                self._reconcile_decided_request(actual)
                return False
            state = actual.status.value if actual is not None else "missing"
            raise ApprovalError(
                f"approval {req.approval_id} decision conflicts with committed "
                f"state {state}"
            )

        self._after_approval_commit(req, task, step, target, actor, reason)
        return True

    def _after_approval_commit(
        self, req: "ApprovalRequest", task: Task, step: PlanStep,
        target: ApprovalStatus, actor: str, reason: str,
    ) -> None:
        gm = self.goal_manager
        if gm is not None and task.goal_id:
            try:
                gm.clear_blocker(
                    task.goal_id, "approval_pending",
                    reason=("approval_granted" if target == ApprovalStatus.APPROVED
                            else "approval_denied"),
                )
            except Exception:
                pass
        if target == ApprovalStatus.APPROVED:
            self._emit("approval.granted", task_id=task.id,
                       step_id=_step_id(step), detail={
                           "scope": req.scope, "resource": req.resource,
                           "approval_id": req.approval_id,
                       })
            self._emit("goal.approval.granted", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id,
                "step_index": step.index, "capability": req.capability,
                "action": req.action, "scope": req.scope,
                "actor": actor, "approval_id": req.approval_id,
            })
            return
        kind = "approval.expired" if target == ApprovalStatus.EXPIRED else "approval.denied"
        self._emit(kind, task_id=task.id, step_id=_step_id(step),
                   success=False, detail={
                       "scope": req.scope, "resource": req.resource,
                       "reason": reason, "approval_id": req.approval_id,
                   })
        if target == ApprovalStatus.EXPIRED:
            self._emit("goal.approval.expired", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id,
                "step_index": step.index, "approval_id": req.approval_id,
            })
        else:
            self._emit("goal.approval.denied", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id,
                "step_index": step.index, "actor": actor,
                "reason": reason, "approval_id": req.approval_id,
            })
        self._emit("task.failed", task_id=task.id, detail={
            "step_index": step.index, "error": reason,
        })
        self._record_memory(task)

    def _reconcile_decided_request(self, req: "ApprovalRequest") -> None:
        """Repair pre-ADR-038 split rows without changing human decision."""
        task = self.storage.load_task(req.task_id)
        if task is None:
            return
        step = task.active_step
        if step is None:
            return
        expected = task.updated_at
        statuses: tuple[str, ...]
        if req.status == ApprovalStatus.APPROVED:
            if task.status != TaskStatus.AWAITING_APPROVAL:
                return  # never revive a failed/completed task
            self._approval_mirror(task, step, req,
                                  req.decision_actor or "reconciler")
            task.status = TaskStatus.RUNNING
            statuses = (TaskStatus.AWAITING_APPROVAL.value,)
        elif req.status in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED):
            if task.status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING):
                return
            reason = ("approval expired; recovery requires new authorization"
                      if req.status == ApprovalStatus.EXPIRED else "approval denied")
            self._approval_mirror(task, step, req,
                                  req.decision_actor or "reconciler")
            step.status = StepStatus.FAILED
            step.error = reason
            task.status = TaskStatus.FAILED
            task.error = reason
            task.completed_at = utcnow()
            statuses = (TaskStatus.AWAITING_APPROVAL.value,
                        TaskStatus.RUNNING.value)
        else:
            return
        reconcile = getattr(self.approval_store,
                            "reconcile_approval_task", None)
        if not callable(reconcile):
            raise ApprovalError(
                "approval store lacks reconciliation support (fail closed)"
            )
        reconcile(req, task, expected, statuses)
        if self.goal_manager is not None and task.goal_id:
            try:
                self.goal_manager.recheck_blockers(task.goal_id)
            except Exception:
                pass

    def _resolve_legacy(self, task: Task, rec: dict, outcome: "ApprovalOutcome", actor: str) -> Task:
        """Legacy in-memory resolution when no durable queue is wired."""
        rec["resolved_by"] = actor
        rec["resolved_at"] = utcnow()
        step = task.active_step
        gm = self.goal_manager
        if outcome == ApprovalOutcome.APPROVED:
            rec["outcome"] = "approved"
            task.status = TaskStatus.RUNNING
            if gm is not None and task.goal_id:
                try:
                    gm.clear_blocker(task.goal_id, "approval_pending", reason="approval_granted")
                except Exception:
                    pass
            self._emit("goal.approval.granted", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id, "step_index": step.index,
                "actor": actor,
            })
        else:
            rec["outcome"] = "denied"
            step.status = StepStatus.FAILED
            step.error = "approval denied"
            task.status = TaskStatus.FAILED
            task.error = "approval denied"
            task.completed_at = utcnow()
            if gm is not None and task.goal_id:
                try:
                    gm.clear_blocker(task.goal_id, "approval_pending", reason="approval_denied")
                except Exception:
                    pass
            self._emit("approval.denied", task_id=task.id, step_id=_step_id(step),
                       success=False, detail={"scope": rec.get("request", {}).get("scope"),
                                              "resource": rec.get("request", {}).get("resource"),
                                              "reason": "approval denied"})
            self._emit("goal.approval.denied", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id, "step_index": step.index,
                "actor": actor, "reason": "approval denied",
            })
            self._emit("task.failed", task_id=task.id, detail={
                "step_index": step.index, "error": "approval denied",
            })
            self._record_memory(task)
        task.updated_at = utcnow()
        self.storage.save_task(task)
        return task

    # ------------------------------------------------------------------ #
    # approval queue helpers
    # ------------------------------------------------------------------ #

    def _pending_request_for_step(self, task_id: str, step_index: int) -> "ApprovalRequest | None":
        """The latest PENDING durable request for a task/step, if any."""
        req = self.approval_store.latest_request_for_step(task_id, step_index)
        if req is not None and req.status == ApprovalStatus.PENDING:
            return req
        return None

    def _pending_queue_request(
        self,
        task_id: str,
        step_index: int,
        request: AuthorizationRequest,
    ) -> "ApprovalRequest | None":
        """Dedupe current and legacy durable authorization fingerprints."""
        for req in self.approval_store.list_requests(
                status=ApprovalStatus.PENDING.value):
            if (req.task_id == task_id and req.step_index == step_index
                    and self._fingerprint_matches(req.fingerprint, request)):
                return req
        return None

    def _queue_request_from_auth(self, task: Task, step: PlanStep, request: AuthorizationRequest,
                                 decision: PolicyDecision) -> "ApprovalRequest":
        fp = self._authz_fingerprint(request)
        presentation = present_resource(request.resource_kind, request.resource)
        summary = (
            f"{request.capability}/{request.action} "
            f"{('on ' + str(presentation.display)) if presentation.display else ''} "
            f"(scope={request.scope}, risk={request.risk})"
        ).strip()
        return ApprovalRequest(
            approval_id=new_id("approval"),
            task_id=task.id,
            step_index=step.index,
            goal_id=task.goal_id,
            capability=request.capability,
            action=request.action,
            scope=request.scope,
            risk=request.risk,
            side_effects=request.side_effects,
            resource_kind=request.resource_kind,
            resource=presentation.display,
            summary=summary[:300],
            requester_actor=request.actor.id,
            actor_chain=list(request.actor.chain),
            params_keys=sorted(request.params.keys()),
            fingerprint=fp,
        )

    def _mirror_from_request(self, task: Task, step: PlanStep, req: "ApprovalRequest") -> dict:
        """Keep the task-level mirror record in sync with the queue record."""
        rec = {
            "record_id": new_id("apr"),
            "approval_id": req.approval_id,
            "step_index": step.index,
            "outcome": req.status.value,
            "actor": req.requester_actor,
            "created_at": req.created_at,
            "reason": req.summary[:200],
            "request": {
                "capability": req.capability,
                "action": req.action,
                "scope": req.scope,
                "risk": req.risk,
                "side_effects": req.side_effects,
                "resource_kind": req.resource_kind,
                "resource": req.resource,
                "resource_fingerprint": req.fingerprint.get("resource_fingerprint"),
                "resource_redacted": bool(req.fingerprint.get("resource_redacted", False)),
                "params_keys": req.params_keys,
            },
            "fingerprint": req.fingerprint,
        }
        task.approvals = list(task.approvals or []) + [rec]
        return rec

    def _plan_for_goal(
        self,
        goal_id: str,
        replan_reason: str | None = None,
        goal_run_claim=None,
    ) -> Task | None:
        """Create + plan a task for a goal (records an immutable plan version).

        STORED-PLAN FAST PATH (ADR-016 addendum Phase B): when the goal's
        LATEST plan version already exists (e.g. a re-adopted historical
        plan from readopt_plan) and no task implements it yet, the task's
        steps are reconstructed deterministically from the stored
        plan_summary - the planner is NOT invoked. Re-adoption is
        INFORMATIONAL: the reconstructed steps are treated exactly like a
        freshly planned task (status normalization to PENDING/SKIPPED) and
        every step passes the FULL live authorization pipeline at
        execution time - historical authorization/capability decisions are
        never trusted. A stored version whose plan_summary is
        malformed/oversized or whose strategy is unknown fails closed
        (ValueError) rather than executing.
        """
        gm = self.goal_manager
        if not self._goal_run_lease_current(
                goal_id, goal_run_claim, "plan selection"):
            return None
        goal = gm.get_goal(goal_id)
        if goal is None:
            return None
        latest = gm.latest_plan(goal_id)
        if latest is not None and replan_reason is None:
            exact = sorted(
                (task for task in gm.task_history(goal_id)
                 if task.plan_version == latest["plan_version"]),
                key=lambda task: (task.created_at, task.id),
            )
            if exact:
                canonical = exact[0]
                if not self._task_matches_latest_plan(canonical, latest):
                    raise ValueError(
                        f"task {canonical.id} diverges from stored plan "
                        f"v{latest['plan_version']} (fail closed)"
                    )
                return canonical
            strategy = latest.get("strategy", "") or ""
            summary = latest.get("plan_summary")
            from arion.cognition.strategy import STRATEGY_NAMES

            if strategy in STRATEGY_NAMES and isinstance(summary, list) \
                    and summary and all(isinstance(s, dict) for s in summary) \
                    and len(summary) <= 500:
                # Stored-plan execution: rebuild PlanStep objects from the
                # stored summary. Steps are normalized exactly like planner
                # output (PENDING unless explicitly SKIPPED) - a stored
                # summary can never carry execution state.
                try:
                    steps = [PlanStep.from_dict(s) for s in summary]
                except Exception as exc:
                    raise ValueError(
                        f"stored plan v{latest['plan_version']} for goal "
                        f"{goal_id} has an invalid step shape: {exc} "
                        f"(fail closed)") from None
                for s in steps:
                    if s.status not in (StepStatus.PENDING, StepStatus.SKIPPED):
                        s.status = StepStatus.PENDING
                        s.result = None
                        s.error = None
                candidate = Task(
                    id=new_id("task"),
                    goal_id=goal.id,
                    description=goal.description,
                    plan_version=latest["plan_version"],
                    steps=steps,
                    status=TaskStatus.PLANNED,
                )
                claim_task = getattr(self.storage, "claim_task_for_plan", None)
                if not callable(claim_task):
                    raise TaskStateError(
                        "storage lacks atomic stored-plan task claim "
                        "(fail closed)"
                    )
                if not self._goal_run_lease_current(
                        goal_id, goal_run_claim,
                        "stored-plan task publication"):
                    return None
                task, published = claim_task(candidate)
                if not self._task_matches_latest_plan(task, latest):
                    raise ValueError(
                        f"task {task.id} does not reproduce stored plan "
                        f"v{latest['plan_version']} (fail closed)"
                    )
                if published:
                    self._emit("task.created", task_id=task.id,
                               detail={"goal_id": goal.id})
                    self._emit(
                        "plan.produced",
                        task_id=task.id,
                        detail={"steps": self._plan_steps_for_audit(steps),
                                "stored_plan": True,
                                "plan_version": latest["plan_version"]},
                    )
                return task
        task = self.create_task(goal)
        return self._plan(
            task, replan_reason=replan_reason,
            goal_run_claim=goal_run_claim,
        )

    def run_task(self, task_id: str) -> Task:
        """Resume one task only while holding its goal's durable run lease."""
        task = self.storage.load_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        claim = self._acquire_goal_run_lease(task.goal_id)
        if claim is None:
            return task
        try:
            return self._run_task_owned(
                task_id, goal_run_claim=claim
            )
        finally:
            self._release_goal_run_lease(task.goal_id, claim)

    def _run_task_owned(self, task_id: str, goal_run_claim=None) -> Task:
        """Resume-or-start a task with goal-run ownership already held.

        Stopping points: COMPLETED, FAILED, or AWAITING_APPROVAL (the task is
        checkpointed and returned so an approval interface can act; calling
        run_task again after approval resumes from the exact same step).

        Survives restarts: if a checkpoint exists, state is restored and the
        task resumes from the checkpointed step instead of starting over.
        """
        task = self.storage.load_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if not self._goal_run_allows_task(
                task, goal_run_claim, "task resume"):
            return task
        if self._fence_task_for_superseded_plan(task):
            return self.storage.load_task(task.id) or task
        if self._goal_is_paused(task):
            return task

        # The durable task row is the terminal authority.  A checkpoint is a
        # recovery snapshot, never a way to move terminal work backwards.
        if task.status in TASK_TERMINAL_STATUSES:
            return task

        checkpoint = self.storage.latest_checkpoint(task_id)
        restored = None
        if checkpoint is not None:
            restored = Task.from_dict(checkpoint.snapshot)
            if (restored.revision == task.revision == 0
                    and checkpoint.created_at >= task.updated_at):
                # Compatibility for pre-ADR-040 rows/checkpoints that carry no
                # revision.  Terminal rows were already protected above.
                task = restored
            # Revisioned task rows are always authoritative. A checkpoint
            # cannot mint a higher revision, and equal-revision snapshots add
            # no state that was not already committed to the task row.
        if checkpoint is not None:
            # mid_execution distinguishes a genuine recovery (a task that had
            # begun executing steps) from a plan-only checkpoint (the normal
            # start-of-run boundary, NOT an interruption). Memory uses this to
            # avoid labeling every completed task as 'recovered'.
            src = restored if restored is not None else task
            mid_execution = checkpoint.status != TaskStatus.PLANNED.value
            self._emit(
                "task.resumed", task_id=task_id,
                detail={"step_index": src.current_step, "mid_execution": mid_execution},
            )
        else:
            self._emit("task.planning", task_id=task_id)

        # A durable terminal goal is stronger than an earlier human approval.
        # Never let an approved-but-not-yet-run task revive cancelled/failed
        # goal work (ADR-038).
        if (task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                and self._goal_is_terminal_for_approval(task)):
            step = task.active_step
            if (task.status == TaskStatus.AWAITING_APPROVAL
                    and step is not None and self.approval_store is not None):
                pending = self._pending_request_for_step(task.id, step.index)
                if pending is not None:
                    self._apply_approval_status(
                        pending, ApprovalStatus.DENIED,
                        "system:terminal_goal",
                        "approval rejected: terminal goal",
                    )
                    return self.storage.load_task(task.id)
            if step is not None and step.status in (
                    StepStatus.PENDING, StepStatus.RUNNING):
                step.status = StepStatus.FAILED
                step.error = "approval cannot resume work for terminal goal"
            task.status = TaskStatus.FAILED
            task.error = "approval cannot resume work for terminal goal"
            task.completed_at = utcnow()
            self.storage.save_task(task)
            self._cancel_waiters_for_task(task)
            self._emit("task.failed", task_id=task.id, detail={
                "step_index": step.index if step is not None else None,
                "error": task.error,
            })
            return task

        # Already terminal (e.g. restored from a compatible checkpoint):
        # return as-is.
        if task.status in TASK_TERMINAL_STATUSES:
            return task

        # Approval still pending (durable queue record exists): stop cleanly
        # and idempotently - never re-execute, re-request or re-queue the
        # awaiting step (ADR-018). resolve_approval(APPROVED) flips the task
        # to RUNNING, which is how the exact-step resume proceeds.
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return task

        # A REQUIRED recovery is task execution authority too, not merely a
        # run_goal planning hint.  This repairs a recovery-row-before-task-save
        # crash without replaying the uncertain mutation.
        if self._fence_task_on_open_recovery(task):
            return task

        if not task.steps:
            task = self._plan(task, goal_run_claim=goal_run_claim)
            if self._goal_is_paused(task):
                return self.storage.load_task(task.id) or task
            if task.status == TaskStatus.FAILED:
                self._record_memory(task)
                return task
        elif checkpoint is None:
            # Dependency-aware execution: for hand-built plans, validate and
            # order steps so every step runs only after its dependencies.
            try:
                task.steps = topo_sort_steps(task.steps)
            except PlanValidationError as exc:
                summary = summarize_error(
                    exc,
                    source=classify_error_source(exc),
                    category=getattr(exc, "category", "plan_validation"),
                )
                task.status = TaskStatus.FAILED
                task.error = sanitize_error_text(
                    f"planning failed: {summary.message}",
                    max_length=500,
                )
                task.completed_at = utcnow()
                self.storage.save_task(task)
                detail = summary.to_event_detail()
                detail["error"] = task.error
                self._emit(
                    "error",
                    task_id=task.id,
                    success=False,
                    detail=detail,
                )
                self._emit("task.failed", task_id=task.id, detail={"error": task.error})
                self._record_memory(task)
                return task

        # ADR-024: dependency-aware concurrent dispatch. The scheduler runs
        # ready steps on bounded workers; every dispatched step still goes
        # through the FULL per-step pipeline (live authorization -> approval
        # -> durable mutation lock -> FIFO queue -> capability -> verify),
        # so concurrency never grants authorization. max_concurrency=1
        # reproduces the historical sequential behavior exactly.
        self._skipped_emitted = getattr(self, "_skipped_emitted", set())
        while True:
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "task execution round"):
                return self.storage.load_task(task.id) or task
            if self._fence_task_for_superseded_plan(task):
                return self.storage.load_task(task.id) or task
            if self._goal_is_paused(task):
                return self.storage.load_task(task.id) or task
            # emit step.skipped provenance for skipped steps we walk past
            for st in task.steps:
                if st.status == StepStatus.SKIPPED and (task.id, st.index) not in self._skipped_emitted:
                    self._skipped_emitted.add((task.id, st.index))
                    self._emit("step.skipped", task_id=task.id, step_id=_step_id(st), detail={
                        "reason": st.skipped_reason or "skipped", "guidance": st.guidance[:5],
                    })

            if self._fail_task_for_terminal_goal(
                    task, phase="execution dispatch"):
                return task
            if self._fence_task_on_open_recovery(task):
                return task

            pending = [i for i, st in enumerate(task.steps) if st.status == StepStatus.PENDING]
            if not pending:
                if self._observe_goal_pause(task):
                    return self.storage.load_task(task.id) or task
                if not self._goal_run_allows_task(
                        task, goal_run_claim, "task completion"):
                    return self.storage.load_task(task.id) or task
                # every step handled -> terminal COMPLETED (existing semantics)
                skipped = sum(1 for st in task.steps if st.status == StepStatus.SKIPPED)
                task.status = TaskStatus.COMPLETED
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._checkpoint(task, reason="task completed")
                self._emit("task.completed", task_id=task.id,
                           detail={"steps": len(task.steps), "skipped_steps": skipped})
                self._record_memory(task)
                self._cancel_waiters_for_task(task)
                return task

            cursor = min(pending)
            task.current_step = cursor

            # The cursor step is ALWAYS dispatched (this is how approvals get
            # requested and the serial path behaves). Additional ready steps
            # are dispatched when: all dependencies are terminal-success or
            # explicitly SKIPPED, no self-lock collision (waiting disabled),
            # and the live policy decision is not REQUIRE_APPROVAL-without-
            # an-existing-approved-record (such a step waits for the cursor).
            dispatch = [cursor]
            chosen_resources: set[tuple[str, str]] = set()
            cursor_spec = self.registry.action_spec(task.steps[cursor].capability, task.steps[cursor].action)
            if cursor_spec is not None:
                ck, cr = self._lock_canonical(cursor_spec, task.steps[cursor])
                if ck and cr:
                    chosen_resources.add((ck, cr))
            for i in pending:
                if i == cursor:
                    continue
                step = task.steps[i]
                if not self._step_deps_terminal(task, step):
                    continue
                spec = self.registry.action_spec(step.capability, step.action)
                if spec is None:
                    dispatch.append(i)  # will fail fast inside _execute_step
                    continue
                if getattr(spec, "side_effects", "read_only") == "mutating":
                    k, r = self._lock_canonical(spec, step)
                    if k and r:
                        key = (k, r)
                        if self.lock_wait_max_seconds <= 0:
                            # waiting disabled: never dispatch two same-resource
                            # mutators in one round (the second would fail
                            # instantly at acquire)
                            if key in chosen_resources or self._inflight_lock_held(k, r):
                                continue
                        chosen_resources.add(key)
                request = self._build_authz_request(task, step, spec)
                decision = self.policy.decide(request)
                if (decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
                        and not self._step_has_approved_record(task, step, spec)):
                    continue  # leave pending; handled when it becomes the cursor
                dispatch.append(i)

            dispatch = dispatch[: self.max_concurrency]
            task.status = TaskStatus.RUNNING
            self._heartbeat_registration()
            enqueued = 0
            for i in dispatch:
                if self._admit_step(
                        task, task.steps[i], goal_run_claim=goal_run_claim):
                    enqueued += 1
            self._last_run_progress = self._last_run_progress or enqueued > 0
            if enqueued:
                self.scheduler.run_until_done()
            else:
                if getattr(task, "_goal_run_ownership_lost", False):
                    return self.storage.load_task(task.id) or task
                # Nothing was claimed.  Do not publish this runner's local
                # RUNNING cursor over the live owner's task snapshot.
                canonical = self.storage.load_task(task.id) or task
                if canonical.status not in (
                        TaskStatus.COMPLETED, TaskStatus.FAILED,
                        TaskStatus.AWAITING_APPROVAL):
                    # Coordination-only return state: let run_goal stop rather
                    # than spin, without replacing the live owner's snapshot.
                    canonical.status = TaskStatus.RUNNING
                return canonical

            if getattr(task, "_goal_run_ownership_lost", False):
                return self.storage.load_task(task.id) or task
            if (self._consume_goal_pause(task)
                    or self._goal_is_paused(task)):
                return self.storage.load_task(task.id) or task

            cstep = task.steps[cursor]
            if task.status == TaskStatus.FAILED:
                # A worker may have observed goal cancellation after capability
                # return.  Respect that terminal task state even when the step
                # itself has a known SUCCEEDED outcome.
                canonical = self.storage.load_task(task.id)
                return canonical or task
            if cstep.status == StepStatus.PENDING and task.status == TaskStatus.AWAITING_APPROVAL:
                # Cursor paused on approval: commit the task mirror before its
                # checkpoint.  Revision CAS rejects a concurrent terminal row.
                self.storage.save_task(task)
                self._checkpoint(task, reason="awaiting approval")
                return task
            if cstep.status == StepStatus.FAILED or any(
                    task.steps[i].status == StepStatus.FAILED for i in dispatch):
                failed_step = next((task.steps[i] for i in dispatch
                                    if task.steps[i].status == StepStatus.FAILED), cstep)
                task.status = TaskStatus.FAILED
                task.error = failed_step.error or "step failed"
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._cancel_waiters_for_task(task)
                self._emit("task.failed", task_id=task.id,
                           detail={"step_index": failed_step.index, "error": task.error})
                self._record_memory(task)
                return task
            self._checkpoint(task, reason="step completed")

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return task
        raise RuntimeError(f"task {task_id} terminated without terminal state")

    # ---------- ADR-025: shared multi-task / multi-goal execution ----------

    def run_tasks(self, task_ids: list[str]) -> dict[str, Task]:
        """Drive at most one requested task per owned goal-run lease."""
        tasks: dict[str, Task] = {}
        grouped: dict[str, list[str]] = {}
        for task_id in dict.fromkeys(task_ids):
            task = self.storage.load_task(task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            tasks[task_id] = task
            grouped.setdefault(task.goal_id, []).append(task_id)

        claims: dict[str, Any] = {}
        results: dict[str, Task] = {}
        owned_task_ids: list[str] = []
        try:
            for goal_id, goal_task_ids in grouped.items():
                claim = self._acquire_goal_run_lease(goal_id)
                if claim is None:
                    results.update({task_id: tasks[task_id]
                                    for task_id in goal_task_ids})
                    continue
                claims[goal_id] = claim
                chosen = next(
                    (task_id for task_id in goal_task_ids
                     if tasks[task_id].status not in TASK_TERMINAL_STATUSES),
                    goal_task_ids[0],
                )
                owned_task_ids.append(chosen)
                results.update({task_id: tasks[task_id]
                                for task_id in goal_task_ids
                                if task_id != chosen})
            if owned_task_ids:
                results.update(self._run_tasks_owned(
                    owned_task_ids, goal_run_claims=claims
                ))
            return results
        finally:
            for goal_id in reversed(list(claims)):
                self._release_goal_run_lease(goal_id, claims[goal_id])

    def _run_tasks_owned(
        self,
        task_ids: list[str],
        goal_run_claims: dict[str, Any] | None = None,
    ) -> dict[str, Task]:
        """Drive MULTIPLE preclaimed tasks through one scheduler (ADR-025).

        Semantics:

        - one shared `StepScheduler` + one global `max_concurrency`; total
          running workers never exceed the bound (the scheduler's worker pool
          is capped, and each round admits at most `max_concurrency` items);
        - fair admission: rounds rotate the task order round-robin and admit
          at most `ceil(max_concurrency / active_tasks)` steps per task per
          round, so one goal can never monopolize all capacity and a task
          with few ready steps gets a worker within one round;
        - every admitted step runs the FULL per-step pipeline (live
          authorization -> approval -> durable mutation lock -> FIFO queue ->
          capability -> verify) on its worker - concurrency never grants
          authorization and the durable lock store stays the only mutation
          ownership authority;
        - dependencies stay authoritative per task; a blocked/approval-
          pending/recovery-gated step is never admitted;
        - safe parking: a mutating step whose canonical resource is actively
          locked by ANOTHER task is PARKED - a durable FIFO waiter row is
          registered (deadline/attempts preserved), but NO worker is
          consumed and no registry row is created;
        - clean stop: when a round admits nothing and every remaining task is
          parked (or blocked), the call returns the current task states
          instead of spinning; parked tasks resume on the next call (the
          goal manager's await_lock path rechecks the live lock store).
        """
        tasks: dict[str, Task] = {}
        for tid in task_ids:
            task = self.storage.load_task(tid)
            if task is None:
                raise KeyError(f"task not found: {tid}")
            tasks[tid] = task
        results: dict[str, Task] = {}
        round_no = 0
        self._last_run_progress = False
        while tasks:
            round_no += 1
            order = list(tasks.keys())
            order = order[round_no % len(order):] + order[:round_no % len(order)]
            per_task_cap = max(1, -(-self.max_concurrency // max(1, len(order))))
            results_before_round = len(results)
            # ---------------- round: compute candidates / parks ----------------
            plan: dict[str, dict[str, Any]] = {}
            for tid in order:
                task = tasks[tid]
                goal_run_claim = (
                    (goal_run_claims or {}).get(task.goal_id)
                    if goal_run_claims is not None else None
                )
                if not self._goal_run_allows_task(
                        task, goal_run_claim, "shared task round"):
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                if task.status in TASK_TERMINAL_STATUSES:
                    results[tid] = task
                    continue
                if self._fence_task_for_superseded_plan(task):
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                if task.status == TaskStatus.AWAITING_APPROVAL:
                    results[tid] = task
                    continue
                if self._goal_is_paused(task):
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                if self._fail_task_for_terminal_goal(
                        task, phase="shared execution dispatch"):
                    results[tid] = task
                    continue
                if self._fence_task_on_open_recovery(task):
                    results[tid] = task
                    continue
                if not task.steps:
                    task = self._plan(
                        task, goal_run_claim=goal_run_claim
                    )
                    if not self._goal_run_allows_task(
                            task, goal_run_claim, "post-planning"):
                        results[tid] = self.storage.load_task(task.id) or task
                        continue
                    if self._goal_is_paused(task):
                        results[tid] = self.storage.load_task(task.id) or task
                        continue
                    if task.status == TaskStatus.FAILED:
                        results[tid] = task
                        continue
                pending = [i for i, st in enumerate(task.steps)
                           if st.status == StepStatus.PENDING]
                if not pending:
                    self._complete_task_shared(
                        task, goal_run_claim=goal_run_claim
                    )
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                cursor = min(pending)
                task.current_step = cursor
                task.status = TaskStatus.RUNNING
                chosen: set[tuple[str, str]] = set()
                cursor_parked = False
                cursor_spec = self.registry.action_spec(
                    task.steps[cursor].capability, task.steps[cursor].action)
                if cursor_spec is not None:
                    ck, cr = self._lock_canonical(cursor_spec, task.steps[cursor])
                    if ck and cr:
                        if (self._cursor_lock_parked(task, cursor, cursor_spec, ck, cr)):
                            cursor_parked = True
                        else:
                            chosen.add((ck, cr))
                candidates = [cursor] if not cursor_parked else []
                parked: list[int] = [cursor] if cursor_parked else []
                for i in pending:
                    if i == cursor:
                        continue
                    ok, reason = self._step_dispatchable(task, i, chosen, lock_gate=True)
                    if ok:
                        candidates.append(i)
                    elif reason == "parked":
                        parked.append(i)
                plan[tid] = {
                    "task": task,
                    "candidates": candidates,
                    "parked": parked,
                    "cursor": cursor,
                    "goal_run_claim": goal_run_claim,
                }
            # ---------------- park (durable waiter, no worker) ----------------
            for tid, info in plan.items():
                if not self._goal_run_allows_task(
                        info["task"], info["goal_run_claim"],
                        "shared lock parking"):
                    info["candidates"] = []
                    info["parked"] = []
                    results[tid] = (
                        self.storage.load_task(info["task"].id)
                        or info["task"]
                    )
                    continue
                for i in info["parked"]:
                    self._park_on_lock(info["task"], info["task"].steps[i])
            # ---------------- fair admission across tasks --------------------
            admitted: list[tuple[str, int]] = []
            taken: dict[str, int] = {}
            cursor_pos = {tid: 0 for tid in plan}
            while len(admitted) < self.max_concurrency:
                progressed = False
                for tid in order:
                    if tid not in plan or taken.get(tid, 0) >= per_task_cap:
                        continue
                    info = plan[tid]
                    if cursor_pos[tid] >= len(info["candidates"]):
                        continue
                    admitted.append((tid, info["candidates"][cursor_pos[tid]]))
                    cursor_pos[tid] += 1
                    taken[tid] = taken.get(tid, 0) + 1
                    progressed = True
                    if len(admitted) >= self.max_concurrency:
                        break
                if not progressed:
                    break
            # ---------------- dispatch + drain --------------------------------
            self._heartbeat_registration()
            enqueued = 0
            for tid, i in admitted:
                task = plan[tid]["task"]
                step = task.steps[i]
                if self._admit_step(
                        task, step,
                        goal_run_claim=plan[tid]["goal_run_claim"]):
                    enqueued += 1
            if enqueued:
                self.scheduler.run_until_done()
            # ---------------- post-round per task -----------------------------
            for tid, info in plan.items():
                task = info["task"]
                if getattr(task, "_goal_run_ownership_lost", False):
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                if (self._consume_goal_pause(task)
                        or self._goal_is_paused(task)):
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                cstep = task.steps[info["cursor"]]
                if task.status == TaskStatus.FAILED:
                    results[tid] = self.storage.load_task(task.id) or task
                    continue
                if cstep.status == StepStatus.PENDING and task.status == TaskStatus.AWAITING_APPROVAL:
                    self.storage.save_task(task)
                    self._checkpoint(task, reason="awaiting approval")
                    results[tid] = task
                    continue
                failed_indices = [i for i in info["candidates"] + info["parked"]
                                  if task.steps[i].status == StepStatus.FAILED]
                if cstep.status == StepStatus.FAILED or failed_indices:
                    failed_step = task.steps[failed_indices[0]] if failed_indices else cstep
                    task.status = TaskStatus.FAILED
                    task.error = failed_step.error or "step failed"
                    task.completed_at = utcnow()
                    self.storage.save_task(task)
                    self._cancel_waiters_for_task(task)
                    self._emit("task.failed", task_id=task.id,
                               detail={"step_index": failed_step.index, "error": task.error})
                    self._record_memory(task)
                    results[tid] = task
                    continue
                if info["candidates"]:
                    self._checkpoint(task, reason="step completed")
            self._last_run_progress = (self._last_run_progress
                                        or enqueued > 0
                                        or len(results) > results_before_round)
            for tid in list(results):
                tasks.pop(tid, None)
            if not tasks:
                break
            if not admitted or not enqueued:
                # every remaining task is parked on a foreign lock, the
                # cursor was approval-gated with nothing else ready, or the
                # cross-process capacity is exhausted (nothing was claimed):
                # return cleanly - the caller re-checks the live lock store /
                # approval state / capacity and re-invokes. No spin.
                for tid, task in tasks.items():
                    results[tid] = task
                break
        return results

    def _cursor_lock_parked(self, task: Task, cursor: int, spec, kind: str,
                            resource: str) -> bool:
        """Park the cursor only when it would otherwise occupy a worker
        waiting on a foreign lock AND it is not approval-gated (an
        approval-gated cursor must still dispatch to REQUEST approval - it
        stops before ever touching the lock)."""
        if self.lock_wait_max_seconds <= 0:
            return False
        if not self._lock_is_active(kind, resource):
            return False
        request = self._build_authz_request(task, task.steps[cursor], spec)
        decision = self.policy.decide(request)
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL and not self._step_has_approved_record(
                task, task.steps[cursor], spec):
            return False  # must dispatch to request approval
        return True

    def _step_dispatchable(self, task: Task, i: int,
                           chosen_resources: set[tuple[str, str]],
                           lock_gate: bool = False) -> tuple[bool, str]:
        """ADR-024/025 readiness for a PENDING non-cursor step. Pure decision
        (no side effects): dependencies, capability spec, same-resource
        collision (waiting disabled), the ADR-025 lock gate (parking), and
        the approval gate. Returns (dispatchable, reason)."""
        step = task.steps[i]
        if not self._step_deps_terminal(task, step):
            return False, "deps"
        spec = self.registry.action_spec(step.capability, step.action)
        if spec is None:
            return True, ""  # will fail fast inside _execute_step
        if getattr(spec, "side_effects", "read_only") == "mutating":
            k, r = self._lock_canonical(spec, step)
            if k and r:
                key = (k, r)
                if self.lock_wait_max_seconds <= 0:
                    if key in chosen_resources or self._inflight_lock_held(k, r):
                        return False, "same-resource"
                elif lock_gate and self._lock_is_active(k, r):
                    return False, "parked"
                chosen_resources.add(key)
        request = self._build_authz_request(task, step, spec)
        decision = self.policy.decide(request)
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL and not self._step_has_approved_record(
                task, step, spec):
            return False, "approval"
        return True, ""

    def _park_on_lock(self, task: Task, step: PlanStep) -> None:
        """ADR-025 safe parking: register a DURABLE FIFO waiter row for a
        step whose canonical resource is actively locked, persist the wait
        metadata (deadline/attempts preserved across restarts), and consume
        NO worker. The step is re-dispatched by a later round once the lock
        frees; `_acquire_mutation_lock` then picks up the persisted waiter
        row and acquires head-gated (FIFO). If the deadline elapsed while
        parked, the step fails durably (typed timeout, no recovery record)."""
        spec = self.registry.action_spec(step.capability, step.action)
        if spec is None:
            return
        kind, resource = self._lock_canonical(spec, step)
        if kind is None or resource is None:
            return
        if self.mutation_lock_store is None:
            return
        prior = None
        if (task.lock_wait or {}).get("resource") == resource \
                and (task.lock_wait or {}).get("step_index") == step.index:
            prior = task.lock_wait
        attempts = int(prior.get("attempts", 0)) if prior else 0
        now = self._lock_now()
        waiter_id = prior.get("waiter_id") if prior else None
        waiter = None
        if waiter_id is not None:
            try:
                waiter = self.mutation_lock_store.get_waiter(waiter_id)
            except Exception:
                waiter = None
            if waiter is None or waiter.status.value != "queued":
                waiter = None
        # The durable waiter row is the deadline authority (ADR-025 Phase H):
        # a forged task-level deadline can never extend a wait. A fresh park
        # computes the deadline from the engine's bounded budget.
        deadline = waiter.deadline if waiter is not None else (
            prior.get("deadline") if prior else None)
        if deadline is None:
            deadline = _iso_plus(now, self.lock_wait_max_seconds)
        if now >= deadline:
            if waiter_id is not None:
                try:
                    self.mutation_lock_store.dequeue_waiter(waiter_id, "timed_out")
                except Exception:
                    pass
            self._fail_lock_wait_timeout(task, step, kind, resource, deadline,
                                         attempts, self._lock_owner(), waiter_id)
            return
        position = None
        if waiter is None:
            try:
                waiter = self.mutation_lock_store.enqueue_waiter(
                    kind, resource, task.id, task.goal_id, step.index,
                    deadline, now=now)
                waiter_id = waiter.waiter_id
                position = waiter.seq
                deadline = waiter.deadline
            except Exception:
                return  # registry-level failure: retry next round
            self._emit("mutation.lock.queued", task_id=task.id,
                       step_id=_step_id(step), success=False, detail={
                           "waiter_id": waiter.waiter_id,
                           "resource_kind": kind, "resource": resource,
                           "capability": step.capability, "action": step.action,
                           "position": waiter.seq, "deadline": deadline,
                       })
        attempts += 1
        backoff = min(
            self.lock_wait_backoff_base * (2 ** min(attempts - 1, 20)),
            self.lock_wait_backoff_max,
        )
        next_retry = _iso_plus(now, backoff)
        task.lock_wait = {
            "step_index": step.index,
            "resource_kind": kind,
            "resource": resource,
            "waiter_id": waiter_id,
            "position": position if position is not None else prior.get("position"),
            "deadline": deadline,
            "attempts": attempts,
            "next_retry": next_retry,
        }
        try:
            self.mutation_lock_store.update_waiter(waiter_id, attempts=attempts,
                                                   next_retry=next_retry)
        except Exception:
            pass
        self._persist_lock_wait(task, step, kind, resource, deadline,
                                attempts, next_retry, waiter_id,
                                position if position is not None
                                else (prior.get("position") if prior else None))
        self._notify_lock_wait_observer(waiter_id, int(waiter.seq), kind, resource,
                                        task.id, task.goal_id, attempts,
                                        deadline, next_retry)

    def _admit_step(
        self, task: Task, step: PlanStep, goal_run_claim=None
    ) -> bool:
        """Admit one step to the shared scheduler + durable registry
        (ADR-025/026). Returns True when the step was dispatched.

        ADR-026 ownership flow:

        1. find-or-reuse the step's QUEUED row (a row left QUEUED by a
           capacity-limited round is retried, never duplicated);
        2. atomically CLAIM it (BEGIN IMMEDIATE in the store: lazy stale
           reclaim + cross-process capacity + QUEUED->RUNNING with a fresh
           worker id + bounded lease). A claim failure due to cross-process
           capacity leaves the row QUEUED for the next round - no worker is
           consumed;
        3. expired RUNNING rows are reclaimed only by the registry's lease
           check; an unexpired foreign row remains authoritative and this
           engine does not dispatch the step;
        4. only a claimed row is enqueued to the in-process worker pool; the
           worker heartbeats throughout execution and reports terminal WITH
           its worker id."""
        if not self._goal_run_allows_task(
                task, goal_run_claim, "scheduler admission"):
            return False
        if self.scheduler_registry is None:
            # no durable registry: plain in-process dispatch (legacy path)
            self.scheduler.enqueue(
                f"{task.id}:{step.index}", task.id, step.index,
                (lambda s=step, g=goal_run_claim:
                 self._run_step_worker(
                     task, s, goal_run_claim=g
                 )))
            return True
        reg = self.scheduler_registry
        now = self._lock_now()
        worker_id = f"worker:{os.getpid()}:{new_id('w')}"
        try:
            # Only the registry's expiry-checked reclaim path may revoke a
            # RUNNING owner.  A live foreign row is not stale merely because
            # it belongs to another engine (ADR-040).
            reg.reclaim_stale(now=now)
            existing = [
                r for r in reg.list_work(task_id=task.id, step_index=step.index)
                if r.status in (SchedulerWorkStatus.QUEUED,
                                SchedulerWorkStatus.RUNNING)]
        except Exception:
            existing = []
        row = None
        for r in existing:
            if r.status == SchedulerWorkStatus.QUEUED:
                row = r
                break
            if r.status == SchedulerWorkStatus.RUNNING:
                # ``reclaim_stale`` above already removed every expired row.
                # Anything still RUNNING has a live lease and cannot be
                # preempted by this engine, whether local or foreign.
                return False
        if row is None:
            try:
                row = reg.create(task_id=task.id, goal_id=task.goal_id,
                                 step_index=step.index,
                                 scheduler_id=self.scheduler_id, now=now)
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"scheduler registry failed closed: {exc}"
                return False
        try:
            claimed = reg.claim(row.work_id, worker_id=worker_id,
                                lease_seconds=self.scheduler_lease_seconds,
                                now=now,
                                max_lease_seconds=self.scheduler_max_lease_seconds,
                                scheduler_id=self.scheduler_id)
        except Exception as exc:
            step.status = StepStatus.FAILED
            step.error = f"scheduler registry failed closed: {exc}"
            return False
        if claimed is None:
            return False  # cross-process capacity full: retry next round
        with self._inflight_lock:
            self._claimed_work[claimed.work_id] = (task.id, step.index)
        self.scheduler.enqueue(
            f"{task.id}:{step.index}", task.id, step.index,
            (lambda s=step, w=claimed.work_id, wk=worker_id,
                    g=goal_run_claim:
             self._run_step_worker(
                 task, s, w, wk, goal_run_claim=g
             )))
        return True

    def _heartbeat_registration(self) -> None:
        """Lazily extend THIS engine's durable scheduler registration
        (ADR-026): keeps a live engine's QUEUED rows abandonable-never while
        peers start up. Best-effort, bounded + monotonic in the store."""
        if (self.scheduler_registry is None or not self._registered
                or not hasattr(self.scheduler_registry, "heartbeat_scheduler")):
            return
        try:
            self.scheduler_registry.heartbeat_scheduler(
                self.scheduler_id,
                lease_seconds=self.scheduler_lease_seconds,
                now=self._lock_now(),
                max_lease_seconds=self.scheduler_max_lease_seconds)
        except Exception:
            pass

    def _complete_task_shared(
        self, task: Task, goal_run_claim=None
    ) -> None:
        """Terminal COMPLETED handling shared by the multi-task driver."""
        if not self._goal_run_allows_task(
                task, goal_run_claim, "shared task completion"):
            return
        if self._fail_task_for_terminal_goal(
                task, phase="shared completion"):
            return
        if self._observe_goal_pause(task):
            return
        if self._fence_task_on_open_recovery(task):
            return
        skipped = sum(1 for st in task.steps if st.status == StepStatus.SKIPPED)
        task.status = TaskStatus.COMPLETED
        task.completed_at = utcnow()
        self.storage.save_task(task)
        self._checkpoint(task, reason="task completed")
        self._emit("task.completed", task_id=task.id,
                   detail={"steps": len(task.steps), "skipped_steps": skipped})
        self._record_memory(task)
        self._cancel_waiters_for_task(task)

    def run_goals(self, goal_ids: list[str], max_replans: int = 5) -> dict[str, Goal]:
        """Drive only goals whose durable run lease this engine owns."""
        gm = self.goal_manager
        if gm is None:
            raise ValueError("goal manager not wired; use execute_goal instead")
        unique_goal_ids = list(dict.fromkeys(goal_ids))
        claims: dict[str, Any] = {}
        results: dict[str, Goal] = {}
        try:
            for goal_id in unique_goal_ids:
                claim = self._acquire_goal_run_lease(goal_id)
                if claim is None:
                    goal = gm.get_goal(goal_id)
                    if goal is None:
                        raise KeyError(f"goal not found: {goal_id}")
                    results[goal_id] = goal
                else:
                    claims[goal_id] = claim
            if claims:
                results.update(self._run_goals_owned(
                    list(claims), max_replans=max_replans,
                    goal_run_claims=claims,
                ))
            return results
        finally:
            for goal_id in reversed(list(claims)):
                self._release_goal_run_lease(goal_id, claims[goal_id])

    def _run_goals_owned(
        self,
        goal_ids: list[str],
        max_replans: int = 5,
        goal_run_claims: dict[str, Any] | None = None,
    ) -> dict[str, Goal]:
        """Drive MULTIPLE preclaimed goals through the shared scheduler (ADR-025).

        Each goal keeps its existing long-horizon lifecycle (ADR-016/017):
        evaluate -> plan -> execute -> observe -> replan/complete, but the
        execute phases of ALL goals share one scheduler, one global
        max_concurrency, and one durable registry. Goals that are blocked
        (approval pending / recovery required / missing capability / lock
        contention) consume no workers and do not stall the others.

        Returns {goal_id: Goal} after every goal is terminal or cleanly
        stopped (blocked/awaiting - the caller re-invokes once the blocker
        resolves, exactly like run_goal)."""
        gm = self.goal_manager
        if gm is None:
            raise ValueError("goal manager not wired; use run_tasks instead")
        active = list(goal_ids)
        results: dict[str, Goal] = {}
        while active:
            tasks_to_run: list[str] = []
            for gid in active:
                goal_run_claim = (
                    (goal_run_claims or {}).get(gid)
                    if goal_run_claims is not None else None
                )
                if not self._goal_run_lease_current(
                        gid, goal_run_claim, "shared goal evaluation"):
                    results[gid] = gm.get_goal(gid)
                    continue
                # ADR-053: converge superseded coordination state before the
                # shared evaluation (per-goal, ownership-scoped).
                self._reconcile_superseded_coordination(
                    gid, goal_run_claim)
                goal = gm.get_goal(gid)
                if goal is None or goal.status in (GoalStatus.COMPLETED, GoalStatus.FAILED):
                    if goal is not None:
                        results[gid] = goal
                    continue
                result, _ = gm.evaluate(gid)
                if not self._goal_run_lease_current(
                        gid, goal_run_claim, "shared post-evaluation"):
                    results[gid] = gm.get_goal(gid)
                    continue
                action = result.next_action
                if action == "complete":
                    if not self._goal_run_lease_current(
                            gid, goal_run_claim, "shared goal completion"):
                        results[gid] = gm.get_goal(gid)
                        continue
                    try:
                        gm.complete_goal(
                            gid,
                            reason="all_work_complete",
                            expect_plan_version=(
                                result.evidence.get("latest_plan_version")
                                if isinstance(result.evidence, dict) else None
                            ),
                        )
                    except GoalPlanLineageError:
                        # ADR-054 (shared loop): stale plan authority for the
                        # completion decision. Fail closed; the goal stays
                        # non-terminal and is re-evaluated against current
                        # durable state on the next cycle/invocation.
                        self._emit("goal.completion.fenced", task_id=None, detail={
                            "goal_id": gid,
                            "expected_plan_version": (
                                result.evidence.get("latest_plan_version")
                                if isinstance(result.evidence, dict) else None
                            ),
                            "reason": "plan lineage advanced before completion",
                        })
                        continue
                    results[gid] = gm.get_goal(gid)
                    continue
                if action == "await_approval":
                    # approval-pending: stop cleanly, never spin on the
                    # awaiting task (ADR-017/018 semantics preserved)
                    continue
                if action == "await_lock":
                    # parked on a mutation lock: re-check the LIVE lock store
                    # (the only lock authority); only when the resource is
                    # free does the parked task resume
                    if gm.recheck_blockers(gid):
                        pending = gm.pending_task(gid)
                        if pending is not None and pending.status not in (
                                TaskStatus.COMPLETED, TaskStatus.FAILED):
                            tasks_to_run.append(pending.id)
                    continue
                if action == "resolve_blocker":
                    if gm.recheck_blockers(gid):
                        continue
                    continue  # still blocked: consumes no worker
                if action in ("none", "paused"):
                    continue  # blocked: consumes no worker, does not stall
                if action == "replan":
                    replan_count = sum(
                        1 for p in gm.plan_history(gid)
                        if str(p.get("reason", "")).startswith("replan"))
                    if replan_count >= max_replans:
                        if not self._goal_run_lease_current(
                                gid, goal_run_claim,
                                "shared goal failure"):
                            results[gid] = gm.get_goal(gid)
                            continue
                        try:
                            gm.fail_goal(
                                gid,
                                reason="max_replans_exceeded",
                                expect_plan_version=(
                                    result.evidence.get("latest_plan_version")
                                    if isinstance(result.evidence, dict) else None
                                ),
                            )
                        except GoalPlanLineageError:
                            # ADR-055 (shared loop): stale plan authority for
                            # the failure decision. Fail closed; the goal
                            # stays non-terminal and is re-evaluated against
                            # current durable state on the next cycle.
                            self._emit("goal.failure.fenced", task_id=None, detail={
                                "goal_id": gid,
                                "expected_plan_version": (
                                    result.evidence.get("latest_plan_version")
                                    if isinstance(result.evidence, dict) else None
                                ),
                                "reason": "plan lineage advanced before failure",
                            })
                            continue
                        results[gid] = gm.get_goal(gid)
                        continue
                    if self._block_on_missing_capability(gid, gm):
                        continue
                    if self._block_on_open_recovery(gid, gm):
                        continue
                    if self._block_on_lock_contention(gid, gm):
                        continue
                    task = self._plan_for_goal(
                        gid,
                        replan_reason=result.evidence.get("reason"),
                        goal_run_claim=goal_run_claim,
                    )
                else:  # continue / initial_plan
                    if self._block_on_missing_capability(gid, gm):
                        continue
                    if self._block_on_open_recovery(gid, gm):
                        continue
                    if self._block_on_lock_contention(gid, gm):
                        continue
                    task = gm.pending_task(gid)
                    if task is None:
                        task = self._plan_for_goal(
                            gid, replan_reason=None,
                            goal_run_claim=goal_run_claim,
                        )
                if task is not None and task.status not in (
                        TaskStatus.COMPLETED, TaskStatus.FAILED):
                    tasks_to_run.append(task.id)
            if not tasks_to_run:
                # every remaining goal is blocked/awaiting: re-check blockers
                # once (capability/approval may have resolved); if nothing
                # changed, stop cleanly - do not spin.
                progressed = False
                for gid in active:
                    if gid not in results and gm.recheck_blockers(gid):
                        progressed = True
                if not progressed:
                    break
                continue
            before = set(results)
            self._run_tasks_owned(
                tasks_to_run, goal_run_claims=goal_run_claims
            )
            if not self._last_run_progress and not (set(results) - before):
                # a full cycle claimed nothing and no goal reached a
                # decision point (e.g. cross-process capacity exhausted):
                # stop cleanly - the caller re-invokes once capacity frees.
                break
            # goals whose task stopped at a decision point stay active; the
            # next cycle evaluates them (replan/fail/complete/await).
        for gid in goal_ids:
            results.setdefault(gid, gm.get_goal(gid))
        return results

    # ---------- pipeline ----------

    def _plan_steps_for_audit(self, steps: list[PlanStep]) -> list[dict[str, Any]]:
        """Non-executable plan metadata with safe resource presentation."""
        out: list[dict[str, Any]] = []
        for step in steps:
            detail: dict[str, Any] = {
                "index": step.index,
                "intent": step.intent[:300],
                "capability": step.capability,
                "action": step.action,
                "scope": step.scope,
                "status": step.status.value,
                "param_keys": sorted(step.params),
                "verification": {
                    "policy": step.verification.policy,
                    "args": step.verification.args,
                },
                "depends_on": list(step.depends_on),
            }
            spec = self.registry.action_spec(step.capability, step.action)
            if spec is not None and spec.resource_kind and spec.resource_param:
                exact = step.params.get(spec.resource_param)
                if isinstance(exact, str):
                    presentation = present_resource(spec.resource_kind, exact)
                    detail["resource_kind"] = spec.resource_kind
                    detail.update(presentation.metadata())
            out.append(detail)
        return out

    def _plan(
        self,
        task: Task,
        replan_reason: str | None = None,
        goal_run_claim=None,
    ) -> Task:
        self._emit("task.planning", task_id=task.id)
        task.status = TaskStatus.PLANNING
        context = self._build_planning_context(task)
        try:
            steps = self.planner.plan(task.description, task.id, self.registry, context=context)
        except Exception as exc:  # planner/model text crosses a trust boundary
            summary = summarize_error(
                exc,
                source=classify_error_source(exc),
                category=getattr(exc, "category", "unknown"),
            )
            task.status = TaskStatus.FAILED
            task.error = sanitize_error_text(
                f"planning failed: {summary.message}",
                max_length=500,
            )
            task.completed_at = utcnow()
            self.storage.save_task(task)
            detail = summary.to_event_detail()
            detail["error"] = task.error
            self._emit(
                "error",
                task_id=task.id,
                success=False,
                detail=detail,
            )
            self._emit("task.failed", task_id=task.id, detail={"error": task.error})
            return task
        # Planner/model calls may block for longer than the goal lease. The
        # exact owner must still be live before immutable plan authority can
        # be minted; ownership loss is a clean stop, not planning failure.
        if not self._goal_run_allows_task(
                task, goal_run_claim, "post-planner plan publication"):
            return self.storage.load_task(task.id) or task
        if self._goal_is_paused(task):
            canonical = self.storage.load_task(task.id)
            if canonical is not None:
                task.__dict__.update(canonical.__dict__)
            return task
        if self._fail_task_for_terminal_goal(
                task, phase="planning"):
            return task
        if not steps:
            # A plan with no steps can never reach a terminal state; fail the
            # task explicitly rather than leaving it dangling in 'planned'.
            task.status = TaskStatus.FAILED
            task.error = "planning produced no steps"
            task.completed_at = utcnow()
            self.storage.save_task(task)
            self._emit("error", task_id=task.id, success=False, detail={"error": task.error})
            self._emit("task.failed", task_id=task.id, detail={"error": task.error})
            self._record_memory(task)
            return task

        # Model/planner output can never carry EXECUTION state (ADR-025
        # Phase H): a plan may only propose work (PENDING) or explicitly
        # skip it (SKIPPED, with provenance from guidance). Any forged
        # status (succeeded/failed/running/completed claims) is normalized
        # back to PENDING so the step still passes live authorization and
        # the real pipeline - a forged completion never sticks.
        for s in steps:
            if s.status not in (StepStatus.PENDING, StepStatus.SKIPPED):
                s.status = StepStatus.PENDING
                s.result = None
                s.error = None
        task.steps = steps
        task.status = TaskStatus.PLANNED
        plan_reason: str | None = None
        plan_record: dict[str, Any] | None = None
        strategy_name = "direct"

        # For managed goals, the immutable plan claim is execution authority.
        # Publish it BEFORE the task becomes durably PLANNED/executable. If
        # task persistence later fails, restart reconstructs from the stored
        # plan; the inverse task-without-plan state must never execute.
        if self.goal_manager is not None and task.goal_id:
            try:
                if context is not None and getattr(context, "strategy", None) is not None:
                    strategy_name = context.strategy.name
                elif self.strategy_selector is not None:
                    beliefs = self.cognition.cognition.list_beliefs(limit=100) if self.cognition else []
                    env_state = self.world_monitor.current_state() if self.world_monitor else {}
                    guidance = list(getattr(context, "guidance", []) or [])
                    outcome_history: list = []
                    try:
                        outcome_history = self.goal_manager.strategy_outcomes(limit=50)
                    except Exception:
                        outcome_history = []
                    strategy_name = self.strategy_selector.select(
                        task.description, beliefs, env_state, guidance,
                        outcome_history=outcome_history,
                    ).name
                history = self.goal_manager.plan_history(task.goal_id)
                plan_reason = "initial_plan" if not history else (
                    f"replan_{replan_reason}" if replan_reason else "replan"
                )
                if not self._goal_run_allows_task(
                        task, goal_run_claim,
                        "immutable plan publication"):
                    return self.storage.load_task(task.id) or task
                plan_record = self.goal_manager.record_plan_version(
                    task.goal_id, strategy_name,
                    [step.to_dict() for step in steps], plan_reason,
                )
                task.plan_version = plan_record["plan_version"]
            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error = "planning persistence failed; execution denied"
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._emit("error", task_id=task.id, success=False, detail={
                    "error": task.error,
                    "error_type": type(exc).__name__,
                    "category": "plan_persistence",
                })
                self._emit("task.failed", task_id=task.id,
                           detail={"error": task.error})
                self._record_memory(task)
                return task

        # One authoritative publication carries normalized steps, PLANNED
        # state, and exact plan version. A stale/restarted claimant adopts the
        # existing canonical task instead of creating a second executable row.
        if self.goal_manager is not None and task.goal_id:
            claim_task = getattr(self.storage, "claim_task_for_plan", None)
            if not callable(claim_task):
                raise TaskStateError(
                    "storage lacks atomic exact-plan task claim (fail closed)"
                )
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "exact-plan task publication"):
                return self.storage.load_task(task.id) or task
            canonical, published = claim_task(task)
            if not published:
                if (plan_record is not None
                        and not self._task_matches_latest_plan(
                            canonical, plan_record
                        )):
                    raise TaskStateError(
                        f"canonical task {canonical.id} diverges from plan "
                        f"v{canonical.plan_version} (fail closed)"
                    )
                task.__dict__.update(canonical.__dict__)
                return task
        else:
            # Standalone engines retain compatible unversioned behavior.
            self.storage.save_task(task)
        self._emit(
            "plan.produced",
            task_id=task.id,
            detail={"steps": self._plan_steps_for_audit(steps)},
        )

        if plan_record is not None and plan_reason is not None \
                and plan_reason.startswith("replan"):
            self._emit("goal.replanned", task_id=task.id, detail={
                "goal_id": task.goal_id,
                "plan_version": plan_record["plan_version"],
                "strategy": strategy_name,
                "reason": plan_reason[:200],
            })
            # Plan history is authoritative; this goal field is a best-effort
            # read-model mirror for CLI/debugging.
            try:
                self.goal_manager.set_replan_reason(task.goal_id, plan_reason)
            except Exception:
                pass
        # Audit memory-driven plan transformation (non-mutating, provenance-
        # carrying). The ORIGINAL plan + every decision are recorded; guidance
        # remains informational - authorization still decides at execution.
        transformation = getattr(self.planner, "last_transformation", None)
        if transformation is not None and transformation.decisions:
            self._emit(
                "planning.memory.transformation",
                task_id=task.id,
                detail={
                    "transformed_steps": len(transformation.transformed),
                    "original_steps": len(transformation.original),
                    "decisions": transformation.decisions[:20],
                    "decision_count": len(transformation.decisions),
                },
            )
        self._checkpoint(task, reason="plan produced")
        return task

    def _step_deps_terminal(self, task: Task, step: PlanStep) -> bool:
        """ADR-024 readiness: a step may be dispatched only when every
        prerequisite is terminal-success or explicitly SKIPPED (existing
        semantics). Dependencies are authoritative - the scheduler never
        bypasses them."""
        for ref in (step.depends_on or []):
            if 0 <= ref < len(task.steps):
                st = task.steps[ref].status
                if st not in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                    return False
        return True

    def _step_has_approved_record(self, task: Task, step: PlanStep, spec) -> bool:
        """True when this step already has an APPROVED record whose canonical
        fingerprint still matches the LIVE spec (stale records never count)."""
        rec = self._approved_record_for(task, step.index)
        if rec is None:
            return False
        try:
            request = self._build_authz_request(task, step, spec)
            return self._fingerprint_matches(rec.get("fingerprint"), request)
        except Exception:
            return False

    def _task_step_is_current(self, task: Task, step: PlanStep) -> bool:
        """Revalidate the task revision and pending step before execution."""
        durable = self.storage.load_task(task.id)
        if durable is None:
            return False
        durable_step = next(
            (candidate for candidate in durable.steps
             if candidate.index == step.index),
            None,
        )
        if (durable.revision != task.revision
                or durable.status in TASK_TERMINAL_STATUSES
                or durable_step is None
                or durable_step.status != StepStatus.PENDING):
            task.__dict__.update(durable.__dict__)
            return False
        return True

    def _start_scheduler_heartbeat(
        self, work_id: str, worker_id: str
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Keep one exact scheduler work claim live while capability code runs."""
        # Validate ownership synchronously before any capability can execute.
        self.scheduler_registry.heartbeat(
            work_id, worker_id,
            lease_seconds=self.scheduler_lease_seconds,
            now=self._lock_now(),
            max_lease_seconds=self.scheduler_max_lease_seconds,
        )
        stop = _threading.Event()
        state: dict[str, Any] = {"error": None}
        interval = max(
            0.01,
            min(5.0, float(self.scheduler_lease_seconds) / 3.0),
        )

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    self.scheduler_registry.heartbeat(
                        work_id, worker_id,
                        lease_seconds=self.scheduler_lease_seconds,
                        now=self._lock_now(),
                        max_lease_seconds=self.scheduler_max_lease_seconds,
                    )
                    self._heartbeat_registration()
                except Exception as exc:
                    state["error"] = exc
                    return

        thread = _threading.Thread(
            target=heartbeat,
            daemon=True,
            name=f"arion-work-heartbeat-{work_id}",
        )
        thread.start()
        return stop, thread, state

    def _run_step_worker(
        self,
        task: Task,
        step: PlanStep,
        work_id: str | None = None,
        worker_id: str | None = None,
        goal_run_claim=None,
    ) -> None:
        """Execute one step on a scheduler worker (ADR-024 Phase D /
        ADR-025/026).

        Terminal per-step status is persisted IMMEDIATELY after the step
        finishes, so a crash mid-round (or process kill) never replays a
        completed mutation on restart: the durable per-step state is the
        unit of restart, not the whole round. Bounded metadata only - the
        task snapshot is the pre-existing durable record (step ids, statuses,
        timestamps); no thread objects, stack traces, capability outputs or
        model output are ever persisted.

        ADR-026/040 ownership: the row was atomically CLAIMED at admission
        (it is already RUNNING with `worker_id`). The worker validates and
        renews that exact ownership throughout capability execution, then
        reports terminal WITH its worker id (a stale owner can never complete
        a row it no longer owns)."""
        scheduler_heartbeat = None
        ownership_lost = not self._goal_run_allows_task(
            task, goal_run_claim, "worker start"
        )
        if (not ownership_lost and work_id is not None
                and self.scheduler_registry is not None):
            try:
                scheduler_heartbeat = self._start_scheduler_heartbeat(
                    work_id, worker_id
                )
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"scheduler lease failed closed: {exc}"
                self.storage.save_task(task)
                self._emit("error", task_id=task.id, step_id=_step_id(step), success=False,
                           detail={"error": step.error[:300]})
                with self._inflight_lock:
                    self._claimed_work.pop(work_id, None)
                return
        persistence_error: BaseException | None = None
        try:
            if (not ownership_lost
                    and self._goal_run_allows_task(
                        task, goal_run_claim, "worker preflight")
                    and not self._fail_task_for_terminal_goal(
                        task, step, phase="worker start")
                    and not self._fence_task_for_superseded_plan(task)
                    and not self._observe_goal_pause(task)
                    and self._task_step_is_current(task, step)):
                self._execute_step(
                    task, step, goal_run_claim=goal_run_claim
                )
                # Capability code is not preemptible. Recheck terminal and
                # pause authority after an invocation returns; a known result
                # is persisted even if the goal-run lease was lost meanwhile.
                # A pre-invocation ownership stop performs no task mutation.
                if getattr(step, "_capability_started", False):
                    self._fail_task_for_terminal_goal(
                        task, step, phase="worker completion")
                    self._fence_task_for_superseded_plan(task)
                    self._observe_goal_pause(task)
        finally:
            heartbeat_state = self._stop_lock_heartbeat(scheduler_heartbeat)
            if heartbeat_state.get("error") is not None:
                self._emit(
                    "error", task_id=task.id, step_id=_step_id(step),
                    success=False, detail={
                        "error": "scheduler ownership heartbeat lost",
                        "error_type": type(heartbeat_state["error"]).__name__,
                        "category": "scheduler_ownership",
                    },
                )

            # ADR-041 ordering: task execution/approval-pause state becomes
            # durable before scheduler work advertises a terminal outcome.
            # If task persistence fails, the work row is FAILED (or remains
            # lease-recoverable after a hard kill), never false-COMPLETED.
            if (task.status == TaskStatus.AWAITING_APPROVAL
                    or step.status in (
                        StepStatus.SUCCEEDED, StepStatus.FAILED,
                        StepStatus.SKIPPED,
                    )):
                try:
                    durable = self.storage.load_task(task.id)
                    if (durable is None
                            or durable.status not in TASK_TERMINAL_STATUSES):
                        self.storage.save_task(task)
                    else:
                        task.__dict__.update(durable.__dict__)
                except BaseException as exc:
                    persistence_error = exc

            if work_id is not None and self.scheduler_registry is not None:
                terminal = (
                    SchedulerWorkStatus.FAILED
                    if (step.status == StepStatus.FAILED
                        or persistence_error is not None
                        or ownership_lost
                        or getattr(
                            task, "_goal_run_ownership_lost", False
                        ))
                    else SchedulerWorkStatus.COMPLETED
                )
                try:
                    self.scheduler_registry.mark_terminal(
                        work_id, terminal,
                        error=(step.error or (
                            "task persistence failed"
                            if persistence_error is not None else (
                                "goal-run ownership lost"
                                if (ownership_lost or getattr(
                                    task, "_goal_run_ownership_lost", False
                                )) else None
                            )
                        )),
                        now=self._lock_now(), owner_worker_id=worker_id)
                except Exception:
                    pass  # the durable step/task state is the authority
                with self._inflight_lock:
                    self._claimed_work.pop(work_id, None)
            if persistence_error is not None:
                raise persistence_error

    def _execute_step(
        self, task: Task, step: PlanStep, goal_run_claim=None
    ) -> None:
        if not self._goal_run_allows_task(
                task, goal_run_claim, "capability pipeline"):
            return
        self._emit(
            "step.started",
            task_id=task.id,
            step_id=_step_id(step),
            detail={"intent": step.intent, "depends_on": step.depends_on},
        )

        # 1. Capability discovery + action spec (source of truth for metadata)
        capability = self.registry.get(step.capability)
        if capability is None:
            step.status = StepStatus.FAILED
            step.error = f"capability not found: {step.capability}"
            self._emit("error", task_id=task.id, step_id=_step_id(step), success=False, detail={"error": step.error})
            return
        spec = self.registry.action_spec(step.capability, step.action)
        if spec is None:
            step.status = StepStatus.FAILED
            step.error = f"unknown action {step.action!r} for capability {step.capability!r}"
            self._emit("error", task_id=task.id, step_id=_step_id(step), success=False, detail={"error": step.error})
            return
        self._emit(
            "capability.discovered",
            task_id=task.id,
            step_id=_step_id(step),
            detail={"capability": step.capability, "action": step.action, "required_scope": spec.required_scope},
        )

        # 2. Authorization (policy decides; scope comes from the ActionSpec, not the plan)
        request = self._build_authz_request(task, step, spec)
        decision = self.policy.decide(request)
        self._emit(
            "permission.checked",
            task_id=task.id,
            step_id=_step_id(step),
            detail=AuthorizationEventDetails.from_mapping(
                decision.to_dict(),
                actor=request.actor.id,
                actor_chain=request.actor.chain,
                param_keys=tuple(request.params),
                step_declared_scope=step.scope,
            ),
        )

        if decision.outcome == PolicyOutcome.DENY:
            step.status = StepStatus.FAILED
            step.error = present_resource_reason(
                decision.reason, request.resource_kind, request.resource
            )
            self._emit(
                "permission.denied",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail=AuthorizationEventDetails.from_mapping(
                    decision.to_dict()
                ),
            )
            return

        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            if not self._handle_approval(task, step, request, decision):
                return  # denied or paused (task will be checkpointed by the caller)

        # 3. Execute with retries, then 4. verify. Revalidate goal ownership
        # before any task fence that could otherwise rewrite a current owner's
        # state; then apply newer-plan/pause authority before capability start.
        if not self._goal_run_allows_task(
                task, goal_run_claim, "capability dispatch"):
            return
        if (self._fence_task_for_superseded_plan(task)
                or self._observe_goal_pause(task)):
            return
        self._execute_with_retries(
            task, step, capability, spec,
            goal_run_claim=goal_run_claim,
        )

    # ---------- authorization helpers ----------

    def _handle_approval(
        self,
        task: Task,
        step: PlanStep,
        request: AuthorizationRequest,
        decision: PolicyDecision,
    ) -> bool:
        """Approval seam: route a REQUIRE_APPROVAL decision to the handler.

        Returns True when the action may proceed (approved), False when it was
        denied or is still pending (the caller must not execute).

        ADR-017/018: a previously APPROVED decision for this exact step is
        honored on resume ONLY when the CURRENT request (rebuilt from live
        ActionSpec + policy metadata) still fingerprints identically. Any
        change to scope/risk/side-effects/resource kind/resource/action/
        security-relevant params forces a FRESH approval request - stale
        approvals never authorize. When the handler returns PENDING, exactly
        ONE durable ApprovalRequest is queued per task/step/authz-fingerprint.
        """
        record = self._approved_record_for(task, step.index)
        if record is not None:
            if self._fingerprint_matches(record.get("fingerprint"), request):
                # the exact approved request is still valid against LIVE
                # metadata: resume without re-requesting
                presentation = present_resource(
                    request.resource_kind, request.resource
                )
                self._emit("task.approval.resumed", task_id=task.id, step_id=_step_id(step), detail={
                    "approval_record": record.get("record_id"),
                    "approval_id": record.get("approval_id"),
                    "resolved_by": record.get("resolved_by"),
                    "scope": request.scope,
                    **presentation.metadata(),
                })
                if self.goal_manager is not None and task.goal_id:
                    try:
                        self.goal_manager.clear_blocker(task.goal_id, "approval_pending", reason="approval_resumed")
                    except Exception:
                        pass
                return True
            # stale approval (metadata changed): fall through to a fresh request

        if self.approval_store is not None:
            existing = self._pending_queue_request(task.id, step.index, request)
            if existing is not None:
                # we are already durably waiting on this exact request:
                # idempotent - no new record, no re-request, no re-queue
                task.status = TaskStatus.AWAITING_APPROVAL
                step.status = StepStatus.PENDING
                return False

        decision_detail = AuthorizationEventDetails.from_mapping(
            decision.to_dict()
        )
        self._emit(
            "approval.requested",
            task_id=task.id,
            step_id=_step_id(step),
            detail=decision_detail,
        )
        outcome = self.approval_handler.request(request, decision)
        if outcome == ApprovalOutcome.APPROVED:
            self._append_approval_record(task, step, request, decision, "approved", actor="system")
            self._emit(
                "approval.granted",
                task_id=task.id,
                step_id=_step_id(step),
                detail=decision_detail,
            )
            return True
        if outcome == ApprovalOutcome.DENIED:
            step.status = StepStatus.FAILED
            step.error = "approval denied"
            self._append_approval_record(task, step, request, decision, "denied", actor="system")
            self._emit(
                "approval.denied",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail=decision_detail,
            )
            return False
        # PENDING: queue exactly one durable request, pause the task durably;
        # a later resolve_approval_request + run resumes the exact same step
        task.status = TaskStatus.AWAITING_APPROVAL
        step.status = StepStatus.PENDING
        req = self._queue_request_from_auth(task, step, request, decision)
        if self.approval_store is not None:
            candidate_id = req.approval_id
            try:
                req = self.approval_store.create_request(req) or req
            except Exception as exc:
                # No durable human decision can exist: fail closed instead of
                # creating an unresolvable AWAITING task (ADR-038).
                task.status = TaskStatus.FAILED
                step.status = StepStatus.FAILED
                step.error = "approval persistence failed; execution denied"
                task.error = step.error
                task.completed_at = utcnow()
                try:
                    self._emit(
                        "error", task_id=task.id, step_id=_step_id(step),
                        success=False,
                        detail={"error": step.error,
                                "error_type": type(exc).__name__,
                                "category": "approval_persistence"},
                    )
                except Exception:
                    pass
                return False
            if req.approval_id == candidate_id:
                self._emit("approval.queued", task_id=task.id, step_id=_step_id(step), detail={
                    "approval_id": req.approval_id,
                    "task_id": task.id,
                    "step_index": step.index,
                    "capability": req.capability,
                    "action": req.action,
                    "scope": req.scope,
                    "resource": req.resource,
                    "resource_fingerprint": req.fingerprint.get("resource_fingerprint"),
                    "resource_redacted": bool(req.fingerprint.get("resource_redacted", False)),
                })
        self._mirror_from_request(task, step, req)
        if self.goal_manager is not None and task.goal_id:
            try:
                self.goal_manager.set_blocked(task.goal_id, {
                    "type": "approval_pending",
                    "task_id": task.id,
                    "step_index": step.index,
                    "capability": request.capability,
                    "action": request.action,
                    "scope": request.scope,
                    "resource": req.resource,
                    "resource_fingerprint": req.fingerprint.get("resource_fingerprint"),
                    "resource_redacted": bool(req.fingerprint.get("resource_redacted", False)),
                    "approval_id": req.approval_id,
                    "reason": present_resource_reason(
                        decision.reason, request.resource_kind, request.resource,
                        max_chars=200,
                    ),
                }, reason="approval_pending")
            except Exception:
                pass
            try:
                self._emit("goal.approval.pending", task_id=task.id, detail={
                    "goal_id": task.goal_id,
                    "task_id": task.id,
                    "step_index": step.index,
                    "capability": request.capability,
                    "action": request.action,
                    "scope": request.scope,
                    "resource": req.resource,
                    "resource_fingerprint": req.fingerprint.get("resource_fingerprint"),
                    "resource_redacted": bool(req.fingerprint.get("resource_redacted", False)),
                    "approval_id": req.approval_id,
                })
            except Exception:
                pass
        return False

    # ---------- approval records (durable, restart-safe) ----------

    def _append_approval_record(self, task: Task, step: PlanStep, request: AuthorizationRequest,
                                decision: PolicyDecision, outcome: str, actor: str) -> None:
        """Append a bounded approval record to the task (persisted via the
        task snapshot / checkpoints). Never stores params values or secrets.
        Used for immediate handler decisions (approved/denied); PENDING uses
        the durable queue path (_mirror_from_request)."""
        presentation = present_resource(request.resource_kind, request.resource)
        task.approvals = list(task.approvals or []) + [{
            "record_id": new_id("apr"),
            "step_index": step.index,
            "outcome": outcome,
            "actor": actor,
            "created_at": utcnow(),
            "reason": present_resource_reason(
                decision.reason, request.resource_kind, request.resource,
                max_chars=200,
            ),
            "request": {
                "capability": request.capability,
                "action": request.action,
                "scope": request.scope,
                "risk": request.risk,
                "side_effects": request.side_effects,
                "resource_kind": request.resource_kind,
                **presentation.metadata(),
                "params_keys": sorted(request.params.keys()),
            },
            "fingerprint": self._authz_fingerprint(request),
        }]

    def _approved_record_for(self, task: Task, step_index: int) -> dict | None:
        """Approved mirror, cross-checked with its durable queue decision."""
        for record in reversed(list(task.approvals or [])):
            if (record.get("step_index") != step_index
                    or record.get("outcome") != "approved"):
                continue
            approval_id = record.get("approval_id")
            if approval_id and self.approval_store is not None:
                durable = self.approval_store.get_request(approval_id)
                if durable is None or durable.status != ApprovalStatus.APPROVED:
                    continue
            return record
        return None

    def _authz_fingerprint_base(self, request: AuthorizationRequest) -> dict[str, Any]:
        fp: dict[str, Any] = {
            "capability": request.capability,
            "action": request.action,
            "scope": request.scope,
            "risk": request.risk,
            "side_effects": request.side_effects,
            "resource_kind": request.resource_kind,
        }
        srp: list[str] = []
        try:
            spec = self.registry.action_spec(request.capability, request.action)
            if spec is not None:
                srp = list(getattr(spec, "security_relevant_params", []) or [])
        except Exception:
            srp = []
        fp["security_relevant_params"] = {
            key: request.params.get(key) for key in srp if key in request.params
        }
        return fp

    def _authz_fingerprint(self, request: AuthorizationRequest) -> dict[str, Any]:
        """Canonical approval fingerprint without persisting exact resource.

        Exact-change detection uses the stable resource hash. Operational
        parameters remain excluded unless the ActionSpec marks them security
        relevant. See ADR-037.
        """
        fp = self._authz_fingerprint_base(request)
        presentation = present_resource(request.resource_kind, request.resource)
        fp.update(presentation.metadata())
        return fp

    def _legacy_authz_fingerprint(self, request: AuthorizationRequest) -> dict[str, Any]:
        """Pre-ADR-037 exact-resource shape, accepted for durable compatibility."""
        fp = self._authz_fingerprint_base(request)
        fp["resource"] = request.resource
        return fp

    def _fingerprint_matches(
        self,
        stored: dict[str, Any] | None,
        request: AuthorizationRequest,
    ) -> bool:
        return stored in (
            self._authz_fingerprint(request),
            self._legacy_authz_fingerprint(request),
        )

    @staticmethod
    def _extract_resource(spec, params: dict[str, Any]) -> str | None:
        """Generic resource extraction: read the ActionSpec-declared param.

        No filesystem-specific logic here - any capability that declares
        resource_kind + resource_param gets its resource extracted the same
        way. A plan cannot redirect which param is read.
        """
        if not spec.resource_kind or not spec.resource_param:
            return None
        p = params.get(spec.resource_param)
        return p if isinstance(p, str) else None

    # ---------- execution & verification ----------

    def _execute_with_retries(
        self, task: Task, step: PlanStep, capability, spec,
        goal_run_claim=None,
    ) -> None:
        verify_failed = False
        exec_error: str | None = None
        mutating = getattr(spec, "side_effects", "read_only") == "mutating"
        lock: Any = None
        waited = False
        if not self._goal_run_allows_task(
                task, goal_run_claim, "pre-execution"):
            return
        if mutating:
            # ORDERING (ADR-021/022): authorization (live policy + approval)
            # has already succeeded in _execute_step BEFORE we reach this
            # point. The advisory mutation lock is acquired NOW, immediately
            # before the actual mutation, with BOUNDED WAITING on contention
            # (ADR-022), and released on EVERY terminal path. A lock is
            # coordination - never permission; waiting is coordination too.
            from arion.state.locks import MutationLockError, MutationLockTimeoutError

            try:
                lock, waited = self._acquire_mutation_lock(
                    task, step, spec, goal_run_claim=goal_run_claim
                )
            except MutationLockTimeoutError as exc:
                # Durable task state must stay bounded even when a resource
                # identifier inside the mixed-trust error is caller-controlled.
                step.error = sanitize_error_text(exc, max_length=500)
                return  # capability NEVER executes; no recovery record
            except MutationLockError as exc:
                # waiting disabled: immediate, durable contention failure
                # (ADR-021 semantics preserved)
                step.status = StepStatus.FAILED
                step.error = sanitize_error_text(
                    f"mutation lock contention: {exc}",
                    max_length=500,
                )
                self._emit("mutation.lock.contended", task_id=task.id,
                           step_id=_step_id(step), success=False, detail={
                               "error": step.error[:200],
                               "capability": step.capability,
                               "action": step.action,
                           })
                self._set_lock_contention_blocker(task, step, spec)
                return  # capability NEVER executes; task fails durably
            if getattr(task, "_goal_run_ownership_lost", False):
                return
            if lock is not None:
                # After a WAITED acquire, re-check LIVE authorization before
                # mutating: the approval/authorization window may have gone
                # stale while waiting. If the step pauses (fresh approval
                # queued) or is denied, release the lock and do NOT execute.
                if not self._revalidate_before_mutation(
                        task, step, spec, waited=waited,
                        goal_run_claim=goal_run_claim):
                    self._release_mutation_lock(lock, task, step)
                    return
        if not self._goal_run_allows_task(
                task, goal_run_claim, "post-lock capability dispatch"):
            if lock is not None:
                self._release_mutation_lock(lock, task, step)
            return
        heartbeat = self._start_lock_heartbeat(lock)
        try:
            self._execute_attempts(
                task, step, capability, spec, mutating,
                verify_failed, exec_error, lock=lock,
                goal_run_claim=goal_run_claim,
            )
        finally:
            self._stop_lock_heartbeat(heartbeat)
            if lock is not None:
                # Publish a known terminal mutation outcome before another
                # waiter can acquire the resource.  A stale task revision is
                # uncertain for a non-retry-safe mutation and therefore keeps
                # the existing recovery fence authoritative.
                persistence_error = None
                if step.status in (
                        StepStatus.SUCCEEDED, StepStatus.FAILED,
                        StepStatus.SKIPPED):
                    try:
                        durable = self.storage.load_task(task.id)
                        if (durable is not None
                                and durable.status in TASK_TERMINAL_STATUSES):
                            task.__dict__.update(durable.__dict__)
                        else:
                            self.storage.save_task(task)
                    except Exception as exc:
                        persistence_error = exc
                        if not getattr(spec, "retry_safe", False):
                            try:
                                self._record_recovery_required(
                                    task, step, spec,
                                    f"mutation task persistence lost: {exc}",
                                )
                            except Exception:
                                pass  # task marker/recovery fallback already attempted
                self._release_mutation_lock(lock, task, step)
                if persistence_error is not None:
                    raise persistence_error

    def _execute_attempts(
        self,
        task: Task,
        step: PlanStep,
        capability,
        spec,
        mutating: bool,
        verify_failed: bool,
        exec_error: str | None,
        lock: Any = None,
        goal_run_claim=None,
    ) -> None:
        from arion.state.locks import MutationLockError

        while step.attempts < step.max_attempts:
            # Every retry is a new external invocation and therefore needs a
            # fresh exact goal-run ownership decision.
            if not self._goal_run_allows_task(
                    task, goal_run_claim, "capability invocation"):
                return
            step.attempts += 1
            step.status = StepStatus.RUNNING
            if mutating:
                self._emit("mutation.attempted", task_id=task.id, step_id=_step_id(step), detail={
                    "capability": getattr(capability, "name", step.capability),
                    "action": step.action,
                    "resource": step.params.get(spec.resource_param) if spec.resource_param else None,
                    "attempt": step.attempts,
                })
            try:
                setattr(step, "_capability_started", True)
                raw_observation = capability.execute(
                    step.action, dict(step.params)
                )
                # ADR-035: verification and persistence receive one detached,
                # JSON-compatible, finite snapshot. For a non-retry-safe
                # mutation, a contract failure is handled by the existing
                # recovery fence because the side effect may already exist.
                observation = normalize_observation(raw_observation)
                if lock is not None:
                    self._renew_mutation_lock(lock)
            except MutationLockError as exc:
                # The side effect may already have happened, but ownership is
                # no longer valid. Never retry without reacquiring; fence it as
                # explicit recovery even when action metadata said retry-safe.
                step.status = StepStatus.FAILED
                step.result = None
                step.error = sanitize_error_text(
                    f"mutation lock ownership lost after execution: {exc}",
                    max_length=500,
                )
                self._emit(
                    "mutation.failed", task_id=task.id,
                    step_id=_step_id(step), success=False,
                    detail={"error": step.error, "attempt": step.attempts},
                )
                self._emit(
                    "mutation.requires_recovery", task_id=task.id,
                    step_id=_step_id(step), success=False,
                    detail={"error": step.error, "attempt": step.attempts},
                )
                self._record_recovery_required(task, step, spec, step.error)
                return
            except CapabilityError as exc:
                # Capability messages combine system templates with resource,
                # OS, or transport text. Keep useful diagnostics, but redact
                # credential conventions and bound them before any durable use.
                exec_error = sanitize_error_text(exc, max_length=500)
                step.result = None
                if mutating:
                    self._emit("mutation.failed", task_id=task.id, step_id=_step_id(step),
                               success=False, detail={"error": exec_error, "attempt": step.attempts})
                # retry only if the action is metadata-marked retry-safe
                if step.attempts >= step.max_attempts or not spec.retry_safe:
                    break
                self._emit(
                    "step.retrying",
                    task_id=task.id,
                    step_id=_step_id(step),
                    success=False,
                    detail={"attempt": step.attempts, "error": exec_error},
                )
                continue
            except Exception as exc:  # unexpected capability/extension failure
                step.status = StepStatus.FAILED
                step.error = sanitize_error_text(
                    f"capability raised unexpected error: {exc!r}",
                    max_length=500,
                )
                if mutating:
                    self._emit("mutation.failed", task_id=task.id, step_id=_step_id(step),
                               success=False, detail={"error": step.error, "attempt": step.attempts})
                    self._emit("mutation.requires_recovery", task_id=task.id,
                               step_id=_step_id(step), success=False,
                               detail={"error": step.error})
                    if not spec.retry_safe:
                        self._record_recovery_required(task, step, spec, step.error)
                self._emit("error", task_id=task.id, step_id=_step_id(step), success=False, detail={"error": step.error})
                return

            step.result = observation
            self._emit(
                "capability.executed",
                task_id=task.id,
                step_id=_step_id(step),
                detail={"observation_keys": sorted(observation.keys())},
            )
            self._emit("observation.recorded", task_id=task.id, step_id=_step_id(step), detail={"action": step.action})

            if self._verify(task, step):
                step.status = StepStatus.SUCCEEDED
                step.error = None
                if mutating:
                    self._emit("mutation.succeeded", task_id=task.id, step_id=_step_id(step), detail={
                        "resource": step.params.get(spec.resource_param) if spec.resource_param else None,
                        "size": observation.get("size"),
                    })
                return
            verify_failed = True
            step.error = "verification failed"
            if step.attempts < step.max_attempts and spec.retry_safe:
                self._emit(
                    "step.retrying",
                    task_id=task.id,
                    step_id=_step_id(step),
                    success=False,
                    detail={"attempt": step.attempts, "error": "verification failed"},
                )
                continue
            break

        step.status = StepStatus.FAILED
        if mutating and not spec.retry_safe and exec_error is not None:
            # Non-retry-safe mutation failed: the operation may have partially
            # applied. NEVER infer safe-to-repeat from the failure itself; the
            # task fails durably with an explainable recovery-required error
            # and recovery needs a NEW planning/authorization decision.
            step.error = f"mutation failed: {exec_error}; recovery required"
        else:
            step.error = step.error or exec_error or "step failed"
        if exec_error is not None:
            self._emit(
                "capability.executed",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail={"error": exec_error},
            )
        kind = "verification.failed" if verify_failed else "error"
        self._emit(
            kind,
            task_id=task.id,
            step_id=_step_id(step),
            success=False,
            detail={"attempt": step.attempts, "error": step.error},
        )
        # A mutating, non-retry-safe step that failed OR could not be verified
        # leaves the world in an uncertain state: durable recovery-required
        # (ADR-020). Verification failure of a mutation is a recovery case too
        # (ADR-021 Phase D): the mutation happened, the postcondition is
        # unconfirmed.
        recovery_needed = mutating and not spec.retry_safe and (exec_error is not None or verify_failed)
        if recovery_needed:
            self._emit("mutation.requires_recovery", task_id=task.id,
                       step_id=_step_id(step), success=False,
                       detail={"error": step.error, "attempt": step.attempts})
            # Durable recovery-required condition (ADR-020): the mutation may
            # have partially applied; a persistent record gates the goal until
            # an explicit recovery transition. This is a gate, NOT an
            # authorization - every new mutation still needs its own approval.
            self._record_recovery_required(task, step, spec, step.error)

    def _verify(self, task: Task, step: PlanStep) -> bool:
        policy = step.verification.policy
        result = step.result or {}
        if policy == "non_empty":
            ok = bool(result)
            detail = {"policy": policy, "result": result is not None}
        elif policy == "schema_keys":
            keys = step.verification.args.get("keys", [])
            missing = [k for k in keys if k not in result]
            ok = not missing
            detail = {"policy": policy, "missing": missing}
        elif policy == "write_verified":
            # ADR-019: confirm the intended postcondition WITHOUT another
            # mutation - the capability reported the exact byte size of the
            # write; it must match the length of the content the plan asked
            # to write. Deterministic, no filesystem re-check needed.
            expected = None
            content = step.params.get("content")
            if isinstance(content, str):
                expected = len(content.encode("utf-8"))
            ok = bool(result.get("written")) and isinstance(result.get("size"), int) and \
                result["size"] == expected
            detail = {"policy": policy, "expected_size": expected,
                      "reported_size": result.get("size")}
        elif policy == "append_verified":
            # ADR-020: confirm the intended postcondition WITHOUT another
            # mutation - prior_size + exactly the appended bytes must equal the
            # reported new size, and the appended bytes must match the planned
            # content length. Deterministic, no filesystem re-check needed.
            expected = None
            content = step.params.get("content")
            if isinstance(content, str):
                expected = len(content.encode("utf-8"))
            prior = result.get("prior_size")
            appended = result.get("appended_bytes")
            ok = (
                bool(result.get("appended"))
                and isinstance(prior, int)
                and isinstance(appended, int)
                and isinstance(result.get("size"), int)
                and appended == expected
                and result["size"] == prior + appended
            )
            detail = {"policy": policy, "expected_appended": expected,
                      "reported_appended": appended, "prior_size": prior,
                      "reported_size": result.get("size")}
        else:
            ok = False
            detail = {"policy": policy, "error": "unknown verification policy"}
        self._emit(
            "verification.passed" if ok else "verification.failed",
            task_id=task.id,
            step_id=_step_id(step),
            success=ok,
            detail=detail,
        )
        return ok

    def _checkpoint(self, task: Task, reason: str) -> None:
        ckpt = Checkpoint(
            task_id=task.id,
            status=task.status.value,
            step_index=task.current_step,
            snapshot=task.to_dict(),
            reason=reason,
        )
        self.storage.save_checkpoint(ckpt)
        # ADR-036: the newest full checkpoint is already durable. Historical
        # pruning is best effort; failure safely leaves extra snapshots and
        # must never fail completed work or weaken recovery.
        prune = getattr(self.storage, "prune_checkpoints", None)
        if callable(prune):
            try:
                prune(task.id, keep_last=DEFAULT_CHECKPOINT_RETENTION)
            except Exception:
                pass
        self._emit(
            "checkpoint.persisted",
            task_id=task.id,
            detail={"checkpoint_id": ckpt.id, "step_index": ckpt.step_index, "reason": reason},
        )

    # ---------- memory integration (ADR-012) ----------

    def _build_planning_context(self, task: Task):
        """Retrieve relevant, bounded memory for the planner (or None if memory
        is disabled). Memory informs planning; it never authorizes."""
        if self.memory is None:
            return None
        try:
            from arion.memory.models import ContextBudget
            from arion.memory.retrieval import MemoryRetriever, build_planning_context

            retriever = MemoryRetriever(self.memory)
            ctx = build_planning_context(
                retriever, task.description, ContextBudget(),
                capabilities=set(self._planner_requirements(task.description)
                                 or []))
            self._emit(
                "memory.retrieval.completed",
                task_id=task.id,
                detail={
                    "episodes": len(ctx.episodes),
                    "reflections": len(ctx.reflections),
                    "tags": ctx.all_tags()[:20],
                    "budget": {"max_episodes": ctx.budget.max_episodes, "max_chars": ctx.budget.max_chars},
                },
            )
            self._emit(
                "planning.context.created",
                task_id=task.id,
                detail={
                    "episodes": len(ctx.episodes),
                    "reflections": len(ctx.reflections),
                    "guidance": len(ctx.guidance),
                    "provenance": ctx.provenance,
                },
            )
            # planning.memory.influence: record which memories influenced this
            # planning decision (IDs/counts/categories - never raw contents).
            self._emit(
                "planning.memory.influence",
                task_id=task.id,
                detail={
                    "episode_ids": ctx.provenance.get("episode_ids", []),
                    "reflection_ids": ctx.provenance.get("reflection_ids", []),
                    "guidance_ids": ctx.provenance.get("guidance_ids", []),
                    "memory_count": len(ctx.episodes) + len(ctx.reflections),
                    "guidance_categories": sorted({g.category for g in ctx.guidance}),
                    "guidance_count": len(ctx.guidance),
                    "deterministic": True,
                },
            )

            # World state (ADR-015): expose current environment facts (bounded)
            # so the planner relies on the CURRENT world, not stale memory.
            if self.world_monitor is not None:
                try:
                    facts = self.world_monitor.store.list_environment_facts(limit=50)
                    ctx.environment = facts[:20]
                except Exception:
                    pass

            # Previous goal plan history (ADR-016): bounded, immutable plan
            # versions so replanning consumes prior goal-plan history.
            if self.goal_manager is not None and task.goal_id:
                try:
                    ctx.plan_history = self.goal_manager.plan_history(task.goal_id)[-5:]
                except Exception:
                    pass

            # Mutation-recovery advisory (ADR-020): bounded, informational
            # records telling the planner that a previous mutation failed /
            # recovery was required / the action is not retry-safe / fresh
            # authorization is needed. ADVISORY ONLY - it can never authorize,
            # clear, or bypass recovery enforcement (the engine re-checks the
            # durable recovery registry and policy independently).
            if self.recovery_store is not None and task.goal_id:
                try:
                    recs = [
                        r for r in self.recovery_store.list_recoveries(goal_id=task.goal_id)
                    ][-10:]
                    ctx.recovery = [r.to_dict() for r in recs]
                    if recs:
                        self._emit("planning.recovery.advisory", task_id=task.id, detail={
                            "goal_id": task.goal_id,
                            "recovery_ids": [r.recovery_id for r in recs],
                            "statuses": sorted({r.status.value for r in recs}),
                            "count": len(recs),
                            "advisory_only": True,
                        })
                except Exception:
                    pass

            # Strategy selection (ADR-015/016): deterministic, informational.
            # previous_strategies (from the goal's immutable plan history) lets
            # the selector escalate instead of blindly repeating a strategy
            # that already failed (ADR-016). outcome_history (durable
            # strategy_outcomes, bounded) feeds the post-rule preference
            # layer (ADR-015 addendum Phase B). It can never authorize.
            if self.strategy_selector is not None:
                try:
                    beliefs = self.cognition.cognition.list_beliefs(limit=100) if self.cognition else []
                    env_state = self.world_monitor.current_state() if self.world_monitor else {}
                    previous_strategies: list[str] = []
                    if self.goal_manager is not None and task.goal_id:
                        previous_strategies = [
                            p.get("strategy", "") for p in self.goal_manager.plan_history(task.goal_id)
                        ]
                    outcome_history: list = []
                    if self.goal_manager is not None:
                        try:
                            outcome_history = self.goal_manager.strategy_outcomes(limit=50)
                        except Exception:
                            outcome_history = []
                    strategy = self.strategy_selector.select(
                        task.description, beliefs, env_state, ctx.guidance,
                        previous_strategies=[s for s in previous_strategies if s],
                        outcome_history=outcome_history,
                    )
                    ctx.strategy = strategy
                    self._last_strategy = strategy
                    self._emit(
                        "strategy.selected",
                        task_id=task.id,
                        detail={
                            "strategy_id": strategy.strategy_id,
                            "name": strategy.name,
                            "constraints": strategy.constraints,
                            "provenance": strategy.provenance,
                        },
                    )
                except Exception:
                    pass
            return ctx
        except Exception:
            # Memory must never break planning.
            return None

    def _record_memory(self, task: Task) -> None:
        """Record a structured episode + reflection for a terminal task.

        IDEMPOTENT (ADR-013 addendum): exactly one episode per task and ONE
        REFLECTION PER EPISODE, both enforced DURABLY at the storage layer
        (task-keyed and episode-keyed unique indexes; first-writer-wins
        claims inside BEGIN IMMEDIATE), so concurrent threads, concurrent
        workers/processes, restarts and crash-retries can duplicate
        neither. If an episode already exists for this task, the pass
        reuses it; an existing reflection means the whole pass already
        completed, so nothing is duplicated; a recorded-but-unreflected
        episode (crash mid-learning) resumes from reflection onward - a
        racing worker that loses the reflection claim adopts the canonical
        reflection instead of creating a second one. Runs best-effort:
        memory failure never changes task outcome. Stores structured
        summaries only - never secrets, credentials, raw prompts, or raw
        model responses.
        """
        if self.memory is None:
            return
        try:
            from arion.memory.lifecycle import build_episode_from_task
            from arion.memory.reflector import DeterministicReflector

            existing = None
            try:
                existing = self.memory.get_episode_by_task(task.id)
            except Exception:
                existing = None
            if existing is not None and existing.reflection_id:
                return  # fully learned already: idempotent no-op

            events = []
            try:
                events = self.storage.list_events(task.id)
            except Exception:
                events = []
            episode = build_episode_from_task(task, events, registry=self.registry)
            if existing is not None:
                # same task, same episode id: refresh content in place
                # (outcome/tags may have been finalized after the first
                # partial pass) - never mint a second episode for a task.
                episode.episode_id = existing.episode_id
                episode.created_at = existing.created_at
            try:
                self.memory.set_episode_lifecycle(
                    episode.episode_id, "recorded")
            except Exception:
                pass
            # DURABLE EPISODE CLAIM (one episode per task, ADR-013
            # addendum): record_episode returns the CANONICAL durable
            # episode for the task - first writer wins the identity, a
            # racing minted id is never stored, and a re-record preserves
            # the durable reflection link. Adopt the canonical identity so
            # every subsequent operation (reflection claim, link,
            # lifecycle) targets the durable row.
            minted_id = episode.episode_id
            stored_episode = self.memory.record_episode(episode)
            if stored_episode is not None:
                episode = stored_episode
            self._emit(
                "memory.episode.recorded",
                task_id=task.id,
                detail={
                    "episode_id": episode.episode_id,
                    "outcome": episode.outcome,
                    "tags": episode.tags[:20],
                    "importance": round(episode.importance, 2),
                    "reused": existing is not None
                    or episode.episode_id != minted_id,
                },
            )

            # Reflect: prefer the configured reflector (may be a ModelReflector);
            # if it fails or produces something invalid, fall back to the
            # deterministic reflector so the loop stays offline-capable.
            reflection = None
            try:
                if self.reflector is not None:
                    reflection = self.reflector.reflect(episode)
            except Exception as exc:
                summary = summarize_error(
                    exc,
                    source=ErrorSource.EXTERNAL,
                    category="reflection_validation",
                )
                detail = summary.to_event_detail()
                detail["fallback"] = "deterministic"
                self._emit(
                    "reflection.validation.failed",
                    task_id=task.id,
                    success=False,
                    detail=detail,
                )
            if reflection is None:
                reflection = DeterministicReflector().reflect(episode)
            # DURABLE REFLECTION CLAIM (one reflection per episode, ADR-013
            # addendum): the store keeps the FIRST reflection inserted for
            # the episode; a concurrent worker that already reflected wins
            # and this pass ADOPTS the canonical reflection instead of
            # duplicating it (idempotent across threads, workers and
            # restarts).
            created_reflection = True
            try:
                canonical = self.memory.record_reflection(reflection)
                if canonical is not None and canonical.reflection_id != reflection.reflection_id:
                    reflection = canonical  # another worker's reflection won
                    created_reflection = False
            except Exception:
                canonical = None  # best-effort memory: link our own reflection
            try:
                self.memory.link_reflection(episode.episode_id, reflection.reflection_id)
            except Exception:
                pass
            try:
                self.memory.set_episode_lifecycle(
                    episode.episode_id, "reflected")
            except Exception:
                pass
            if created_reflection:
                self._emit(
                    "reflection.created",
                    task_id=task.id,
                    detail={"reflection_id": reflection.reflection_id, "episode_id": episode.episode_id},
                )

            # Cognitive state: derive + store beliefs (semantic/procedural)
            # with full provenance (ADR-014). Informational only.
            self._derive_beliefs(episode, reflection)

            # Consolidation: deterministic duplicate/lesson merging (never deletes).
            self._consolidate(task.id)
            try:
                self.memory.set_episode_lifecycle(
                    episode.episode_id, "consolidated")
            except Exception:
                pass
        except Exception:
            # Memory is best-effort; never break the task lifecycle.
            pass

    def learn_from_terminal_tasks(self, limit: int = 200) -> int:
        """ADR-013 addendum: catch-up learning after restart.

        Records an episode (then reflection + consolidation) for every
        TERMINAL task that has none - recovering experience that was lost
        when a process crashed between the durable terminal task save and
        the episode write. Idempotent: tasks that already have a fully
        learned episode are skipped; a second pass records nothing new.
        Bounded (limit tasks per pass), read-mostly, best-effort - never
        touches scheduler or execution authority. Returns the number of
        tasks newly learned in this pass.
        """
        if self.memory is None:
            return 0
        from arion.state.models import TaskStatus

        terminal = [t for t in self.storage.list_tasks()
                    if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)]
        recorded = 0
        skipped = 0
        for task in terminal[: int(limit)]:
            try:
                existing = self.memory.get_episode_by_task(task.id)
            except Exception:
                existing = None
            if existing is not None and existing.reflection_id:
                skipped += 1
                continue
            self._record_memory(task)
            try:
                after = self.memory.get_episode_by_task(task.id)
            except Exception:
                after = None
            if after is not None:
                recorded += 1
            else:
                skipped += 1
        try:
            self._emit(
                "memory.learning.catchup",
                task_id=None,
                detail={"processed": len(terminal[: int(limit)]),
                        "recorded": recorded, "skipped": skipped,
                        "limit": int(limit)},
            )
        except Exception:
            pass
        return recorded

    def _derive_beliefs(self, episode, reflection) -> None:
        """Derive + store cognitive beliefs from the latest experience.

        Every belief carries provenance (episode/reflection/guidance ids),
        confidence, timestamps, and source. Routed through the SAME
        :meth:`CognitiveState.persist_belief` funnel as every other belief
        writer (ADR-014 addendum): the identity/confidence/version/super-
        session decision runs atomically inside the storage layer, so
        repeated learning and concurrent workers cannot leave two ACTIVE
        revisions for one logical belief, and a higher-confidence revision
        deterministically supersedes the prior active row.

        ``belief.derived`` is emitted ONLY for beliefs this call actually
        created (``created=True``); concurrent losers and equal/lower-
        confidence observations adopt the canonical row and emit nothing.

        Best-effort: cognitive state must never break the task loop.
        Informational only.
        """
        if self.cognition is None or self.belief_deriver is None:
            return
        try:
            from arion.memory.guidance import DeterministicMemoryGuidance

            guidance = DeterministicMemoryGuidance().build([episode], [reflection])
            beliefs = self.belief_deriver.derive([episode], [reflection], guidance)
            for b in beliefs:
                # The facade routes through the storage-layer transactional
                # claim and mutates `b` in place to the CANONICAL active
                # belief's identity/version. created=True only when this
                # call inserted a new revision (concurrent losers and
                # equal/lower-confidence observations adopt the canonical
                # row and emit nothing).
                created = self.cognition.persist_belief(b)
                if not created:
                    continue
                self._emit(
                    "belief.derived",
                    task_id=episode.task_id,
                    detail={
                        "belief_id": b.belief_id,
                        "category": b.category,
                        "confidence": round(b.confidence, 3),
                        "importance": round(b.importance, 3),
                        "version": b.version,
                        "provenance": b.provenance,
                        "source": b.source,
                    },
                )
        except Exception:
            pass

    def _consolidate(self, task_id: str) -> None:
        """Run deterministic consolidation; emit memory.consolidated per record.

        ADR-013 addendum: the consolidator returns ONLY records this invocation
        actually created - a worker that merely ADOPTS a concurrent peer's
        canonical consolidation (same source set already persisted durably)
        returns nothing, so `memory.consolidated` is emitted ONLY for real
        creations and a racing learner can never emit a duplicate event.
        """
        try:
            from arion.memory.consolidation import MemoryConsolidator

            records = MemoryConsolidator(self.memory).consolidate(limit=100)
            for record in records:
                self._emit(
                    "memory.consolidated",
                    task_id=task_id,
                    detail={
                        "consolidation_id": record.consolidation_id,
                        "source_episode_ids": record.source_episode_ids,
                        "category": record.category,
                        "count": record.count,
                        "importance": record.importance,
                    },
                )
        except Exception:
            pass

    def _emit(
        self,
        kind: str,
        task_id: str | None,
        step_id: str | None = None,
        success: bool = True,
        detail: dict[str, Any] | EventDetails | None = None,
    ) -> None:
        self.events.emit(AuditEvent(
            kind=kind,
            task_id=task_id,
            step_id=step_id,
            success=success,
            detail=detail if detail is not None else {},
        ))


def _step_id(step: PlanStep) -> str:
    return f"step_{step.index}"
