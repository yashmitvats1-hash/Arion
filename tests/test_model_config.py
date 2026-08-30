"""M1: model configuration surface (ADR-057 D1/D8).

`ModelProviderConfig`, `load_model_config()` environment parsing, the
`build_router` factory, malformed-configuration typed failures, and
credential redaction. No provider configured must mean the deterministic
spine is untouched (`build_router` returns None).
"""

import pytest

from arion.intelligence.config import ModelProviderConfig, load_model_config
from arion.intelligence.errors import ProviderConfigurationError
from arion.intelligence.providers import (
    PROVIDER_REGISTRY,
    OpenAICompatModelRouter,
    build_router,
)


# --- no provider configured -> deterministic spine unchanged ---


def test_default_config_is_disabled():
    config = load_model_config({})
    assert config.enabled is False
    assert config.provider is None
    assert config.timeout_seconds == 60.0
    assert config.max_retries == 2
    assert config.retry_backoff_base == 1.0
    assert config.retry_backoff_max == 8.0
    assert config.fallback_enabled is True
    assert config.reflection_enabled is True
    assert build_router(config) is None


def test_build_router_none_when_disabled():
    assert build_router(None) is None
    assert build_router(ModelProviderConfig()) is None
    assert build_router(ModelProviderConfig(provider=None)) is None
    assert build_router(ModelProviderConfig(provider="")) is None
    assert build_router(ModelProviderConfig(provider="none")) is None
    assert build_router(ModelProviderConfig(provider="NONE")) is None
    assert build_router(ModelProviderConfig(provider="none", model="m")) is None


def test_disabled_config_never_requires_model():
    # provider unset/"none" must not force a model name
    assert ModelProviderConfig(provider=None).enabled is False
    assert ModelProviderConfig(provider="none").enabled is False


# --- environment parsing ---


def test_load_model_config_full():
    env = {
        "ARION_LLM_PROVIDER": "openai-compatible",
        "ARION_LLM_MODEL": "gpt-test-1",
        "ARION_LLM_BASE_URL": "https://llm.example.test/v1",
        "ARION_LLM_API_KEY": "sk-test-secret-123",
        "ARION_LLM_TIMEOUT_SECONDS": "30",
        "ARION_LLM_MAX_RETRIES": "3",
        "ARION_LLM_FALLBACK": "0",
        "ARION_LLM_REFLECTION": "false",
    }
    config = load_model_config(env)
    assert config.enabled is True
    assert config.provider == "openai-compatible"
    assert config.model == "gpt-test-1"
    assert config.base_url == "https://llm.example.test/v1"
    assert config.api_key == "sk-test-secret-123"
    assert config.timeout_seconds == 30.0
    assert config.max_retries == 3
    assert config.fallback_enabled is False
    assert config.reflection_enabled is False


def test_load_model_config_full_includes_output_bounds():
    env = {
        "ARION_LLM_PROVIDER": "openai-compatible",
        "ARION_LLM_MODEL": "m",
        "ARION_LLM_MAX_RESPONSE_BYTES": "65536",
        "ARION_LLM_MAX_JSON_DEPTH": "8",
        "ARION_LLM_MAX_PLAN_STEPS": "50",
        "ARION_LLM_MAX_PARAMS_PER_STEP": "16",
        "ARION_LLM_MAX_STEP_STRING": "512",
    }
    config = load_model_config(env)
    assert config.max_response_bytes == 65536
    assert config.max_json_depth == 8
    assert config.max_plan_steps == 50
    assert config.max_params_per_step == 16
    assert config.max_step_string == 512


def test_default_output_bounds_match_plan_schema_constants():
    from arion.intelligence.plan_schema import (
        MAX_JSON_DEPTH,
        MAX_MODEL_RESPONSE_BYTES,
        MAX_PARAMS_PER_STEP,
        MAX_PLAN_STEPS,
        MAX_STEP_STRING,
    )

    config = load_model_config({})
    assert config.max_response_bytes == MAX_MODEL_RESPONSE_BYTES
    assert config.max_json_depth == MAX_JSON_DEPTH
    assert config.max_plan_steps == MAX_PLAN_STEPS
    assert config.max_params_per_step == MAX_PARAMS_PER_STEP
    assert config.max_step_string == MAX_STEP_STRING


@pytest.mark.parametrize("env_name", [
    "ARION_LLM_MAX_RESPONSE_BYTES",
    "ARION_LLM_MAX_JSON_DEPTH",
    "ARION_LLM_MAX_PLAN_STEPS",
    "ARION_LLM_MAX_PARAMS_PER_STEP",
    "ARION_LLM_MAX_STEP_STRING",
])
@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5", ""])
def test_malformed_output_bound_env_rejected(env_name, bad):
    env = {"ARION_LLM_PROVIDER": "openai-compatible", "ARION_LLM_MODEL": "m", env_name: bad}
    if bad == "":
        # empty means "use default" — must NOT raise
        assert load_model_config(env).enabled is True
        return
    with pytest.raises(ProviderConfigurationError, match=env_name):
        load_model_config(env)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_response_bytes": 0},
        {"max_response_bytes": -1},
        {"max_json_depth": 0},
        {"max_plan_steps": -5},
        {"max_params_per_step": 0},
        {"max_step_string": 1.5},
        {"max_step_string": True},
        {"max_plan_steps": "100"},
    ],
)
def test_invalid_output_bound_config_values_rejected(kwargs):
    with pytest.raises(ProviderConfigurationError):
        ModelProviderConfig(provider="openai-compatible", model="m", **kwargs)


