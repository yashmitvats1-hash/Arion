"""RealModelPlanner: structured planning through a ModelRouter (ADR-011,
ADR-057 D3/D5; M3 fallback composition).

Pipeline implemented here:

    Goal -> ModelRouter.plan_structured -> PlanSchema -> PlanValidator -> PlanSteps

with ADR-057 M3 fallback composition at the PLANNER layer:

    model failure (after permitted retries)
        -> DeterministicPlanner -> identical downstream pipeline

The model proposes a structured plan; the PlanValidator validates it against
the live capability registry; authorization happens later in the orchestrator.
The planner NEVER grants permissions - it resolves scope/risk/side effects
from the registry's ActionSpec metadata. Deterministic fallback produces a
plan that enters the SAME engine pipeline (status normalization, immutable
plan version, live authorization, execution) - there is no second execution
path, and a failed/adversarial model response carries no authority weight.

Retry separation (ADR-057 M1 vs M3):
- M1 transport retry: network/timeout/5xx/429 INSIDE the provider adapter,
  observable via the `model.retry` event. The planner never emits it.
- M3 semantic retry: malformed/schema/capability failures are reprompted at
  the planner/cognition boundary, bounded by `semantic_max_retries`. These
  retries emit NO `model.retry` event; `model.fallback` is emitted only after
  the semantic-retry budget is exhausted (or immediately for provider
  categories, which are never semantically retried).

Failure categories (ADR-057 D5 table): all seven typed categories
(provider_unavailable, provider_rate_limit, provider_auth, provider_config,
malformed_response, schema_validation, capability_validation) fall back to
the deterministic planner when `fallback_enabled=True`; with
`fallback_enabled=False` (strict mode) the typed failure is raised durably.
Unexpected (non-PlanningError) exceptions are NEVER retried or fallen back -
they are wrapped and raised (a programming error must not silently degrade to
deterministic cognition).

Observability: planning.requested, plan.validation.passed/failed,
model.fallback (bounded metadata only - reason/category, attempts,
fallback:"deterministic"; never prompts, responses, credentials, or provider
payloads). Source markers: `last_source` = "model" | "deterministic" is read
by the engine for plan.produced / plan.versioned (ADR-057 D3).
"""

from __future__ import annotations

from typing import Any

from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.errors import PlanValidationError, PlanningError
from arion.intelligence.plan_schema import PlanSchema
from arion.intelligence.plan_validator import PlanValidator
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import ModelRouter
from arion.observability.error_boundary import (
    ErrorSource,
    classify_error_source,
    summarize_error,
)
from arion.observability.events import AuditEvent
from arion.state.models import PlanStep

# ADR-057 D5: cognition-shaped failures may be reprompted at the planner
# layer (bounded). Provider transport categories are never semantically
# retried - M1 transport retry inside the adapter owns those.
_SEMANTIC_RETRY_CATEGORIES = frozenset(
    {"malformed_response", "schema_validation", "capability_validation"}
)

# ADR-057 D5: every typed model/provider failure category is eligible for
# deterministic fallback (when enabled). Nothing else is - arbitrary
# exceptions must never silently become deterministic cognition.
_FALLBACK_CATEGORIES = frozenset(
    {
        "provider_unavailable",
        "provider_rate_limit",
        "provider_auth",
        "provider_config",
        "malformed_response",
        "schema_validation",
        "capability_validation",
    }
)


