"""Orchestration layer: the agent loop engine.

Ownership: the engine owns task lifecycle, state transitions, permissions,
execution, verification, checkpoints, recovery and completion. The LLM/model is
only ever an intelligence component the engine calls (via the ModelRouter) -
it never drives the loop (architectural rule, ADR-004).

Conceptual flow per task:
  Goal -> Task -> Plan -> Permission -> Capability -> Observation
        -> Verification -> Checkpoint -> Complete/Recover
"""

from __future__ import annotations

from typing import Any, Protocol

from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.intelligence.planner import Planner
from arion.intelligence.router import ModelRouter
from arion.observability.events import AuditEvent, EventLogger
from arion.state.models import (
    Checkpoint,
    Goal,
    PlanStep,
    StepStatus,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)
from arion.state.store import Storage


class PermissionPolicy(Protocol):
    """Decides whether a requested scope/params may run."""

    def check(self, scope: str, params: dict[str, Any]) -> tuple[bool, str | None]: ...


class AllowAllPolicy:
    """Default policy for the vertical slice: allow only declared read scopes.

    In this slice every planned step declares a read-only scope, so allow-all
    is still safe. Future policies (explicit allowlists, human approval, time
    windows) implement the same protocol.
    """

    def check(self, scope: str, params: dict[str, Any]) -> tuple[bool, str | None]:
        if not scope:
            return False, "empty permission scope"
        if not scope.startswith("filesystem:"):
            return False, f"scope {scope!r} not permitted by current policy"
        return True, None


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
    ):
        self.storage = storage
        self.registry = registry
        self.planner = planner
        self.router = router
        self.events = events
        self.policy = policy or AllowAllPolicy()

    # ---------- public API ----------

    def submit_goal(self, description: str, source: str = "cli") -> Goal:
        goal = Goal(id=new_id("goal"), description=description, source=source)
        self.storage.save_goal(goal)
        self._emit("goal.submitted", task_id=None, detail={"goal_id": goal.id, "description": description})
        return goal

    def create_task(self, goal: Goal) -> Task:
        task = Task(id=new_id("task"), goal_id=goal.id, description=goal.description)
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

    def run_task(self, task_id: str) -> Task:
        """Resume-or-start a task and drive it to completion/failure.

        Survives restarts: if a checkpoint exists, state is restored and the
        task resumes from the checkpointed step instead of starting over.
        """
        task = self.storage.load_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")

        checkpoint = self.storage.latest_checkpoint(task_id)
        if checkpoint is not None:
            restored = Task.from_dict(checkpoint.snapshot)
            self._emit("task.resumed", task_id=task_id, detail={"step_index": restored.current_step})
            task = restored
        else:
            self._emit("task.planning", task_id=task_id)

        # Already terminal (e.g. completed before a restart): return as-is.
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return task

        if not task.steps:
            task = self._plan(task)

        while True:
            step = task.active_step
            if step is None:
                break
            self._execute_step(task, step)
            self.storage.save_task(task)

            if step.status == StepStatus.FAILED:
                task.status = TaskStatus.FAILED
                task.error = step.error or "step failed"
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._emit("task.failed", task_id=task.id, detail={"step_index": step.index, "error": task.error})
                return task

            if step.index + 1 >= len(task.steps):
                task.status = TaskStatus.COMPLETED
                task.completed_at = utcnow()
                self.storage.save_task(task)
                self._checkpoint(task, reason="task completed")
                self._emit("task.completed", task_id=task.id, detail={"steps": len(task.steps)})
                return task

            task.current_step += 1
            self._checkpoint(task, reason="step completed")

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return task
        raise RuntimeError(f"task {task_id} terminated without terminal state")

    # ---------- pipeline ----------

    def _plan(self, task: Task) -> Task:
        self._emit("task.planning", task_id=task.id)
        task.status = TaskStatus.PLANNING
        steps = self.planner.plan(task.description, task.id, self.registry)
        task.steps = steps
        task.status = TaskStatus.PLANNED
        self.storage.save_task(task)
        self._emit(
            "plan.produced",
            task_id=task.id,
            detail={"steps": [s.to_dict() for s in steps]},
        )
        self._checkpoint(task, reason="plan produced")
        return task

    def _execute_step(self, task: Task, step: PlanStep) -> None:
        self._emit("step.started", task_id=task.id, step_id=_step_id(step), detail={"intent": step.intent})

        # 1. Permission check
        allowed, denial = self.policy.check(step.scope, step.params)
        if not allowed:
            step.status = StepStatus.FAILED
            step.error = denial or "permission denied"
            self._emit(
                "permission.denied",
                task_id=task.id,
                step_id=_step_id(step),
                success=False,
                detail={"scope": step.scope, "params": step.params, "reason": step.error},
            )
            return
        self._emit(
            "permission.checked",
            task_id=task.id,
            step_id=_step_id(step),
            detail={"scope": step.scope, "params": step.params},
        )

        # 2. Capability discovery
        capability = self.registry.get(step.capability)
        if capability is None:
            step.status = StepStatus.FAILED
            step.error = f"capability not found: {step.capability}"
            self._emit("error", task_id=task.id, step_id=_step_id(step), success=False, detail={"error": step.error})
            return
        self._emit("capability.discovered", task_id=task.id, step_id=_step_id(step), detail={"capability": step.capability})

        # 3. Execute with retries, then 4. verify
        verify_failed = False
        while step.attempts < step.max_attempts:
            step.attempts += 1
            step.status = StepStatus.RUNNING
            try:
                observation = capability.execute(step.action, dict(step.params))
            except CapabilityError as exc:
                step.error = str(exc)
                step.result = None
                if step.attempts >= step.max_attempts:
                    break
                self._emit(
                    "step.retrying",
                    task_id=task.id,
                    step_id=_step_id(step),
                    success=False,
                    detail={"attempt": step.attempts, "error": step.error},
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
            if step.attempts < step.max_attempts:
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
        step.error = step.error or "step failed"
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
