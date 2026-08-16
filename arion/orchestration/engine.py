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
from typing import Any

from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.state.approvals import ApprovalError, ApprovalRequest, ApprovalStatus, ApprovalStore
from arion.state.recovery import MutationRecovery, RecoveryError, RecoveryStatus
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.intelligence.plan_schema import PlanValidationError
from arion.intelligence.plan_validator import topo_sort_steps
from arion.intelligence.planner import Planner
from arion.intelligence.router import ModelRouter
from arion.observability.events import AuditEvent, EventLogger
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
    Task,
    TaskStatus,
    new_id,
    utcnow,
)
from arion.state.store import Storage


def _iso_plus(iso: str, seconds: float) -> str:
    """ISO timestamp + seconds (lock wait deadlines; deterministic clock)."""
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


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
    ):
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
        import threading as _threading

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
        # This engine's durable scheduler identity: on (re)start, QUEUED rows
        # owned by a DIFFERENT (presumed dead) scheduler are abandoned and
        # stale RUNNING leases are reclaimed, so no immortal RUNNING worker
        # and no dead process's queue can ever be mistaken for live work.
        self.scheduler_id = new_id("sched")
        if self.scheduler_registry is not None and scheduler_reclaim_on_start:
            try:
                self.scheduler_registry.reclaim_stale(now=self._lock_now())
                self.scheduler_registry.abandon_foreign_queued(self.scheduler_id)
            except Exception:
                pass
        # The goal manager's lock_contention recheck resolves via the engine's
        # live lock store (the lock store is the only lock authority).
        if self.goal_manager is not None:
            try:
                self.goal_manager.lock_contention_resolver = self._lock_contention_resolver
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

    def _record_recovery_required(self, task: Task, step: PlanStep, spec,
                                  reason: str) -> MutationRecovery | None:
        """Durably record that a non-retry-safe mutation failed (ADR-020).

        Idempotent per (task_id, step_index): a REQUIRED record is never
        duplicated. Also gates the goal durably with a `recovery_required`
        blocker so no fresh plan can proceed until the operator explicitly
        acknowledges the recovery. The record carries bounded metadata only.
        """
        if self.recovery_store is None:
            return None
        existing = [
            r for r in self.recovery_store.list_recoveries(task_id=task.id)
            if r.step_index == step.index and r.status == RecoveryStatus.REQUIRED
        ]
        rec = existing[0] if existing else None
        if rec is None:
            resource = step.params.get(spec.resource_param) if getattr(spec, "resource_param", None) else None
            rec = MutationRecovery(
                recovery_id=new_id("recovery"),
                task_id=task.id,
                goal_id=task.goal_id,
                step_index=step.index,
                capability=step.capability,
                action=step.action,
                resource=resource if isinstance(resource, str) else None,
                reason=(reason or "mutation failed; recovery required")[:500],
            )
            self.recovery_store.create_recovery(rec)
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
        # explicitly acknowledged. Idempotent by blocker key.
        if self.goal_manager is not None and task.goal_id:
            try:
                self.goal_manager.set_blocked(task.goal_id, {
                    "type": "recovery_required",
                    "task_id": task.id,
                    "step_index": step.index,
                    "capability": step.capability,
                    "action": step.action,
                    "resource": step.params.get(spec.resource_param) if getattr(spec, "resource_param", None) else None,
                    "recovery_id": rec.recovery_id,
                    "reason": "mutation failed; recovery required (non-retry-safe)",
                }, reason="recovery_required")
            except Exception:
                pass
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
        self.recovery_store.update_recovery(rec)
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

    def _acquire_mutation_lock(self, task: Task, step: PlanStep, spec) -> Any | None:
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
            now = self._lock_now()
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

    def _revalidate_before_mutation(self, task: Task, step: PlanStep, spec,
                                    waited: bool) -> bool:
        """Re-check LIVE authorization immediately before mutating (ADR-022).

        Only meaningful after the task actually WAITED for the lock: the
        approval/authorization window may have gone stale while waiting. We
        rebuild the authorization request from the CURRENT ActionSpec and
        policy, and re-run the approval seam (which compares the canonical
        fingerprint and forces a FRESH approval when anything security-
        relevant changed). Returns True when the step may proceed; False when
        the step was paused (fresh approval queued) or denied - the caller
        must NOT execute the capability."""
        if not waited:
            return True  # no contention: the single authz check already ran
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
            detail={
                **decision.to_dict(),
                "params": request.params,
                "actor": request.actor.id,
                "revalidated_after_lock_wait": True,
            },
        )
        if decision.outcome == PolicyOutcome.DENY:
            step.status = StepStatus.FAILED
            step.error = decision.reason
            self._emit("permission.denied", task_id=task.id, step_id=_step_id(step),
                       success=False, detail=decision.to_dict())
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
        the latest retry - set_blocked alone is idempotent by key and would
        keep stale metadata."""
        if not goal_id or self.goal_manager is None:
            return
        try:
            gm = self.goal_manager
            goal = gm.get_goal(goal_id)
            if goal is None:
                return
            blockers = list(goal.blockers or [])
            idx = next((i for i, b in enumerate(blockers)
                        if (b.get("key") or b.get("type")) == "lock_contention"), None)
            if idx is None:
                gm.set_blocked(goal_id, {"type": "lock_contention", **fields},
                               reason="lock_contention")
                return
            kept = dict(blockers[idx])
            blockers[idx] = {"key": "lock_contention", "type": "lock_contention",
                             **fields, "added_at": kept.get("added_at", utcnow())}
            goal.blockers = blockers
            goal.updated_at = utcnow()
            self.storage.save_goal(goal)
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

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop the in-process step scheduler (ADR-024/025): no new work is
        accepted, queued (not-running) work is cancelled, bounded active
        workers are joined. After shutdown returns, no worker thread may
        continue mutating. Idempotent.

        The durable registry is mirrored: QUEUED rows of THIS scheduler are
        marked CANCELLED (a cancelled row can never run). Rows already
        RUNNING were drained by the join and reach their terminal state via
        the worker's own mirror; stale rows are reclaimed on the next
        engine construction."""
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

    # ---------- approval expiry (ADR-019) ----------

    def expire_stale_approvals(self, now: str | None = None) -> list[str]:
        """Mark stale PENDING approval requests as EXPIRED (durable, idempotent).

        A request expires when it has been pending longer than the engine's
        configured TTL (approval_ttl_seconds). `now` is injectable for tests
        (ISO-8601); defaults to the real clock. Returns the approval ids that
        were newly expired in this call. Idempotent: already-EXPIRED requests
        are never touched again, so no duplicate events. Expired requests
        remain fully auditable - nothing is deleted.

        An expired approval fails the awaiting task durably with an explicit
        'approval expired; recovery requires new authorization' error and
        clears the goal's approval_pending blocker, so a later run_goal
        replans and requests FRESH authorization. A stale approval can never
        cause a mutation.
        """
        if now is None:
            from arion.state.models import utcnow

            now = utcnow()
        if self.approval_store is None:
            return []
        ttl = self.approval_ttl_seconds
        if ttl is None:
            return []  # expiration disabled

        from datetime import datetime, timedelta, timezone

        try:
            now_dt = datetime.fromisoformat(now)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return []
        cutoff = now_dt - timedelta(seconds=max(0.0, float(ttl)))
        cutoff_iso = cutoff.isoformat()

        expired: list[str] = []
        for req in self.approval_store.list_requests(status=ApprovalStatus.PENDING.value):
            try:
                created_dt = datetime.fromisoformat(req.created_at)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if created_dt >= cutoff:
                continue
            req.status = ApprovalStatus.EXPIRED
            req.expired_at = now
            req.updated_at = now
            self.approval_store.update_request(req)
            expired.append(req.approval_id)
            self._emit(
                "approval.expired",
                task_id=req.task_id,
                step_id=f"{req.task_id}:{req.step_index}",
                success=False,
                detail={
                    "approval_id": req.approval_id,
                    "task_id": req.task_id,
                    "step_index": req.step_index,
                    "capability": req.capability,
                    "action": req.action,
                    "resource": req.resource,
                    "reason": "pending approval exceeded TTL",
                },
            )
            self._fail_awaiting_task_on_expiry(req)
        return expired

    def _fail_awaiting_task_on_expiry(self, req: "ApprovalRequest") -> None:
        """Fail the task that was waiting on an expired approval, durably.

        Mirrors the DENIED path but with an explicit expiry reason: the
        mutation is NOT executed, the task becomes terminally FAILED with an
        explainable error, and the goal's approval_pending blocker is cleared
        so the next run_goal replans and requests fresh authorization.
        """
        task = self.storage.load_task(req.task_id)
        if task is None or task.status != TaskStatus.AWAITING_APPROVAL:
            return
        step = task.active_step
        if step is not None and step.index == req.step_index:
            step.status = StepStatus.FAILED
            step.error = "approval expired; recovery requires new authorization"
        task.status = TaskStatus.FAILED
        task.error = "approval expired; recovery requires new authorization"
        task.completed_at = utcnow()
        if self.goal_manager is not None and task.goal_id:
            try:
                self.goal_manager.clear_blocker(task.goal_id, "approval_pending",
                                                reason="approval_expired")
            except Exception:
                pass
        self._emit("task.failed", task_id=task.id, detail={
            "step_index": req.step_index, "error": task.error,
        })
        self._emit("goal.approval.expired", task_id=task.id, detail={
            "goal_id": task.goal_id, "task_id": task.id, "step_index": req.step_index,
            "approval_id": req.approval_id,
        })
        self._record_memory(task)
        task.updated_at = utcnow()
        self.storage.save_task(task)

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
        """Long-horizon goal loop (ADR-016/017):

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
            result, _goal = gm.evaluate(goal_id)
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
                        pending = self.run_task(pending.id)
                        if pending.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL):
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
                gm.complete_goal(goal_id, reason="all_work_complete")
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
                    gm.fail_goal(goal_id, reason="max_replans_exceeded")
                    return gm.get_goal(goal_id)
                if self._block_on_missing_capability(goal_id, gm):
                    return gm.get_goal(goal_id)
                if self._block_on_open_recovery(goal_id, gm):
                    return gm.get_goal(goal_id)
                if self._block_on_lock_contention(goal_id, gm):
                    return gm.get_goal(goal_id)
                task = self._plan_for_goal(goal_id, replan_reason=result.evidence.get("reason"))
                if task is not None:
                    task = self.run_task(task.id)
                    if task.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL):
                        return gm.get_goal(goal_id)  # caller decides next step
                continue

            # continue / initial_plan: resume a pending task for the latest
            # plan version if one exists (replay safety), else plan + execute.
            pending = gm.pending_task(goal_id)
            if pending is not None:
                pending = self.run_task(pending.id)
                if pending.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL):
                    return gm.get_goal(goal_id)
                continue
            if self._block_on_missing_capability(goal_id, gm):
                return gm.get_goal(goal_id)
            if self._block_on_open_recovery(goal_id, gm):
                return gm.get_goal(goal_id)
            if self._block_on_lock_contention(goal_id, gm):
                return gm.get_goal(goal_id)
            task = self._plan_for_goal(goal_id, replan_reason=None)
            if task is not None:
                task = self.run_task(task.id)
                if task.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL):
                    return gm.get_goal(goal_id)

    def _block_on_open_recovery(self, goal_id: str, gm) -> bool:
        """Gate the goal before planning while a mutation recovery is open (ADR-020).

        A failed non-retry-safe mutation leaves a durable REQUIRED recovery
        record; while ANY such record exists for the goal, fresh planning is
        durably blocked (recovery_required blocker) until an operator
        explicitly acknowledges the recovery. Recovery is a GATE, never an
        authorization - a fresh plan still needs its own approval.
        """
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
            self._resolve_request(req, outcome, actor)
            return self.storage.load_task(task_id)
        # No durable queue (legacy wiring): fall back to the in-memory mirror.
        recs = [r for r in task.approvals
                if r.get("step_index") == step.index and r.get("outcome") == "pending"]
        if not recs:
            raise GoalStateError(f"task {task_id} has no pending approval for step {step.index}")
        return self._resolve_legacy(task, recs[-1], outcome, actor)

    def resolve_approval_request(self, approval_id: str, outcome: "ApprovalOutcome",
                                 actor: str = "approver") -> "ApprovalRequest":
        """Resolve a durable approval-queue record by id (ADR-018).

        APPROVED: the record is marked approved, the goal unblocks and the
        next run resumes the EXACT pending step (no replan, no re-request).

        DENIED: the record is durably denied; the step + task fail with
        reason 'approval denied'.

        Fail closed (ApprovalError): unknown id, already-resolved, or the
        request's task/step no longer awaits this approval.
        """
        if outcome not in (ApprovalOutcome.APPROVED, ApprovalOutcome.DENIED):
            raise ValueError(f"resolve_approval_request accepts APPROVED or DENIED, got {outcome!r}")
        if self.approval_store is None:
            raise ApprovalError("approval queue is not available on this engine")
        req = self.approval_store.get_request(approval_id)
        if req is None:
            raise ApprovalError(f"unknown approval id: {approval_id}")
        if req.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"approval {approval_id} is already resolved ({req.status.value})")
        task = self.storage.load_task(req.task_id)
        if task is None:
            raise ApprovalError(f"approval {approval_id} references a missing task {req.task_id}")
        step = task.active_step
        if task.status != TaskStatus.AWAITING_APPROVAL or step is None or step.index != req.step_index:
            raise ApprovalError(
                f"approval {approval_id} no longer matches the task's pending step "
                f"(task={req.task_id} step={req.step_index})"
            )
        self._resolve_request(req, outcome, actor)
        return self.approval_store.get_request(approval_id)

    def _resolve_request(self, req: "ApprovalRequest", outcome: "ApprovalOutcome",
                         actor: str) -> None:
        """Resolve a durable queue record + the mirrored task state."""
        task = self.storage.load_task(req.task_id)
        step = task.active_step
        req.status = ApprovalStatus.APPROVED if outcome == ApprovalOutcome.APPROVED else ApprovalStatus.DENIED
        req.decision_actor = actor
        req.decided_at = utcnow()
        self.approval_store.update_request(req)

        mirror = [r for r in (task.approvals or []) if r.get("approval_id") == req.approval_id]
        rec = mirror[-1] if mirror else self._mirror_from_request(task, step, req)
        rec["outcome"] = req.status.value
        rec["resolved_by"] = actor
        rec["resolved_at"] = req.decided_at

        gm = self.goal_manager
        if outcome == ApprovalOutcome.APPROVED:
            task.status = TaskStatus.RUNNING  # resumable; not terminal
            if gm is not None and task.goal_id:
                try:
                    gm.clear_blocker(task.goal_id, "approval_pending", reason="approval_granted")
                except Exception:
                    pass
            self._emit("approval.granted", task_id=task.id, step_id=_step_id(step), detail={
                "scope": req.scope, "resource": req.resource, "approval_id": req.approval_id,
            })
            self._emit("goal.approval.granted", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id, "step_index": step.index,
                "capability": req.capability, "action": req.action, "scope": req.scope,
                "actor": actor, "approval_id": req.approval_id,
            })
        else:
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
                       success=False, detail={
                           "scope": req.scope, "resource": req.resource,
                           "reason": "approval denied", "approval_id": req.approval_id,
                       })
            self._emit("goal.approval.denied", task_id=task.id, detail={
                "goal_id": task.goal_id, "task_id": task.id, "step_index": step.index,
                "actor": actor, "reason": "approval denied", "approval_id": req.approval_id,
            })
            self._emit("task.failed", task_id=task.id, detail={
                "step_index": step.index, "error": "approval denied",
            })
            self._record_memory(task)
        task.updated_at = utcnow()
        self.storage.save_task(task)

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

    def _pending_queue_request(self, task_id: str, step_index: int, fingerprint: dict) -> "ApprovalRequest | None":
        """Dedupe: an existing PENDING request for the same task/step/authz
        fingerprint. Repeated pauses never create duplicate queue records."""
        for req in self.approval_store.list_requests(status=ApprovalStatus.PENDING.value):
            if req.task_id == task_id and req.step_index == step_index and req.fingerprint == fingerprint:
                return req
        return None

    def _queue_request_from_auth(self, task: Task, step: PlanStep, request: AuthorizationRequest,
                                 decision: PolicyDecision) -> "ApprovalRequest":
        fp = self._authz_fingerprint(request)
        summary = (
            f"{request.capability}/{request.action} "
            f"{('on ' + str(request.resource)) if request.resource else ''} "
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
            resource=request.resource,
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
                "params_keys": req.params_keys,
            },
            "fingerprint": req.fingerprint,
        }
        task.approvals = list(task.approvals or []) + [rec]
        return rec

    def _plan_for_goal(self, goal_id: str, replan_reason: str | None = None) -> Task | None:
        """Create + plan a task for a goal (records an immutable plan version)."""
        gm = self.goal_manager
        goal = gm.get_goal(goal_id)
        if goal is None:
            return None
        task = self.create_task(goal)
        self._plan(task, replan_reason=replan_reason)
        return task

    def run_task(self, task_id: str) -> Task:
        """Resume-or-start a task and drive it to a stopping point.

        Stopping points: COMPLETED, FAILED, or AWAITING_APPROVAL (the task is
        checkpointed and returned so an approval interface can act; calling
        run_task again after approval resumes from the exact same step).

        Survives restarts: if a checkpoint exists, state is restored and the
        task resumes from the checkpointed step instead of starting over.
        """
        task = self.storage.load_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")

        checkpoint = self.storage.latest_checkpoint(task_id)
        restored = None
        if checkpoint is not None:
            restored = Task.from_dict(checkpoint.snapshot)
            if checkpoint.created_at >= task.updated_at:
                # the checkpoint is the freshest state (normal crash recovery)
                task = restored
            # else: an out-of-band update (e.g. resolve_approval) wrote a
            # NEWER task row - the task row is authoritative.
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

        # Already terminal (e.g. completed before a restart): return as-is.
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return task

        # Approval still pending (durable queue record exists): stop cleanly
        # and idempotently - never re-execute, re-request or re-queue the
        # awaiting step (ADR-018). resolve_approval(APPROVED) flips the task
        # to RUNNING, which is how the exact-step resume proceeds.
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return task

        if not task.steps:
            task = self._plan(task)
            if task.status == TaskStatus.FAILED:
                self._record_memory(task)
                return task
        elif checkpoint is None:
            # Dependency-aware execution: for hand-built plans, validate and
            # order steps so every step runs only after its dependencies.
            try:
                task.steps = topo_sort_steps(task.steps)
            except PlanValidationError as exc:
                task.status = TaskStatus.FAILED
                task.error = f"planning failed: {exc}"
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._emit("error", task_id=task.id, success=False, detail={
                    "error": task.error,
                    "error_type": type(exc).__name__,
                    "category": getattr(exc, "category", "unknown"),
                })
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
            # emit step.skipped provenance for skipped steps we walk past
            for st in task.steps:
                if st.status == StepStatus.SKIPPED and (task.id, st.index) not in self._skipped_emitted:
                    self._skipped_emitted.add((task.id, st.index))
                    self._emit("step.skipped", task_id=task.id, step_id=_step_id(st), detail={
                        "reason": st.skipped_reason or "skipped", "guidance": st.guidance[:5],
                    })

            pending = [i for i, st in enumerate(task.steps) if st.status == StepStatus.PENDING]
            if not pending:
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
            for i in dispatch:
                self._enqueue_step(task, task.steps[i])
            self.scheduler.run_until_done()

            self.storage.save_task(task)

            cstep = task.steps[cursor]
            if cstep.status == StepStatus.PENDING and task.status == TaskStatus.AWAITING_APPROVAL:
                # cursor paused on approval: durable stop, exact-step resume
                self._checkpoint(task, reason="awaiting approval")
                self.storage.save_task(task)
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
        """Drive MULTIPLE tasks through the ONE bounded scheduler (ADR-025).

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
        while tasks:
            round_no += 1
            order = list(tasks.keys())
            order = order[round_no % len(order):] + order[:round_no % len(order)]
            per_task_cap = max(1, -(-self.max_concurrency // max(1, len(order))))
            # ---------------- round: compute candidates / parks ----------------
            plan: dict[str, dict[str, Any]] = {}
            for tid in order:
                task = tasks[tid]
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                   TaskStatus.AWAITING_APPROVAL):
                    results[tid] = task
                    continue
                if not task.steps:
                    task = self._plan(task)
                    if task.status == TaskStatus.FAILED:
                        results[tid] = task
                        continue
                pending = [i for i, st in enumerate(task.steps)
                           if st.status == StepStatus.PENDING]
                if not pending:
                    self._complete_task_shared(task)
                    results[tid] = task
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
                plan[tid] = {"task": task, "candidates": candidates,
                             "parked": parked, "cursor": cursor}
            # ---------------- park (durable waiter, no worker) ----------------
            for tid, info in plan.items():
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
            for tid, i in admitted:
                task = plan[tid]["task"]
                step = task.steps[i]
                self._enqueue_step(task, step)
            if admitted:
                self.scheduler.run_until_done()
            # ---------------- post-round per task -----------------------------
            for tid, info in plan.items():
                task = info["task"]
                self.storage.save_task(task)
                cstep = task.steps[info["cursor"]]
                if cstep.status == StepStatus.PENDING and task.status == TaskStatus.AWAITING_APPROVAL:
                    self._checkpoint(task, reason="awaiting approval")
                    self.storage.save_task(task)
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
            for tid in list(results):
                tasks.pop(tid, None)
            if not tasks:
                break
            if not admitted:
                # every remaining task is parked on a foreign lock (or the
                # cursor was approval-gated and nothing else is ready):
                # return cleanly - the caller re-checks the live lock store /
                # approval state and re-invokes. No spin, no busy loop.
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

    def _enqueue_step(self, task: Task, step: PlanStep) -> None:
        """Admit one step to the shared scheduler + durable registry
        (ADR-025): a QUEUED registry row is created (bounded metadata) and
        the work item carries the row's id so the worker can mirror
        RUNNING/terminal transitions."""
        work_id = None
        if self.scheduler_registry is not None:
            try:
                work = self.scheduler_registry.create(
                    task_id=task.id, goal_id=task.goal_id, step_index=step.index,
                    scheduler_id=self.scheduler_id, now=self._lock_now())
                work_id = work.work_id
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"scheduler registry failed closed: {exc}"
                return
        self.scheduler.enqueue(
            f"{task.id}:{step.index}", task.id, step.index,
            (lambda s=step, w=work_id: self._run_step_worker(task, s, w)))

    def _complete_task_shared(self, task: Task) -> None:
        """Terminal COMPLETED handling shared by the multi-task driver."""
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
        """Drive MULTIPLE goals through the shared scheduler (ADR-025).

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
                goal = gm.get_goal(gid)
                if goal is None or goal.status in (GoalStatus.COMPLETED, GoalStatus.FAILED):
                    if goal is not None:
                        results[gid] = goal
                    continue
                result, _ = gm.evaluate(gid)
                action = result.next_action
                if action == "complete":
                    gm.complete_goal(gid, reason="all_work_complete")
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
                        gm.fail_goal(gid, reason="max_replans_exceeded")
                        results[gid] = gm.get_goal(gid)
                        continue
                    if self._block_on_missing_capability(gid, gm):
                        continue
                    if self._block_on_open_recovery(gid, gm):
                        continue
                    if self._block_on_lock_contention(gid, gm):
                        continue
                    task = self._plan_for_goal(
                        gid, replan_reason=result.evidence.get("reason"))
                else:  # continue / initial_plan
                    if self._block_on_missing_capability(gid, gm):
                        continue
                    if self._block_on_open_recovery(gid, gm):
                        continue
                    if self._block_on_lock_contention(gid, gm):
                        continue
                    task = gm.pending_task(gid)
                    if task is None:
                        task = self._plan_for_goal(gid, replan_reason=None)
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
            self.run_tasks(tasks_to_run)
            # goals whose task stopped at a decision point stay active; the
            # next cycle evaluates them (replan/fail/complete/await).
        for gid in goal_ids:
            results.setdefault(gid, gm.get_goal(gid))
        return results

    # ---------- pipeline ----------

    def _plan(self, task: Task, replan_reason: str | None = None) -> Task:
        self._emit("task.planning", task_id=task.id)
        task.status = TaskStatus.PLANNING
        context = self._build_planning_context(task)
        try:
            steps = self.planner.plan(task.description, task.id, self.registry, context=context)
        except Exception as exc:  # planner/validator/provider failure: degrade gracefully
            task.status = TaskStatus.FAILED
            task.error = f"planning failed: {exc}"
            task.completed_at = utcnow()
            self.storage.save_task(task)
            self._emit("error", task_id=task.id, success=False, detail={
                "error": task.error,
                "error_type": type(exc).__name__,
                "category": getattr(exc, "category", "unknown"),
            })
            self._emit("task.failed", task_id=task.id, detail={"error": task.error})
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
        self.storage.save_task(task)
        self._emit(
            "plan.produced",
            task_id=task.id,
            detail={"steps": [s.to_dict() for s in steps]},
        )

        # Long-horizon goal management (ADR-016): record an IMMUTABLE plan
        # version + strategy against the goal; previous plans are never
        # mutated. The task carries its plan_version for replay safety.
        if self.goal_manager is not None and task.goal_id:
            try:
                strategy_name = "direct"
                if context is not None and getattr(context, "strategy", None) is not None:
                    strategy_name = context.strategy.name
                elif self.strategy_selector is not None:
                    beliefs = self.cognition.cognition.list_beliefs(limit=100) if self.cognition else []
                    env_state = self.world_monitor.current_state() if self.world_monitor else {}
                    guidance = list(getattr(context, "guidance", []) or [])
                    strategy_name = self.strategy_selector.select(
                        task.description, beliefs, env_state, guidance
                    ).name
                history = self.goal_manager.plan_history(task.goal_id)
                reason = "initial_plan" if not history else (
                    f"replan_{replan_reason}" if replan_reason else "replan"
                )
                record = self.goal_manager.record_plan_version(
                    task.goal_id, strategy_name, [s.to_dict() for s in steps], reason
                )
                task.plan_version = record["plan_version"]
                self.storage.save_task(task)
                if reason.startswith("replan"):
                    self._emit("goal.replanned", task_id=task.id, detail={
                        "goal_id": task.goal_id,
                        "plan_version": record["plan_version"],
                        "strategy": strategy_name,
                        "reason": reason[:200],
                    })
                    # keep the goal row's replan provenance in sync (the plan
                    # history is the source of truth; this mirrors the reason
                    # on the goal for CLI/debugging, ADR-016)
                    try:
                        g = self.goal_manager.get_goal(task.goal_id)
                        if g is not None:
                            g.last_replan_reason = reason
                            g.updated_at = utcnow()
                            self.storage.save_goal(g)
                    except Exception:
                        pass
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
            return self._authz_fingerprint(request) == rec.get("fingerprint")
        except Exception:
            return False

    def _run_step_worker(self, task: Task, step: PlanStep, work_id: str | None = None) -> None:
        """Execute one step on a scheduler worker (ADR-024 Phase D / ADR-025).

        Terminal per-step status is persisted IMMEDIATELY after the step
        finishes, so a crash mid-round (or process kill) never replays a
        completed mutation on restart: the durable per-step state is the
        unit of restart, not the whole round. Bounded metadata only - the
        task snapshot is the pre-existing durable record (step ids, statuses,
        timestamps); no thread objects, stack traces, capability outputs or
        model output are ever persisted.

        When a durable registry row exists for this dispatch (ADR-025), the
        worker mirrors its lifecycle: QUEUED -> RUNNING (with a lease)
        BEFORE the capability pipeline starts, and RUNNING -> COMPLETED /
        FAILED afterwards. If the mirror fails (row was abandoned/reclaimed
        or is unknown), the step FAILS CLOSED and never executes."""
        if work_id is not None and self.scheduler_registry is not None:
            try:
                self.scheduler_registry.mark_running(
                    work_id,
                    worker_id=f"worker:{_threading.get_ident()}:{new_id('w')}",
                    lease_seconds=self.scheduler_lease_seconds,
                    now=self._lock_now())
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = f"scheduler registry failed closed: {exc}"
                self.storage.save_task(task)
                self._emit("error", task_id=task.id, step_id=_step_id(step), success=False,
                           detail={"error": step.error[:300]})
                return
        try:
            self._execute_step(task, step)
        finally:
            if work_id is not None and self.scheduler_registry is not None:
                # the WORK unit finished: FAILED only when the step failed;
                # SUCCEEDED/SKIPPED AND approval-paused steps are COMPLETED
                # work (an approval-paused step requested its approval and
                # stops cleanly - the task state is the authority on that)
                terminal = (SchedulerWorkStatus.FAILED
                            if step.status == StepStatus.FAILED
                            else SchedulerWorkStatus.COMPLETED)
                try:
                    self.scheduler_registry.mark_terminal(
                        work_id, terminal, error=step.error,
                        now=self._lock_now())
                except Exception:
                    pass  # the durable step/task state is the authority
        if step.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED):
            self.storage.save_task(task)

    def _execute_step(self, task: Task, step: PlanStep) -> None:
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
            detail={
                **decision.to_dict(),
                "params": request.params,
                "step_declared_scope": step.scope,
                "actor": request.actor.id,
                "actor_chain": list(request.actor.chain),
            },
        )

        if decision.outcome == PolicyOutcome.DENY:
            step.status = StepStatus.FAILED
            step.error = decision.reason
            self._emit(
                "permission.denied",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail=decision.to_dict(),
            )
            return

        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            if not self._handle_approval(task, step, request, decision):
                return  # denied or paused (task will be checkpointed by the caller)

        # 3. Execute with retries, then 4. verify
        self._execute_with_retries(task, step, capability, spec)

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
            if self._authz_fingerprint(request) == record.get("fingerprint"):
                # the exact approved request is still valid against LIVE
                # metadata: resume without re-requesting
                self._emit("task.approval.resumed", task_id=task.id, step_id=_step_id(step), detail={
                    "approval_record": record.get("record_id"),
                    "approval_id": record.get("approval_id"),
                    "resolved_by": record.get("resolved_by"),
                    "scope": request.scope,
                    "resource": request.resource,
                })
                if self.goal_manager is not None and task.goal_id:
                    try:
                        self.goal_manager.clear_blocker(task.goal_id, "approval_pending", reason="approval_resumed")
                    except Exception:
                        pass
                return True
            # stale approval (metadata changed): fall through to a fresh request

        fp = self._authz_fingerprint(request)
        if self.approval_store is not None:
            existing = self._pending_queue_request(task.id, step.index, fp)
            if existing is not None:
                # we are already durably waiting on this exact request:
                # idempotent - no new record, no re-request, no re-queue
                task.status = TaskStatus.AWAITING_APPROVAL
                step.status = StepStatus.PENDING
                return False

        self._emit("approval.requested", task_id=task.id, step_id=_step_id(step), detail=decision.to_dict())
        outcome = self.approval_handler.request(request, decision)
        if outcome == ApprovalOutcome.APPROVED:
            self._append_approval_record(task, step, request, decision, "approved", actor="system")
            self._emit("approval.granted", task_id=task.id, step_id=_step_id(step), detail=decision.to_dict())
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
                detail=decision.to_dict(),
            )
            return False
        # PENDING: queue exactly one durable request, pause the task durably;
        # a later resolve_approval_request + run resumes the exact same step
        task.status = TaskStatus.AWAITING_APPROVAL
        step.status = StepStatus.PENDING
        req = self._queue_request_from_auth(task, step, request, decision)
        if self.approval_store is not None:
            try:
                self.approval_store.create_request(req)
            except Exception:
                pass
            self._emit("approval.queued", task_id=task.id, step_id=_step_id(step), detail={
                "approval_id": req.approval_id,
                "task_id": task.id,
                "step_index": step.index,
                "capability": req.capability,
                "action": req.action,
                "scope": req.scope,
                "resource": req.resource,
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
                    "resource": request.resource,
                    "approval_id": req.approval_id,
                    "reason": decision.reason[:200],
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
                    "resource": request.resource,
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
        task.approvals = list(task.approvals or []) + [{
            "record_id": new_id("apr"),
            "step_index": step.index,
            "outcome": outcome,
            "actor": actor,
            "created_at": utcnow(),
            "reason": decision.reason[:200],
            "request": {
                "capability": request.capability,
                "action": request.action,
                "scope": request.scope,
                "risk": request.risk,
                "side_effects": request.side_effects,
                "resource_kind": request.resource_kind,
                "resource": request.resource,
                "params_keys": sorted(request.params.keys()),
            },
            "fingerprint": self._authz_fingerprint(request),
        }]

    def _approved_record_for(self, task: Task, step_index: int) -> dict | None:
        """The most recent APPROVED record for a step, if any."""
        for r in reversed(list(task.approvals or [])):
            if r.get("step_index") == step_index and r.get("outcome") == "approved":
                return r
        return None

    def _authz_fingerprint(self, request: AuthorizationRequest) -> dict[str, Any]:
        """Canonical authorization fingerprint (ADR-017/018).

        Everything an approval covers: capability, action, the resolved
        required scope, risk, side effects, resource kind, resource, and the
        SECURITY-RELEVANT parameters declared by the live ActionSpec
        (ActionSpec.security_relevant_params). The resource parameter is
        always covered via `resource`. Operational parameters (limits,
        formatting, verification args) are NOT fingerprinted unless declared.
        Any change forces fresh authorization.
        """
        fp: dict[str, Any] = {
            "capability": request.capability,
            "action": request.action,
            "scope": request.scope,
            "risk": request.risk,
            "side_effects": request.side_effects,
            "resource_kind": request.resource_kind,
            "resource": request.resource,
        }
        srp: list[str] = []
        try:
            spec = self.registry.action_spec(request.capability, request.action)
            if spec is not None:
                srp = list(getattr(spec, "security_relevant_params", []) or [])
        except Exception:
            srp = []
        fp["security_relevant_params"] = {k: request.params.get(k) for k in srp if k in request.params}
        return fp

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

    def _execute_with_retries(self, task: Task, step: PlanStep, capability, spec) -> None:
        verify_failed = False
        exec_error: str | None = None
        mutating = getattr(spec, "side_effects", "read_only") == "mutating"
        lock: Any = None
        waited = False
        if mutating:
            # ORDERING (ADR-021/022): authorization (live policy + approval)
            # has already succeeded in _execute_step BEFORE we reach this
            # point. The advisory mutation lock is acquired NOW, immediately
            # before the actual mutation, with BOUNDED WAITING on contention
            # (ADR-022), and released on EVERY terminal path. A lock is
            # coordination - never permission; waiting is coordination too.
            from arion.state.locks import MutationLockError, MutationLockTimeoutError

            try:
                lock, waited = self._acquire_mutation_lock(task, step, spec)
            except MutationLockTimeoutError as exc:
                # durable typed timeout (blocker set inside); task fails via
                # the normal FAILED path in run_task
                step.error = str(exc)
                return  # capability NEVER executes; no recovery record
            except MutationLockError as exc:
                # waiting disabled: immediate, durable contention failure
                # (ADR-021 semantics preserved)
                step.status = StepStatus.FAILED
                step.error = f"mutation lock contention: {exc}"
                self._emit("mutation.lock.contended", task_id=task.id,
                           step_id=_step_id(step), success=False, detail={
                               "error": step.error[:200],
                               "capability": step.capability,
                               "action": step.action,
                           })
                self._set_lock_contention_blocker(task, step, spec)
                return  # capability NEVER executes; task fails durably
            if lock is not None:
                # After a WAITED acquire, re-check LIVE authorization before
                # mutating: the approval/authorization window may have gone
                # stale while waiting. If the step pauses (fresh approval
                # queued) or is denied, release the lock and do NOT execute.
                if not self._revalidate_before_mutation(task, step, spec, waited=waited):
                    self._release_mutation_lock(lock, task, step)
                    return
        try:
            self._execute_attempts(task, step, capability, spec, mutating,
                                   verify_failed, exec_error)
        finally:
            if lock is not None:
                self._release_mutation_lock(lock, task, step)

    def _execute_attempts(self, task: Task, step: PlanStep, capability, spec,
                          mutating: bool, verify_failed: bool, exec_error: str | None) -> None:
        while step.attempts < step.max_attempts:
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
                observation = capability.execute(step.action, dict(step.params))
            except CapabilityError as exc:
                exec_error = str(exc)
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
            except Exception as exc:  # unexpected capability bug - fail loudly
                step.status = StepStatus.FAILED
                step.error = f"capability raised unexpected error: {exc!r}"
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
            ctx = build_planning_context(retriever, task.description, ContextBudget())
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
            # that already failed (ADR-016). It can never authorize anything.
            if self.strategy_selector is not None:
                try:
                    beliefs = self.cognition.cognition.list_beliefs(limit=100) if self.cognition else []
                    env_state = self.world_monitor.current_state() if self.world_monitor else {}
                    previous_strategies: list[str] = []
                    if self.goal_manager is not None and task.goal_id:
                        previous_strategies = [
                            p.get("strategy", "") for p in self.goal_manager.plan_history(task.goal_id)
                        ]
                    strategy = self.strategy_selector.select(
                        task.description, beliefs, env_state, ctx.guidance,
                        previous_strategies=[s for s in previous_strategies if s],
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

        Runs best-effort: memory failure never changes task outcome. Stores
        structured summaries only - never secrets, credentials, raw prompts,
        or raw model responses.
        """
        if self.memory is None:
            return
        try:
            from arion.memory.lifecycle import build_episode_from_task
            from arion.memory.reflector import DeterministicReflector

            events = []
            try:
                events = self.storage.list_events(task.id)
            except Exception:
                events = []
            episode = build_episode_from_task(task, events, registry=self.registry)
            self.memory.record_episode(episode)
            self._emit(
                "memory.episode.recorded",
                task_id=task.id,
                detail={
                    "episode_id": episode.episode_id,
                    "outcome": episode.outcome,
                    "tags": episode.tags[:20],
                    "importance": round(episode.importance, 2),
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
                self._emit(
                    "reflection.validation.failed",
                    task_id=task.id,
                    success=False,
                    detail={"error": str(exc)[:300], "fallback": "deterministic"},
                )
            if reflection is None:
                reflection = DeterministicReflector().reflect(episode)
            self.memory.record_reflection(reflection)
            try:
                self.memory.link_reflection(episode.episode_id, reflection.reflection_id)
            except Exception:
                pass
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
        except Exception:
            # Memory is best-effort; never break the task lifecycle.
            pass

    def _derive_beliefs(self, episode, reflection) -> None:
        """Derive + store cognitive beliefs from the latest experience.

        Every belief carries provenance (episode/reflection/guidance ids),
        confidence, timestamps, and source. Best-effort: cognitive state must
        never break the task loop. Informational only.
        """
        if self.cognition is None or self.belief_deriver is None:
            return
        try:
            from arion.memory.guidance import DeterministicMemoryGuidance

            guidance = DeterministicMemoryGuidance().build([episode], [reflection])
            beliefs = self.belief_deriver.derive([episode], [reflection], guidance)
            store = self.cognition.cognition  # SQLiteCognitiveStore behind the facade
            for b in beliefs:
                existing = store.list_beliefs(category=b.category, limit=1000)
                if any(e.statement == b.statement and e.confidence >= b.confidence for e in existing):
                    continue
                store.record_belief(b)
                self._emit(
                    "belief.derived",
                    task_id=episode.task_id,
                    detail={
                        "belief_id": b.belief_id,
                        "category": b.category,
                        "confidence": round(b.confidence, 3),
                        "importance": round(b.importance, 3),
                        "provenance": b.provenance,
                        "source": b.source,
                    },
                )
        except Exception:
            pass

    def _consolidate(self, task_id: str) -> None:
        """Run deterministic consolidation; emit memory.consolidated per record."""
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
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.events.emit(AuditEvent(kind=kind, task_id=task_id, step_id=step_id, success=success, detail=detail or {}))


def _step_id(step: PlanStep) -> str:
    return f"step_{step.index}"