class RealModelPlanner:
    """A Planner implementation that uses a ModelRouter for structured plans,
    with ADR-057 M3 bounded semantic retry + deterministic fallback."""

    def __init__(
        self,
        router: ModelRouter,
        events: Any | None = None,
        fallback_enabled: bool = True,
        semantic_max_retries: int = 2,
    ):
        self.router = router
        self.events = events  # duck-typed EventLogger (any object with .emit)
        self.fallback_enabled = fallback_enabled
        self.semantic_max_retries = semantic_max_retries
        self.last_transformation = None  # PlanTransformation | None (audit, ADR-013)
        self.last_source: str | None = None  # "model" | "deterministic" (ADR-057 D3)

    def plan(
        self,
        goal_description: str,
        task_id: str,
        registry: CapabilityRegistry,
        context: Any | None = None,
    ) -> list[PlanStep]:
        catalog = registry.capabilities_summary()
        self._emit("planning.requested", task_id=task_id, detail={"goal": goal_description[:200]})
        # Memory context is informational: a bounded digest handed to the model.
        # It can never authorize anything - the validator + policy decide that.
        router_context: dict[str, Any] = {"task_id": task_id}
        if context is not None and hasattr(context, "digest"):
            try:
                router_context["memory"] = context.digest()
            except Exception:
                pass
        self.last_transformation = None
        self.last_source = None

        attempt = 0
        while True:
            try:
                schema: PlanSchema = self.router.plan_structured(goal_description, catalog, router_context)
                steps = PlanValidator(registry).validate(schema)
                break
            except PlanningError as exc:
                category = exc.category
                # M3 semantic retry (bounded, planner/cognition boundary):
                # malformed/schema/capability failures are reprompted with the
                # SAME goal + catalog. Never emits model.retry (that is M1
                # transport retry, owned by the provider adapter).
                if category in _SEMANTIC_RETRY_CATEGORIES and attempt < self.semantic_max_retries:
                    attempt += 1
                    continue
                # Final model-path failure: emit the existing validation-failed
                # audit, then fall back (when permitted) or fail durably with
                # the typed category.
                summary = summarize_error(
                    exc,
                    source=classify_error_source(exc),
                    category=category,
                )
                self._emit(
                    "plan.validation.failed",
                    task_id=task_id,
                    success=False,
                    detail=summary.to_event_detail(),
                )
                if self.fallback_enabled and category in _FALLBACK_CATEGORIES:
                    return self._fallback_to_deterministic(
                        goal_description, task_id, registry, context,
                        category, attempts=attempt + 1,
                    )
                raise
            except Exception as exc:  # unknown router/validator text is untrusted here
                # Unexpected programming errors are NEVER retried or fallen
                # back: wrapping + durable failure only.
                summary = summarize_error(
                    exc,
                    source=ErrorSource.EXTERNAL,
                    category="unknown",
                )
                self._emit(
                    "plan.validation.failed",
                    task_id=task_id,
                    success=False,
                    detail=summary.to_event_detail(),
                )
                raise PlanValidationError(summary.message) from exc

        self.last_source = "model"
        self._emit("plan.validation.passed", task_id=task_id, detail={"steps": len(steps)})

        # Memory-driven guidance applied AFTER validation (registry-aware,
        # non-mutating, auditable). Informational only - authorization decides.
        if context is not None and getattr(context, "guidance", None):
            from arion.memory.guidance import apply_guidance_to_steps, registry_resource_param

            transformation = apply_guidance_to_steps(
                steps,
                context.guidance,
                resource_param_resolver=lambda cap, act: registry_resource_param(registry, cap, act),
                action_meta_resolver=lambda cap, act: registry.action_spec(cap, act),
            )
            self.last_transformation = transformation
            steps = transformation.transformed
        return steps

    def _fallback_to_deterministic(
        self,
        goal_description: str,
        task_id: str,
        registry: CapabilityRegistry,
        context: Any | None,
        category: str,
        attempts: int,
    ) -> list[PlanStep]:
        """ADR-057 D5: deterministic fallback after model-path failure.

        Emits the bounded `model.fallback` audit event (reason/category,
        attempts, fallback:"deterministic" - never prompts, responses,
        credentials, or provider payloads), then produces a plan with the
        DeterministicPlanner. The fallback plan returns to the caller as an
        ORDINARY plan and enters the SAME engine pipeline (status
        normalization, immutable plan version, live authorization,
        execution) - the model never influenced it, so a failed or
        adversarial model response carries no authority weight.
        """
        self._emit(
            "model.fallback",
            task_id=task_id,
            detail={
                "reason": category,
                "attempts": attempts,
                "fallback": "deterministic",
            },
        )
        planner = DeterministicPlanner()
        steps = planner.plan(goal_description, task_id, registry, context)
        self.last_source = "deterministic"
        self.last_transformation = planner.last_transformation
        return steps

    def required_capabilities(self, goal_description: str) -> set[str]:
        """Declare the capabilities this model-backed planner needs (ADR-018).

        The heuristic mirrors the deterministic planner's templates so the
        gate is consistent; the ACTUAL produced plan is additionally validated
        by PlanValidator against the live registry - a model-proposed
        unregistered capability is rejected before execution. Never returns an
        empty set to mean 'unknown' (fail closed)."""
        from arion.intelligence.planner import planner_requirements

        return planner_requirements(goal_description)

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
