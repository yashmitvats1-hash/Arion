"""Model-backing configuration surface (ADR-057 D1, D8; M1/M2).

M1 implemented the configuration surface: the `ModelProviderConfig`
dataclass, `load_model_config()` (environment parsing), and explicit typed
validation. M2 adds the output-bound fields (`max_response_bytes`,
`max_json_depth`, `max_plan_steps`, `max_params_per_step`, `max_step_string`)
whose defaults are the plan_schema constants (ADR-057 D2). The router
factory `build_router()` lives in `arion.intelligence.providers`; runtime
composition (fallback, reflection wiring, opt-in engine plumbing) is NOT
part of M1/M2 and is owned by later milestones (M3-M5).

No provider configured must mean the existing deterministic spine, unchanged:
`enabled` is False when `provider` is unset, empty, or "none", and
`build_router` returns None in that case.

Credentials (`api_key`) are read from the environment only, are never
persisted, never logged, and never appear in `repr`/`str`/exception messages.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping

from arion.intelligence.errors import ProviderConfigurationError
from arion.intelligence.plan_schema import (
    MAX_JSON_DEPTH,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_PARAMS_PER_STEP,
    MAX_PLAN_STEPS,
    MAX_STEP_STRING,
)

# The complete ARION_LLM_* configuration surface (ADR-057 D1/D8).
ENV_PROVIDER = "ARION_LLM_PROVIDER"
ENV_MODEL = "ARION_LLM_MODEL"
ENV_BASE_URL = "ARION_LLM_BASE_URL"
ENV_API_KEY = "ARION_LLM_API_KEY"
ENV_TIMEOUT = "ARION_LLM_TIMEOUT_SECONDS"
ENV_MAX_RETRIES = "ARION_LLM_MAX_RETRIES"
ENV_FALLBACK = "ARION_LLM_FALLBACK"
ENV_REFLECTION = "ARION_LLM_REFLECTION"
# Output bounds (ADR-057 D2, M2) — defaults are the plan_schema constants.
ENV_MAX_RESPONSE_BYTES = "ARION_LLM_MAX_RESPONSE_BYTES"
ENV_MAX_JSON_DEPTH = "ARION_LLM_MAX_JSON_DEPTH"
ENV_MAX_PLAN_STEPS = "ARION_LLM_MAX_PLAN_STEPS"
ENV_MAX_PARAMS_PER_STEP = "ARION_LLM_MAX_PARAMS_PER_STEP"
ENV_MAX_STEP_STRING = "ARION_LLM_MAX_STEP_STRING"

# Values of ARION_LLM_PROVIDER that mean "no model path" (deterministic).
_NO_PROVIDER = frozenset({"", "none"})


@dataclass(frozen=True)
class ModelProviderConfig:
    """Validated, credential-safe model configuration (ADR-057 D1).

    `fallback_enabled` and `reflection_enabled` are part of the approved
    configuration surface; M1 parses and validates them but does NOT wire
    their behavior (M3 owns fallback, M4 owns reflection).
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_base: float = 1.0
    retry_backoff_max: float = 8.0
    fallback_enabled: bool = True
    reflection_enabled: bool = True
    # Output bounds (ADR-057 D2, M2); defaults are the plan_schema constants.
    max_response_bytes: int = MAX_MODEL_RESPONSE_BYTES
    max_json_depth: int = MAX_JSON_DEPTH
    max_plan_steps: int = MAX_PLAN_STEPS
    max_params_per_step: int = MAX_PARAMS_PER_STEP
    max_step_string: int = MAX_STEP_STRING

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ProviderConfigurationError(
                f"model timeout_seconds must be a finite number > 0 (got {self.timeout_seconds!r})"
            )
        if self.max_retries < 0:
            raise ProviderConfigurationError(
                f"model max_retries must be >= 0 (got {self.max_retries!r})"
            )
        if not math.isfinite(self.retry_backoff_base) or self.retry_backoff_base <= 0:
            raise ProviderConfigurationError(
                f"model retry_backoff_base must be a finite number > 0 (got {self.retry_backoff_base!r})"
            )
        if (
            not math.isfinite(self.retry_backoff_max)
            or self.retry_backoff_max < self.retry_backoff_base
        ):
            raise ProviderConfigurationError(
                f"model retry_backoff_max ({self.retry_backoff_max!r}) must be >= "
                f"retry_backoff_base ({self.retry_backoff_base!r})"
            )
        for name in (
            "max_response_bytes",
            "max_json_depth",
            "max_plan_steps",
            "max_params_per_step",
            "max_step_string",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ProviderConfigurationError(
                    f"model {name} must be a positive integer (got {value!r})"
                )
        if self.enabled and not (self.model or "").strip():
            raise ProviderConfigurationError(
                f"model provider {self.provider!r} requires a model name ({ENV_MODEL})"
            )

    @property
    def enabled(self) -> bool:
        """True when a provider is selected; False means deterministic spine."""
        name = (self.provider or "").strip().lower()
        return name not in _NO_PROVIDER

    def __repr__(self) -> str:
        # Credential-safe: api_key never appears in repr/str (ADR-057 D8).
        return (
            f"ModelProviderConfig(provider={self.provider!r}, model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key={'<redacted>' if self.api_key else None!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, max_retries={self.max_retries!r}, "
            f"retry_backoff_base={self.retry_backoff_base!r}, "
            f"retry_backoff_max={self.retry_backoff_max!r}, "
            f"fallback_enabled={self.fallback_enabled!r}, "
            f"reflection_enabled={self.reflection_enabled!r}, "
            f"max_response_bytes={self.max_response_bytes!r}, "
            f"max_json_depth={self.max_json_depth!r}, "
            f"max_plan_steps={self.max_plan_steps!r}, "
            f"max_params_per_step={self.max_params_per_step!r}, "
            f"max_step_string={self.max_step_string!r})"
        )


def load_model_config(environ: Mapping[str, str] | None = None) -> ModelProviderConfig:
    """Load the model configuration from the ARION_LLM_* environment surface.

    When `environ` is omitted, `os.environ` is read. Returns a DISABLED
    config when no provider is selected (deterministic spine unchanged).
    Malformed values fail explicitly with a typed `ProviderConfigurationError`
    naming the offending variable; the value itself is echoed only for
    non-secret variables (the API key is never parsed or echoed).
    """
    env = os.environ if environ is None else environ
    return ModelProviderConfig(
        provider=env.get(ENV_PROVIDER),
        model=env.get(ENV_MODEL),
        base_url=env.get(ENV_BASE_URL),
        api_key=env.get(ENV_API_KEY),
        timeout_seconds=_env_float(env, ENV_TIMEOUT, 60.0),
        max_retries=_env_int(env, ENV_MAX_RETRIES, 2),
        fallback_enabled=_env_bool(env, ENV_FALLBACK, True),
        reflection_enabled=_env_bool(env, ENV_REFLECTION, True),
        max_response_bytes=_env_positive_int(env, ENV_MAX_RESPONSE_BYTES, MAX_MODEL_RESPONSE_BYTES),
        max_json_depth=_env_positive_int(env, ENV_MAX_JSON_DEPTH, MAX_JSON_DEPTH),
        max_plan_steps=_env_positive_int(env, ENV_MAX_PLAN_STEPS, MAX_PLAN_STEPS),
        max_params_per_step=_env_positive_int(env, ENV_MAX_PARAMS_PER_STEP, MAX_PARAMS_PER_STEP),
        max_step_string=_env_positive_int(env, ENV_MAX_STEP_STRING, MAX_STEP_STRING),
    )


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ProviderConfigurationError(
            f"{name} must be a number (got {raw!r})"
        ) from None


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ProviderConfigurationError(
            f"{name} must be an integer (got {raw!r})"
        ) from None


def _env_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse a strictly-positive integer env var (output bounds, ADR-057 D2).

    Zero/negative values are configuration errors, not valid limits.
    """
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ProviderConfigurationError(
            f"{name} must be a positive integer (got {raw!r})"
        ) from None
    if value <= 0:
        raise ProviderConfigurationError(
            f"{name} must be a positive integer (got {raw!r})"
        )
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ProviderConfigurationError(
        f"{name} must be a boolean (1/0/true/false/yes/no/on/off; got {raw!r})"
    )
