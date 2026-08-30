"""Intelligence layer public surface."""

from arion.intelligence.config import ModelProviderConfig, load_model_config
from arion.intelligence.errors import (
    MalformedProviderResponseError,
    ModelPlanError,
    PlanCapabilityValidationError,
    PlanSchemaValidationError,
    PlanValidationError,
    PlanningError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import (
    MAX_JSON_DEPTH,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_PARAMS_PER_STEP,
    MAX_PLAN_STEPS,
    MAX_STEP_STRING,
    PLAN_SCHEMA_VERSION,
    PlanSchema,
    StructuredStep,
    json_depth,
    json_text_depth,
)
from arion.intelligence.plan_validator import PlanValidator, topo_sort_steps
from arion.intelligence.planner import DeterministicPlanner, Planner
from arion.intelligence.router import ModelRouter, DeterministicRouter
from arion.intelligence.providers import OpenAICompatModelRouter, build_router

__all__ = [
    "DeterministicPlanner",
    "DeterministicRouter",
    "MAX_JSON_DEPTH",
    "MAX_MODEL_RESPONSE_BYTES",
    "MAX_PARAMS_PER_STEP",
    "MAX_PLAN_STEPS",
    "MAX_STEP_STRING",
    "MalformedProviderResponseError",
    "ModelPlanError",
    "ModelProviderConfig",
    "ModelRouter",
    "OpenAICompatModelRouter",
    "PLAN_SCHEMA_VERSION",
    "PlanCapabilityValidationError",
    "PlanSchema",
    "PlanSchemaValidationError",
    "PlanValidationError",
    "PlanValidator",
    "Planner",
    "PlanningError",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "RealModelPlanner",
    "StructuredStep",
    "build_router",
    "json_depth",
    "json_text_depth",
    "load_model_config",
    "topo_sort_steps",
]