def test_env_bool_variants():
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        config = load_model_config({"ARION_LLM_FALLBACK": truthy})
        assert config.fallback_enabled is True
    for falsy in ("0", "false", "FALSE", "no", "off"):
        config = load_model_config({"ARION_LLM_REFLECTION": falsy})
        assert config.reflection_enabled is False


def test_env_empty_values_use_defaults():
    env = {
        "ARION_LLM_PROVIDER": "openai-compatible",
        "ARION_LLM_MODEL": "m",
        "ARION_LLM_TIMEOUT_SECONDS": "",
        "ARION_LLM_MAX_RETRIES": "",
    }
    config = load_model_config(env)
    assert config.timeout_seconds == 60.0
    assert config.max_retries == 2


# --- build_router factory ---


def test_build_router_wires_config():
    config = ModelProviderConfig(
        provider="openai-compatible",
        model="m1",
        base_url="https://x.test/v1",
        api_key="sk-secret",
        timeout_seconds=7.0,
        max_retries=4,
        retry_backoff_base=0.5,
        retry_backoff_max=4.0,
        max_response_bytes=8192,
        max_json_depth=6,
        max_plan_steps=20,
        max_params_per_step=8,
        max_step_string=256,
    )
    router = build_router(config)
    assert isinstance(router, OpenAICompatModelRouter)
    assert router.model == "m1"
    assert router.base_url == "https://x.test/v1"
    assert router.api_key == "sk-secret"
    assert router.timeout == 7.0
    assert router.max_retries == 4
    assert router.retry_backoff_base == 0.5
    assert router.retry_backoff_max == 4.0
    assert router.max_response_bytes == 8192
    assert router.max_json_depth == 6
    assert router.max_plan_steps == 20
    assert router.max_params_per_step == 8
    assert router.max_step_string == 256


def test_provider_registry_contains_openai_compatible():
    assert "openai-compatible" in PROVIDER_REGISTRY
    assert PROVIDER_REGISTRY["openai-compatible"] is OpenAICompatModelRouter


def test_unknown_provider_fails_closed():
    config = ModelProviderConfig(provider="anthropic", model="claude")
    with pytest.raises(ProviderConfigurationError, match="unknown model provider"):
        build_router(config)
    # the error names the supported set so operators can self-correct
    with pytest.raises(ProviderConfigurationError, match="openai-compatible"):
        build_router(config)


def test_provider_case_insensitive():
    router = build_router(
        ModelProviderConfig(provider="OpenAI-Compatible", model="m")
    )
    assert isinstance(router, OpenAICompatModelRouter)


# --- malformed configuration -> typed, actionable errors ---


def test_enabled_provider_requires_model():
    with pytest.raises(ProviderConfigurationError, match="requires a model"):
        ModelProviderConfig(provider="openai-compatible")


def test_malformed_env_timeout():
    with pytest.raises(ProviderConfigurationError, match="ARION_LLM_TIMEOUT_SECONDS"):
        load_model_config({"ARION_LLM_TIMEOUT_SECONDS": "abc"})


def test_malformed_env_max_retries():
    with pytest.raises(ProviderConfigurationError, match="ARION_LLM_MAX_RETRIES"):
        load_model_config({"ARION_LLM_MAX_RETRIES": "1.5"})


def test_malformed_env_bool():
    with pytest.raises(ProviderConfigurationError, match="ARION_LLM_FALLBACK"):
        load_model_config({"ARION_LLM_FALLBACK": "maybe"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": -5},
        {"max_retries": -1},
        {"retry_backoff_base": 0},
        {"retry_backoff_base": -1},
        {"retry_backoff_max": 0.5, "retry_backoff_base": 1.0},
    ],
)
def test_invalid_config_values_rejected(kwargs):
    with pytest.raises(ProviderConfigurationError):
        ModelProviderConfig(provider="openai-compatible", model="m", **kwargs)


def test_router_constructor_rejects_invalid_retry_policy():
    with pytest.raises(ProviderConfigurationError, match="max_retries"):
        OpenAICompatModelRouter(model="m", api_key="", max_retries=-1)
    with pytest.raises(ProviderConfigurationError, match="retry_backoff_max"):
        OpenAICompatModelRouter(
            model="m", api_key="", retry_backoff_base=2.0, retry_backoff_max=1.0
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": "60"},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"max_retries": 2.5},
        {"max_retries": True},
        {"retry_backoff_base": float("nan")},
        {"retry_backoff_max": float("inf")},
    ],
)
def test_router_constructor_rejects_non_finite_or_wrong_typed(kwargs):
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatModelRouter(model="m", api_key="", **kwargs)


def test_public_surface_reexports():
    # the config surface is reachable from the intelligence package root
    from arion.intelligence import (
        ModelProviderConfig as RootConfig,
        ProviderRateLimitError,
        build_router as root_build_router,
        load_model_config as root_load,
    )

    assert RootConfig is ModelProviderConfig
    assert root_build_router is build_router
    assert root_load is load_model_config
    assert ProviderRateLimitError.__name__ == "ProviderRateLimitError"
    assert root_build_router(ModelProviderConfig()) is None


# --- credential safety ---


def test_config_repr_redacts_api_key():
    config = ModelProviderConfig(
        provider="openai-compatible", model="m", api_key="sk-super-secret"
    )
    assert "sk-super-secret" not in repr(config)
    assert "sk-super-secret" not in str(config)
    assert "<redacted>" in repr(config)


def test_exception_messages_do_not_contain_api_key():
    # unknown-provider error text stays bounded and never echoes the api_key
    with pytest.raises(ProviderConfigurationError) as ei:
        build_router(ModelProviderConfig(provider="wat", model="m", api_key="sk-abc"))
    assert "sk-abc" not in str(ei.value)
