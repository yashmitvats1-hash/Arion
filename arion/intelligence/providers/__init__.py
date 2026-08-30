"""Provider adapters behind the ModelRouter abstraction (ADR-005, ADR-011,
ADR-057 D1).

No provider-specific types leak outside this package. Adapters produce
structured PlanSchema objects; they never authorize anything.

`build_router()` is the M1 factory: it maps a validated `ModelProviderConfig`
to a concrete `ModelRouter` adapter, or returns `None` when no provider is
configured — preserving the existing deterministic spine byte-for-byte
(ADR-057 D8). Runtime wiring of the router (engine/CLI opt-in) is owned by
later milestones (M3/M5).
"""

from __future__ import annotations

from typing import Any

from arion.intelligence.config import ModelProviderConfig
from arion.intelligence.errors import ProviderConfigurationError
from arion.intelligence.providers.openai_compat import (
    DEFAULT_BASE_URL,
    PLANNING_SYSTEM_PROMPT,
    OpenAICompatModelRouter,
)
from arion.intelligence.router import ModelRouter

# Registered provider adapters. A new provider is one adapter class plus one
# registry entry; nothing outside `intelligence/` changes (ADR-005, ADR-011).
PROVIDER_REGISTRY: dict[str, type[ModelRouter]] = {
    "openai-compatible": OpenAICompatModelRouter,
}

__all__ = [
    "DEFAULT_BASE_URL",
    "OpenAICompatModelRouter",
    "PLANNING_SYSTEM_PROMPT",
    "PROVIDER_REGISTRY",
    "build_router",
]


def build_router(
    config: ModelProviderConfig | None,
    sink: Any | None = None,
) -> ModelRouter | None:
    """Construct the configured model router, or None when no provider is
    configured (deterministic spine unchanged; ADR-057 D1/D8).

    `config=None` and disabled configs (provider unset/empty/"none") both
    mean "no model path". An unknown provider name fails closed with a typed
    `ProviderConfigurationError` — never a silent fallback to another
    provider. The error text is bounded and never includes credentials.
    """
    if config is None or not config.enabled:
        return None
    name = config.provider.strip().lower()
    adapter = PROVIDER_REGISTRY.get(name)
    if adapter is None:
        shown = name if len(name) <= 32 else name[:32] + "..."
        raise ProviderConfigurationError(
            f"unknown model provider {shown!r}; supported providers: {sorted(PROVIDER_REGISTRY)}"
        )
    return adapter(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
        retry_backoff_base=config.retry_backoff_base,
        retry_backoff_max=config.retry_backoff_max,
        max_response_bytes=config.max_response_bytes,
        max_json_depth=config.max_json_depth,
        max_plan_steps=config.max_plan_steps,
        max_params_per_step=config.max_params_per_step,
        max_step_string=config.max_step_string,
        sink=sink,
    )
