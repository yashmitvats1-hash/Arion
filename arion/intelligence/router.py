"""Intelligence layer: ModelRouter abstraction (ADR-005, ADR-011).

The router is the only place that knows about model providers. It exposes a
minimal, provider-neutral interface: free-form `generate` and the structured
`plan_structured` path which returns a validated PlanSchema (never free-form
prose). Provider-specific types never leak outside this module.

The default deterministic mode requires no model at all, so every other layer
stays fully testable and functional without an LLM. Future providers implement
the same protocol (see arion/intelligence/providers/).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from arion.intelligence.errors import ModelPlanError  # noqa: F401  (re-exported for compatibility)
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema, StructuredStep
from arion.state.models import VerificationPolicy


class ModelRouter(Protocol):
    """Minimal contract for model-backed intelligence (provider-neutral)."""

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """Generate free-form text for the given prompt."""
        ...

    def plan_structured(self, goal: str, capabilities: list[dict[str, Any]], context: dict[str, Any]) -> PlanSchema:
        """Produce a structured plan for a goal, given the live capability
        catalog. The model proposes; the caller validates and authorizes.

        Returns a PlanSchema (already structurally valid) or raises
        ModelPlanError. Never returns free-form prose."""
        ...


class DeterministicRouter:
    """No-LLM router: deterministic structured planning + canned free-form text.

    Keeps the whole orchestration spine functional and testable offline
    (ADR-008). `plan_structured` produces a valid PlanSchema from the
    capability catalog - the same structured path a real provider uses.
    """

    def __init__(self, planner: Any):
        self._planner = planner

    def generate(self, prompt: str, **kwargs: Any) -> str:
        # Very limited: used only for verification summaries etc. in deterministic mode.
        if "summary" in prompt.lower():
            return "[deterministic summary placeholder]"
        return "[deterministic response]"

    def planner(self) -> Any:
        return self._planner

    def plan_structured(self, goal: str, capabilities: list[dict[str, Any]], context: dict[str, Any]) -> PlanSchema:
        """Deterministic structured planning against the capability catalog."""
        catalog = {c["name"] for c in capabilities}
        if "filesystem.read" not in catalog:
            raise ModelPlanError("capability 'filesystem.read' is not present in the catalog")

        text = goal.lower().strip()
        if "summarize" in text or "explore" in text or "inspect" in text:
            steps = [
                StructuredStep(
                    intent="list root",
                    capability="filesystem.read",
                    action="list",
                    params={"path": "."},
                    verification=VerificationPolicy("non_empty"),
                ),
                StructuredStep(
                    intent="read key files",
                    capability="filesystem.read",
                    action="read",
                    params={"path": "README.md"},
                    verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
                ),
            ]
            return PlanSchema(version=PLAN_SCHEMA_VERSION, intent=goal, steps=steps)

        m = re.search(r"(\S+\.\w+)", goal)
        if m:
            return PlanSchema(
                version=PLAN_SCHEMA_VERSION,
                intent=goal,
                steps=[
                    StructuredStep(
                        intent="read file",
                        capability="filesystem.read",
                        action="read",
                        params={"path": m.group(1)},
                        verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
                    )
                ],
            )

        raise ModelPlanError(f"goal not decomposable by DeterministicRouter: {goal!r}")
