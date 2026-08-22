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
from arion.resource_identifiers import present_resource
from arion.state.models import PlanStep, StepStatus, Task, TaskStatus, new_id, utcnow

_DENY_MARKERS = ("not permitted", "outside boundary", "no resource boundary", "approval denied",
                 "explicitly denied", "fail closed")


def episode_outcome_for(task: Task, events: list[AuditEvent] | None = None) -> str:
    """completed | failed | denied | recovered

    'recovered' requires a genuine mid-execution resume: a task.resumed event
    whose checkpoint had begun executing steps (mid_execution=True). A
    plan-only checkpoint (the normal start-of-run boundary) is NOT an
    interruption - resuming from it is a plain completed run, so successful
    tasks still yield 'completed' episodes (and prefer guidance).
    """
    denials = [e for e in (events or []) if e.kind in ("permission.denied", "approval.denied")]
    if denials and task.status == TaskStatus.FAILED:
        return "denied"
    resumed = [e for e in (events or []) if e.kind == "task.resumed"]
    mid_execution = any(bool(e.detail.get("mid_execution")) for e in resumed)
    if mid_execution and task.status == TaskStatus.COMPLETED:
        return "recovered"
    if task.status == TaskStatus.COMPLETED:
        return "completed"
    return "failed"


def _importance_for(outcome: str) -> float:
    return {"completed": 0.5, "recovered": 0.6, "denied": 0.65, "failed": 0.7}.get(outcome, 0.5)


def build_episode_from_task(
    task: Task,
    events: list[AuditEvent] | None = None,
    registry: Any | None = None,
) -> Episode:
    """Construct a structured episode from a terminal task.

    When a registry is provided, each step's DECLARED resource value (from the
    ActionSpec resource_param) is recorded in `resources` - structured safe
    metadata that lets later planning guidance be resource-aware. Arbitrary
    param values are still never stored.
    """
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
    resources: list[dict[str, Any]] = []
    if registry is not None:
        for s in task.steps:
            spec = None
            try:
                spec = registry.action_spec(s.capability, s.action)
            except Exception:
                spec = None
            if spec is not None and spec.resource_kind and spec.resource_param:
                value = s.params.get(spec.resource_param)
                if isinstance(value, str) and value:
                    presentation = present_resource(spec.resource_kind, value)
                    resources.append({
                        "step": s.index,
                        "capability": s.capability,
                        "action": s.action,
                        "resource_kind": spec.resource_kind,
                        "resource": presentation.display,
                        "resource_fingerprint": presentation.fingerprint,
                        "resource_redacted": presentation.redacted,
                        "status": s.status.value,
                    })
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
            "resource_kind": e.detail.get("resource_kind"),
            "resource": e.detail.get("resource"),
            "resource_fingerprint": e.detail.get("resource_fingerprint"),
            "resource_redacted": bool(e.detail.get("resource_redacted", False)),
            "reason": str(e.detail.get("reason", ""))[:200],
        }
        for e in events
        if e.kind in ("permission.denied", "approval.denied")
    ]
    authorization = {
        "denials": denials,
        "approvals_required": any(e.kind == "approval.requested" for e in events),
    }

    resumed_events = [e for e in events if e.kind == "task.resumed"]
    mid_execution = any(bool(e.detail.get("mid_execution")) for e in resumed_events)
    recovery = {
        "resumed": mid_execution,
        "plan_only_resume": bool(resumed_events) and not mid_execution,
    }

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
        resources=resources,
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
