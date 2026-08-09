"""Lifecycle integration: build structured episodes from finished tasks.

Pure helper - converts a terminal Task (plus optional audit events) into an
Episode. Used by the orchestration engine after completion/failure.

Privacy rules:
- plan_summary stores param KEY NAMES only, never values.
- failures bound error text (first 500 chars).
- authorization stores scope/resource/reason - never tokens or credentials.
- no raw prompts or model responses are ever stored.
"""

from __future__ import annotations

from typing import Any

from arion.memory.models import Episode, EPISODE_OUTCOMES
from arion.observability.events import AuditEvent
from arion.state.models import PlanStep, StepStatus, Task, TaskStatus, new_id, utcnow

_DENY_MARKERS = ("not permitted", "outside boundary", "no resource boundary", "approval denied",
                 "explicitly denied", "fail closed")


def episode_outcome_for(task: Task, events: list[AuditEvent] | None = None) -> str:
    """completed | failed | denied | recovered"""
    denials = [e for e in (events or []) if e.kind in ("permission.denied", "approval.denied")]
    if denials and task.status == TaskStatus.FAILED:
        return "denied"
    if any(e.kind == "task.resumed" for e in (events or [])) and task.status == TaskStatus.COMPLETED:
        return "recovered"
    if task.status == TaskStatus.COMPLETED:
        return "completed"
    return "failed"


def _importance_for(outcome: str) -> float:
    return {"completed": 0.5, "recovered": 0.6, "denied": 0.65, "failed": 0.7}.get(outcome, 0.5)


def build_episode_from_task(task: Task, events: list[AuditEvent] | None = None) -> Episode:
    """Construct a structured episode from a terminal task."""
    events = events or []
    outcome = episode_outcome_for(task, events)

    plan_summary = [
        {
            "index": s.index,
            "intent": s.intent,
            "capability": s.capability,
            "action": s.action,
            "status": s.status.value,
            "params_keys": sorted(s.params.keys()),  # keys only - never values
        }
        for s in task.steps
    ]
    actions = [
        {"capability": s.capability, "action": s.action, "status": s.status.value, "attempts": s.attempts}
        for s in task.steps
    ]
    verification = {
        "passed": [s.index for s in task.steps if s.status == StepStatus.SUCCEEDED],
        "failed": [s.index for s in task.steps if s.status == StepStatus.FAILED],
    }

    failures: list[dict[str, Any]] = []
    for s in task.steps:
        if s.status == StepStatus.FAILED and s.error:
            failures.append({
                "step": s.index,
                "capability": s.capability,
                "action": s.action,
                "error": s.error[:500],
                "category": _category_for(s, task, events),
            })
    if task.error and not failures:
        failures.append({"step": None, "error": task.error[:500], "category": _category_for(None, task, events)})

    denials = [
        {
            "scope": e.detail.get("scope"),
            "resource": e.detail.get("resource"),
            "reason": str(e.detail.get("reason", ""))[:200],
        }
        for e in events
        if e.kind in ("permission.denied", "approval.denied")
    ]
    authorization = {
        "denials": denials,
        "approvals_required": any(e.kind == "approval.requested" for e in events),
    }

    recovery = {"resumed": any(e.kind == "task.resumed" for e in events)}

    tags: set[str] = set()
    for s in task.steps:
        tags.add(s.capability)
    tags.add(f"outcome:{outcome}")
    for f in failures:
        if f.get("category"):
            tags.add(f"category:{f['category']}")
    if recovery["resumed"]:
        tags.add("recovery:resumed")
    if denials:
        tags.add("authorization:denied")

    return Episode(
        episode_id=new_id("ep"),
        task_id=task.id,
        goal_id=task.goal_id,
        goal=task.description[:500],
        plan_summary=plan_summary,
        actions=actions,
        outcome=outcome,
        verification=verification,
        failures=failures,
        authorization=authorization,
        recovery=recovery,
        tags=sorted(tags),
        importance=_importance_for(outcome),
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def _category_for(step: PlanStep | None, task: Task, events: list[AuditEvent]) -> str:
    """Best-effort typed category from audit events (provider/schema/capability/execution)."""
    error_events = [e for e in events if e.kind == "error" and e.detail.get("category")]
    if error_events:
        return str(error_events[-1].detail["category"])
    if task.error and task.error.startswith("planning failed"):
        return "planning"
    return "execution"
