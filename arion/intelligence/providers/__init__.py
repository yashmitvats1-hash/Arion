"""Provider adapters behind the ModelRouter abstraction (ADR-005, ADR-011).

No provider-specific types leak outside this package. Adapters produce
structured PlanSchema objects; they never authorize anything.
"""

from arion.intelligence.providers.openai_compat import (
    DEFAULT_BASE_URL,
    PLANNING_SYSTEM_PROMPT,
    OpenAICompatModelRouter,
)

__all__ = ["DEFAULT_BASE_URL", "OpenAICompatModelRouter", "PLANNING_SYSTEM_PROMPT"]
