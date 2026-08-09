"""RealModelPlanner: structured planning through a ModelRouter (ADR-011).

Pipeline implemented here:

    Goal -> ModelRouter.plan_structured -> PlanSchema -> PlanValidator -> PlanSteps

The model proposes a structured plan; the PlanValidator validates it against
the live capability registry; authorization happens later in the orchestrator.
The planner NEVER grants permissions - it resolves scope/risk/side effects
from the registry's ActionSpec metadata.

Observability: emits planning.requested and plan.validation.passed/failed
events (without persisting raw prompts or model responses - see ADR-011).
"""

from __future__ import annotations

from typing import Any

from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.errors import PlanValidationError, PlanningError
from arion.intelligence.plan_schema import PlanSchema
from arion.intelligence.plan_validator import PlanValidator
from arion.intelligence.router import ModelRouter
from arion.observability.events import AuditEvent
from arion.state.models import PlanStep


class RealModelPlanner:
    """A Planner implementation that uses a ModelRouter for structured plans."""

    def __init__(self, router: ModelRouter, events: Any | None = None):
        self.router = router
        self.events = events  # duck-typed EventLogger (any object with .emit)

    def plan(self, goal_description: str, task_id: str, registry: CapabilityRegistry) -> list[PlanStep]:
        catalog = registry.capabilities_summary()
        self._emit("planning.requested", task_id=task_id, detail={"goal": goal_description[:200]})
        try:
            schema: PlanSchema = self.router.plan_structured(goal_description, catalog, {"task_id": task_id})
            steps = PlanValidator(registry).validate(schema)
        except PlanningError as exc:
            # Typed planning failure: propagate the category so audit/recovery
            # know WHY planning failed (provider, schema, capability, ...).
            self._emit(
                "plan.validation.failed",
                task_id=task_id,
                success=False,
                detail={"error": str(exc), "error_type": type(exc).__name__, "category": exc.category},
            )
            raise
        except Exception as exc:  # unexpected planner bug: keep the task failing gracefully
            self._emit(
                "plan.validation.failed",
                task_id=task_id,
                success=False,
                detail={"error": str(exc), "error_type": type(exc).__name__, "category": "unknown"},
            )
            raise PlanValidationError(str(exc)) from exc
        self._emit("plan.validation.passed", task_id=task_id, detail={"steps": len(steps)})
        return steps

    def _emit(self, kind: str, task_id: str | None, success: bool = True, detail: dict[str, Any] | None = None) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(
                AuditEvent(kind=kind, task_id=task_id, success=success, detail=detail or {})
            )
        except Exception:
            # Observability must never break planning.
            pass
