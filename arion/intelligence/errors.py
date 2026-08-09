"""Typed planning errors (ADR-011, amended).

A useful error taxonomy so orchestration, audit events and future recovery
logic can tell WHY planning failed, without conflating categories:

- provider unavailable            (network, timeout, HTTP 5xx)
- provider authentication/config  (HTTP 401/403, missing key, HTTP 4xx)
- malformed provider response     (unparseable envelope/content)
- schema validation failure       (model returned a structurally invalid plan)
- capability/param/resource validation failure

The orchestration layer still fails the task gracefully; it just records the
typed category instead of a generic string.

Hierarchy (kept compatible with pre-existing names):
  PlanningError
   ├─ PlanValidationError            (schema + capability base)
   │   ├─ PlanSchemaValidationError  (category "schema_validation")
   │   └─ PlanCapabilityValidationError (category "capability_validation")
   └─ ModelPlanError                 (provider base)
       ├─ ProviderUnavailableError        (category "provider_unavailable")
       ├─ ProviderAuthenticationError     (category "provider_auth")
       ├─ ProviderConfigurationError      (category "provider_config")
       └─ MalformedProviderResponseError  (category "malformed_response")
"""

from __future__ import annotations


class PlanningError(Exception):
    """Base class for all intelligence/planning failures."""

    category = "planning"


class PlanValidationError(PlanningError):
    """Base for plan schema/capability validation failures."""

    category = "plan_validation"


class PlanSchemaValidationError(PlanValidationError):
    """The model produced a structurally invalid plan (bad schema/version/fields)."""

    category = "schema_validation"


class PlanCapabilityValidationError(PlanValidationError):
    """The plan references capabilities/actions/params/resources the system
    does not provide or that are incompatible with the registry."""

    category = "capability_validation"


class ModelPlanError(PlanningError):
    """Base for model-provider failures."""

    category = "model"


class ProviderUnavailableError(ModelPlanError):
    """The provider endpoint could not be reached (network/timeout/HTTP 5xx)."""

    category = "provider_unavailable"


class ProviderAuthenticationError(ModelPlanError):
    """Authentication or authorization with the provider failed (HTTP 401/403)."""

    category = "provider_auth"


class ProviderConfigurationError(ModelPlanError):
    """The provider is reachable but the request/configuration is wrong (HTTP 4xx)."""

    category = "provider_config"


class MalformedProviderResponseError(ModelPlanError):
    """The provider returned something that cannot be parsed into a plan."""

    category = "malformed_response"
