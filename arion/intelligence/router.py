"""Intelligence layer: ModelRouter abstraction.

The router is the only place that knows about model providers. It exposes a
minimal, provider-agnostic interface (generate/complete) plus an optional
'planner' hook. The default deterministic mode requires no model at all, so
every other layer stays fully testable and functional without an LLM.

Provider-specific code must never leak outside this module (ADR-005).
"""

from __future__ import annotations

from typing import Any, Protocol


class ModelRouter(Protocol):
    """Minimal contract for model-backed intelligence."""

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """Generate text for the given prompt (no system history management)."""
        ...

    def planner(self) -> Any:
        """Return the active Planner instance."""
        ...


class DeterministicRouter:
    """No-LLM router: deterministic responses for fixed prompts, deterministic planner.

    Keeps the whole orchestration spine functional and testable offline.
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
