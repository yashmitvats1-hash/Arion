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

from typing import Any

from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.state.approvals import ApprovalError, ApprovalRequest, ApprovalStatus, ApprovalStore
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
    PlanStep,
    StepStatus,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)
from arion.state.store import Storage


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
            task = self._plan_for_goal(goal_id, replan_reason=None)
            if task is not None:
                task = self.run_task(task.id)
                if task.status in (TaskStatus.FAILED, TaskStatus.AWAITING_APPROVAL):
                    return gm.get_goal(goal_id)

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

        while True:
            step = task.active_step
            if step is None:
                # Walked past the last step: every step was either executed or
                # explicitly skipped, so the task reaches a terminal state.
                skipped = sum(1 for s in task.steps if s.status == StepStatus.SKIPPED)
                task.status = TaskStatus.COMPLETED
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._checkpoint(task, reason="task completed")
                self._emit(
                    "task.completed",
                    task_id=task.id,
                    detail={"steps": len(task.steps), "skipped_steps": skipped},
                )
                self._record_memory(task)
                return task

            if step.status == StepStatus.SKIPPED:
                # Explicitly skipped step (memory guidance): never executed,
                # audited with its provenance, terminal for dependency purposes.
                self._emit(
                    "step.skipped",
                    task_id=task.id,
                    step_id=_step_id(step),
                    detail={
                        "reason": step.skipped_reason or "skipped",
                        "guidance": step.guidance[:5],
                    },
                )
            else:
                task.status = TaskStatus.RUNNING
                self._execute_step(task, step)

                if step.status == StepStatus.PENDING and task.status == TaskStatus.AWAITING_APPROVAL:
                    self._checkpoint(task, reason="awaiting approval")
                    self.storage.save_task(task)
                    return task

                self.storage.save_task(task)

                if step.status == StepStatus.FAILED:
                    task.status = TaskStatus.FAILED
                    task.error = step.error or "step failed"
                    task.completed_at = utcnow()
                    self.storage.save_task(task)
                    self._emit("task.failed", task_id=task.id, detail={"step_index": step.index, "error": task.error})
                    self._record_memory(task)
                    return task

            task.current_step += 1
            self._checkpoint(task, reason="step completed")

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return task
        raise RuntimeError(f"task {task_id} terminated without terminal state")

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
        request = AuthorizationRequest(
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
        while step.attempts < step.max_attempts:
            step.attempts += 1
            step.status = StepStatus.RUNNING
            try:
                observation = capability.execute(step.action, dict(step.params))
            except CapabilityError as exc:
                exec_error = str(exc)
                step.result = None
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
