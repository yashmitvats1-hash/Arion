"""Deterministic progress evaluation (ADR-016).

ProgressEvaluator is a seam that evaluates a goal's progress from:

  - completed / failed / skipped work (task steps)
  - blockers
  - outstanding plan steps (latest plan version)
  - world-state changes since the last evaluation

It returns a structured ProgressResult:

  progress (0..1), status, blockers, next_action, evidence (with provenance)

Deterministic, model-independent, and INFORMATIONAL - it can never authorize
anything. It only recommends the next action for the goal loop.

next_action values:
  none              - terminal goal (no action)
  paused            - goal is paused
  await_approval    - a task is AWAITING_APPROVAL; stop cleanly (no spin), the
                      goal is durably BLOCKED with an approval_pending blocker
  resolve_blocker   - goal is blocked; do not execute
  continue          - run the next task for outstanding plan steps
  replan            - a task failed, the world materially changed, or a
                      missing-capability blocker became satisfiable; produce a
                      NEW plan version
  complete          - all plan steps succeeded and no blockers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from arion.state.models import Goal, GoalStatus, StepStatus, TaskStatus


@dataclass
class ProgressResult:
    """Structured progress evaluation for a goal."""

    goal_id: str
    progress: float                      # 0..1
    status: str                          # suggested goal status
    blockers: list[dict[str, Any]] = field(default_factory=list)
    next_action: str = "continue"
    evidence: dict[str, Any] = field(default_factory=dict)  # counts + provenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "progress": round(self.progress, 3),
            "status": self.status,
            "blockers": self.blockers,
            "next_action": self.next_action,
            "evidence": self.evidence,
        }


class ProgressEvaluator(Protocol):
    def evaluate(self, goal: Goal, tasks: list, latest_plan: dict | None,
                 world_changes: list | None = None, world_state: dict | None = None) -> ProgressResult: ...


class DeterministicProgressEvaluator:
    """Deterministic progress evaluation (reference path, offline).

    Rules (first match wins):
      1. terminal goal status           -> next_action "none"
      2. paused                         -> next_action "paused"
      3. any task AWAITING_APPROVAL     -> next_action "await_approval" (durable
                                          BLOCKED; never spin)
      4. blockers present:
         - missing_capability blockers whose capabilities are ALL present in
           the current world state -> next_action "replan" (capability_available)
         - otherwise                -> next_action "resolve_blocker"
      5. a resumable (non-terminal) task implements the LATEST plan version
         -> next_action "continue" (resume it; never abandon in-flight work
         - e.g. an approved task - merely because the world changed)
      6. world changes since last eval -> next_action "replan" (material)
      7. no plan / no tasks            -> next_action "continue" (first plan)
      8. any task failed               -> next_action "replan"
      9. all plan steps succeeded      -> next_action "complete"
      10. outstanding steps remain     -> next_action "continue"
    """

    @staticmethod
    def _registered_capabilities(world_state: dict | None) -> list:
        if not world_state:
            return []
        reg = world_state.get("registered_capabilities") or {}
        if isinstance(reg, dict):
            return list(reg.get("value", []) or [])
        if isinstance(reg, list):
            return list(reg)
        return []

    def evaluate(
        self,
        goal: Goal,
        tasks: list,
        latest_plan: dict | None,
        world_changes: list | None = None,
        world_state: dict | None = None,
    ) -> ProgressResult:
        world_changes = world_changes or []
        status = goal.status_value

        # Terminal / paused goals: no action.
        if goal.status in (GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED):
            return ProgressResult(
                goal_id=goal.id, progress=1.0 if goal.status == GoalStatus.COMPLETED else 0.0,
                status=status, next_action="none",
                evidence={"terminal": True, "goal_version": goal.version},
            )
        if goal.status == GoalStatus.PAUSED:
            return ProgressResult(
                goal_id=goal.id, progress=goal.progress_metadata.get("progress", 0.0),
                status=status, next_action="paused",
                evidence={"paused": True, "goal_version": goal.version},
            )

        evidence: dict[str, Any] = {
            "goal_version": goal.version,
            "tasks": len(tasks),
            "completed": 0, "failed": 0, "skipped": 0, "pending": 0,
            "awaiting_approval": 0,
            "plan_versions": 0,
            "world_changes": [w.to_dict() if hasattr(w, "to_dict") else w for w in world_changes[:10]],
        }
        blockers = list(goal.blockers or [])

        awaiting: list[dict[str, Any]] = []
        for t in tasks:
            st = t.status.value if hasattr(t.status, "value") else str(t.status)
            if st == TaskStatus.COMPLETED.value:
                evidence["completed"] += 1
            elif st == TaskStatus.FAILED.value:
                evidence["failed"] += 1
            else:
                evidence["pending"] += 1
                if st == TaskStatus.AWAITING_APPROVAL.value:
                    evidence["awaiting_approval"] += 1
                    awaiting.append({
                        "task_id": t.id,
                        "step_index": t.current_step,
                        "plan_version": t.plan_version,
                    })
        for t in tasks:
            for s in getattr(t, "steps", []):
                if s.status == StepStatus.SKIPPED:
                    evidence["skipped"] += 1

        plan_steps = 0
        if latest_plan is not None:
            try:
                plan_steps = len(latest_plan.get("plan_summary") or [])
            except (AttributeError, TypeError):
                plan_steps = 0
            evidence["plan_versions"] = plan_steps

        # Count handled steps (succeeded + skipped) across ALL tasks of the
        # goal - completion is never inferred from a single successful task.
        succeeded_steps = 0
        skipped_steps = 0
        for t in tasks:
            for s in getattr(t, "steps", []):
                if s.status == StepStatus.SUCCEEDED:
                    succeeded_steps += 1
                elif s.status == StepStatus.SKIPPED:
                    skipped_steps += 1
        evidence["succeeded_steps"] = succeeded_steps
        evidence["skipped_steps_total"] = skipped_steps
        handled_steps = succeeded_steps + skipped_steps

        progress = 0.0
        total_steps = plan_steps or max(succeeded_steps + evidence["failed"], 1)
        if total_steps:
            progress = min(1.0, succeeded_steps / total_steps)

        # Rule 3: a task awaiting approval -> stop cleanly (never spin), the
        # goal stays durably BLOCKED; approval-pending is distinct from a task
        # failure and from a missing capability.
        if awaiting:
            return ProgressResult(
                goal_id=goal.id, progress=progress,
                status=GoalStatus.BLOCKED.value if blockers else goal.status_value,
                blockers=blockers, next_action="await_approval",
                evidence={**evidence, "reason": "awaiting_approval",
                          "approval_pending_steps": awaiting[:10]},
            )

        # Rule 4: blockers.
        # A missing_capability blocker whose required capabilities are ALL
        # present in the CURRENT world state is resolved -> replan
        # (capability_available). Any other blocker (e.g. approval_pending,
        # or a missing capability that is STILL missing) -> resolve_blocker.
        if blockers:
            missing_blocks = [
                b for b in blockers
                if (b.get("key") or b.get("type")) == "missing_capability"
                and (b.get("capabilities") or [])
            ]
            non_missing = [
                b for b in blockers
                if (b.get("key") or b.get("type")) != "missing_capability"
            ]
            caps = self._registered_capabilities(world_state)
            still_missing = [
                cap for b in missing_blocks for cap in (b.get("capabilities") or [])
                if cap not in caps
            ]
            if missing_blocks and not non_missing and not still_missing:
                return ProgressResult(
                    goal_id=goal.id, progress=progress, status=GoalStatus.ACTIVE.value,
                    blockers=[], next_action="replan",
                    evidence={**evidence, "reason": "capability_available",
                              "cleared_blockers": ["missing_capability"]},
                )
            return ProgressResult(
                goal_id=goal.id, progress=progress, status=GoalStatus.BLOCKED.value,
                blockers=blockers, next_action="resolve_blocker",
                evidence={**evidence, "reason": "blocked"},
            )

        # Rule 5: a resumable task for the LATEST plan version takes priority
        # over a world-change replan - never abandon in-flight work (e.g. an
        # approved step awaiting resume) just because the world changed.
        if latest_plan is not None:
            latest_version = latest_plan.get("plan_version")
            resumable = [
                t for t in tasks
                if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                and t.status != TaskStatus.AWAITING_APPROVAL
                and (t.plan_version == latest_version or t.plan_version is None)
            ]
            if resumable:
                return ProgressResult(
                    goal_id=goal.id, progress=progress, status=goal.status_value,
                    blockers=[], next_action="continue",
                    evidence={**evidence, "reason": "resume_pending",
                              "resume_task_id": resumable[0].id},
                )

        # Rule 6: world changed materially -> replan
        if world_changes:
            return ProgressResult(
                goal_id=goal.id, progress=progress, status=GoalStatus.ACTIVE.value,
                blockers=[], next_action="replan",
                evidence={**evidence, "reason": "world_changed",
                          "world_change_keys": [w.key for w in world_changes[:10]]},
            )

        # Rule 7: no plan yet -> continue (first plan)
        if latest_plan is None:
            return ProgressResult(
                goal_id=goal.id, progress=0.0, status=GoalStatus.ACTIVE.value,
                blockers=[], next_action="continue",
                evidence={**evidence, "reason": "initial_plan"},
            )

        latest_version = latest_plan.get("plan_version")
        # A failed task implementing the LATEST plan version is unresolved
        # work -> replan (superseded failures from older versions are not
        # counted once a newer plan is fully handled). A task without a plan
        # version is treated as belonging to the latest plan (conservative).
        latest_failed = [
            t for t in tasks
            if t.status == TaskStatus.FAILED
            and (t.plan_version == latest_version or t.plan_version is None)
        ]
        evidence["latest_plan_failed"] = len(latest_failed)

        # Rule 8: ALL plan steps of the LATEST version handled (succeeded or
        # explicitly skipped) with no unresolved failure -> complete.
        if plan_steps > 0 and handled_steps >= plan_steps and not latest_failed:
            return ProgressResult(
                goal_id=goal.id, progress=1.0, status=GoalStatus.COMPLETED.value,
                blockers=[], next_action="complete",
                evidence={**evidence, "reason": "all_work_complete"},
            )

        # Rule 9: unresolved failed work on the latest plan -> replan
        if latest_failed:
            return ProgressResult(
                goal_id=goal.id, progress=progress, status=GoalStatus.ACTIVE.value,
                blockers=[], next_action="replan",
                evidence={**evidence, "reason": "task_failed"},
            )

        # Rule 10: outstanding work remains -> continue
        return ProgressResult(
            goal_id=goal.id, progress=progress, status=GoalStatus.ACTIVE.value,
            blockers=[], next_action="continue",
            evidence={**evidence, "reason": "outstanding_work"},
        )
