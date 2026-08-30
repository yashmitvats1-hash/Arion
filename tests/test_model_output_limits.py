"""M2: model-output size/depth limits (ADR-057 D2).

Provider output is treated as UNTRUSTED input. Deterministic resource bounds
(MAX_MODEL_RESPONSE_BYTES, MAX_JSON_DEPTH, MAX_PLAN_STEPS,
MAX_PARAMS_PER_STEP, MAX_STEP_STRING) are enforced before any model-produced
content can progress toward validation/authorization. Oversized or
pathologically nested output must produce a deterministic TYPED failure —
never retried, never fallback, never persisted, never a crash.

All tests use fake in-memory transports. No live provider dependency.
"""

import json

import pytest

from arion.intelligence.errors import (
    MalformedProviderResponseError,
    PlanSchemaValidationError,
    PlanningError,
    ProviderConfigurationError,
)
from arion.intelligence.plan_schema import (
    MAX_JSON_DEPTH,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_PARAMS_PER_STEP,
    MAX_PLAN_STEPS,
    MAX_STEP_STRING,
    PLAN_SCHEMA_VERSION,
    PlanSchema,
    json_depth,
    json_text_depth,
)
from arion.intelligence.providers import OpenAICompatModelRouter
from arion.observability.events import EVENT_KINDS


def _step(index: int, **overrides) -> dict:
    step = {
        "intent": f"step {index}",
        "capability": "filesystem.read",
        "action": "read",
        "params": {"path": "README.md"},
        "verification": {"policy": "non_empty"},
    }
    step.update(overrides)
    return step


def _plan(steps=None, intent="Inspect this repository") -> dict:
    return {
        "version": PLAN_SCHEMA_VERSION,
        "intent": intent,
        "steps": steps if steps is not None else [_step(0), _step(1)],
    }


def _ok_envelope(plan_dict: dict) -> str:
    """OpenAI-compatible 200 envelope: content is the plan JSON string."""
    return json.dumps({"choices": [{"message": {"content": json.dumps(plan_dict)}}]})


def _raw_content_envelope(content_text: str) -> str:
    """Envelope whose content field is the RAW text (single encoding)."""
    return json.dumps({"choices": [{"message": {"content": content_text}}]})


class CountingTransport:
    def __init__(self, response: str, status: int = 200):
        self.response = response
        self.status = status
        self.calls = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        return self.status, self.response


class SequenceTransport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        item = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class MemorySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class RecorderSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _router(transport, *, sink=None, max_retries=0, **limits):
    return OpenAICompatModelRouter(
        model="test-model",
        base_url="https://example.test/v1",
        api_key="",
        transport=transport,
        sink=sink,
        max_retries=max_retries,
        sleep=RecorderSleep(),
        **limits,
    )


# --- defaults ---


def test_default_limits_are_the_adr_057_proposed_values():
    assert MAX_MODEL_RESPONSE_BYTES == 262_144
    assert MAX_JSON_DEPTH == 10
    assert MAX_PLAN_STEPS == 100
    assert MAX_PARAMS_PER_STEP == 32
    assert MAX_STEP_STRING == 2000


def test_comfortably_below_limit_accepted():
    transport = CountingTransport(_ok_envelope(_plan()))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert schema.intent == "Inspect this repository"
    assert len(schema.steps) == 2
    assert transport.calls == 1


def test_valid_existing_plan_behaves_identically():
    # pre-M2 plans (small, shallow) parse identically under the new caps
    plan = _plan()
    schema = PlanSchema.from_dict(plan)
    assert schema.intent == plan["intent"]
    assert [s.action for s in schema.steps] == ["read", "read"]


# --- raw response byte size ---


def test_response_exactly_at_byte_limit_accepted():
    envelope = _ok_envelope(_plan())
    transport = CountingTransport(envelope)
    router = _router(transport, max_response_bytes=len(envelope))
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps) == 2


def test_response_one_byte_over_rejected():
    envelope = _ok_envelope(_plan(intent="MARKER-SECRET-123"))
    transport = CountingTransport(envelope)
    router = _router(transport, max_response_bytes=len(envelope) - 1)
    with pytest.raises(MalformedProviderResponseError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "exceeds maximum size" in str(ei.value)
    # the body content never appears in the error, only the size figures
    assert "MARKER-SECRET-123" not in str(ei.value)


def test_extremely_large_response_rejected():
    transport = CountingTransport("x" * (MAX_MODEL_RESPONSE_BYTES * 4))
    router = _router(transport)
    with pytest.raises(MalformedProviderResponseError, match="exceeds maximum size"):
        router.plan_structured("Inspect this repository", [], {})
    assert transport.calls == 1


def test_large_valid_looking_plan_rejected():
    # a structurally valid but large plan cannot bypass the byte cap
    plan = _plan(steps=[_step(i) for i in range(200)])
    envelope = _ok_envelope(plan)
    assert len(envelope) > 4096
    transport = CountingTransport(envelope)
    router = _router(transport, max_response_bytes=4096)
    with pytest.raises(MalformedProviderResponseError, match="exceeds maximum size"):
        router.plan_structured("Inspect this repository", [], {})


# --- plan step count ---


def test_steps_exactly_at_default_limit_accepted():
    plan = _plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS)])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps) == MAX_PLAN_STEPS


def test_steps_one_over_default_limit_rejected():
    plan = _plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "'steps' exceeds maximum size" in str(ei.value)
    assert str(MAX_PLAN_STEPS + 1) in str(ei.value)


def test_steps_at_custom_limit_boundary():
    plan_ok = _plan(steps=[_step(i) for i in range(2)])
    router_ok = _router(CountingTransport(_ok_envelope(plan_ok)), max_plan_steps=2)
    assert len(router_ok.plan_structured("g", [], {}).steps) == 2

    plan_bad = _plan(steps=[_step(i) for i in range(3)])
    router_bad = _router(CountingTransport(_ok_envelope(plan_bad)), max_plan_steps=2)
    with pytest.raises(PlanSchemaValidationError, match="'steps' exceeds maximum size"):
        router_bad.plan_structured("g", [], {})


# --- params per step ---


def test_params_exactly_at_default_limit_accepted():
    params = {f"k{i}": "v" for i in range(MAX_PARAMS_PER_STEP)}
    plan = _plan(steps=[_step(0, params=params)])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps[0].params) == MAX_PARAMS_PER_STEP


def test_params_one_over_default_limit_rejected():
    params = {f"k{i}": "v" for i in range(MAX_PARAMS_PER_STEP + 1)}
    plan = _plan(steps=[_step(0, params=params)])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "'params' exceeds maximum size" in str(ei.value)


# --- individual strings ---


def test_step_string_exactly_at_limit_accepted():
    plan = _plan(steps=[_step(0, intent="i" * MAX_STEP_STRING)])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps[0].intent) == MAX_STEP_STRING


def test_step_string_one_over_rejected():
    plan = _plan(steps=[_step(0, intent="i" * (MAX_STEP_STRING + 1))])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "'intent' exceeds maximum length" in str(ei.value)


def test_param_string_value_over_rejected():
    plan = _plan(steps=[_step(0, params={"path": "p" * (MAX_STEP_STRING + 1)})])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "param 'path' exceeds maximum length" in str(ei.value)


def test_plan_intent_over_rejected():
    plan = _plan(intent="x" * (MAX_STEP_STRING + 1))
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="'intent' exceeds maximum length"):
        router.plan_structured("Inspect this repository", [], {})


def test_step_capability_string_over_rejected():
    plan = _plan(steps=[_step(0, capability="c" * (MAX_STEP_STRING + 1))])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="'capability' exceeds maximum length"):
        router.plan_structured("Inspect this repository", [], {})


def test_step_action_string_over_rejected():
    plan = _plan(steps=[_step(0, action="a" * (MAX_STEP_STRING + 1))])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="'action' exceeds maximum length"):
        router.plan_structured("Inspect this repository", [], {})


def test_param_key_over_rejected():
    plan = _plan(steps=[_step(0, params={("k" * (MAX_STEP_STRING + 1)): "v"})])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="param key exceeds maximum length"):
        router.plan_structured("Inspect this repository", [], {})


def test_verification_key_over_rejected():
    long_key = "k" * (MAX_STEP_STRING + 1)
    plan = _plan(steps=[_step(
        0,
        verification={"policy": "schema_keys", "args": {"keys": [long_key]}},
    )])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="verification key exceeds maximum length"):
        router.plan_structured("Inspect this repository", [], {})


# --- JSON nesting depth ---


def _nested_params(n: int) -> dict:
    """params value nested `n` dicts deep: {"path": {"a": {"a": ... }}}."""
    inner: dict = {"leaf": "v"}
    for _ in range(n):
        inner = {"a": inner}
    return {"path": inner}


def test_depth_exactly_at_default_limit_accepted():
    # plan depth = 1(top) + steps(2) + step(3) + params(4) + (n+1) nested
    # dicts; n = MAX_JSON_DEPTH - 5 yields exactly MAX_JSON_DEPTH
    plan = _plan(steps=[_step(0, params=_nested_params(MAX_JSON_DEPTH - 5))])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps) == 1


def test_depth_one_over_default_limit_rejected():
    plan = _plan(steps=[_step(0, params=_nested_params(MAX_JSON_DEPTH - 4))])
    transport = CountingTransport(_ok_envelope(plan))
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "nesting depth exceeds maximum" in str(ei.value)


def test_depth_near_boundary_accepted_rejected():
    # depth 9 and 10 accepted (default MAX_JSON_DEPTH=10); depth 11 rejected
    for n in (MAX_JSON_DEPTH - 6, MAX_JSON_DEPTH - 5):
        plan = _plan(steps=[_step(0, params=_nested_params(n))])
        router = _router(CountingTransport(_ok_envelope(plan)))
        schema = router.plan_structured("g", [], {})
        assert len(schema.steps) == 1
    plan = _plan(steps=[_step(0, params=_nested_params(MAX_JSON_DEPTH - 4))])
    router = _router(CountingTransport(_ok_envelope(plan)))
    with pytest.raises(PlanSchemaValidationError, match="nesting depth exceeds maximum"):
        router.plan_structured("g", [], {})


def test_deeply_nested_envelope_rejected_no_recursion_error():
    # pathological ENVELOPE nesting would blow up json.loads via RecursionError;
    # the raw-text scan bounds it first with a typed failure
    transport = CountingTransport("[" * 100_000 + "]" * 100_000)
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="nesting depth exceeds maximum"):
        router.plan_structured("Inspect this repository", [], {})


def test_deeply_nested_plan_content_rejected_no_recursion_error():
    # pathological nesting INSIDE the content string is invisible to the
    # envelope scan (brackets live in a string literal); the content-level
    # scan bounds it before json.loads(content). Deep ARRAYS are compact
    # enough (200KB) to stay under the byte cap while carrying depth 100k.
    deep_content = "[" * 100_000 + "]" * 100_000
    envelope = _raw_content_envelope(deep_content)
    assert len(envelope) < MAX_MODEL_RESPONSE_BYTES
    transport = CountingTransport(envelope)
    router = _router(transport)
    with pytest.raises(PlanSchemaValidationError, match="nesting depth exceeds maximum"):
        router.plan_structured("Inspect this repository", [], {})


def test_json_depth_helpers_agree():
    plan = _plan(steps=[_step(0, params=_nested_params(5))])
    text = json.dumps(plan)
    assert json_text_depth(text) == json_depth(json.loads(text)) == 10
    assert json_depth({"a": {"b": {"c": 1}}}) == 3
    assert json_text_depth('{"a": {"b": {"c": 1}}}') == 3
    assert json_depth("scalar") == 0
    assert json_text_depth('{"s": "{[}["}') == 1  # brackets inside strings ignored


def test_plan_schema_from_dict_enforces_depth_iteratively():
    plan = _plan(steps=[_step(0, params=_nested_params(100_000))])
    with pytest.raises(PlanSchemaValidationError, match="nesting depth exceeds maximum"):
        PlanSchema.from_dict(plan)


# --- malformed + size/depth interaction ---


def test_malformed_output_preserves_existing_behavior():
    # envelope-level malformed body -> existing envelope error path
    transport = CountingTransport("this is not json")
    router = _router(transport)
    with pytest.raises(MalformedProviderResponseError, match="malformed JSON"):
        router.plan_structured("Inspect this repository", [], {})
    # content-level malformed (valid envelope) -> existing content error path
    transport2 = CountingTransport(_raw_content_envelope("this is not json"))
    router2 = _router(transport2)
    with pytest.raises(MalformedProviderResponseError, match="invalid structured plan"):
        router2.plan_structured("Inspect this repository", [], {})


def test_oversized_malformed_rejected_by_size_first():
    # order: byte size -> depth -> parse; oversized malformed hits the size cap
    transport = CountingTransport("x" * (MAX_MODEL_RESPONSE_BYTES + 1))
    router = _router(transport)
    with pytest.raises(MalformedProviderResponseError, match="exceeds maximum size"):
        router.plan_structured("Inspect this repository", [], {})


# --- failure semantics: never retried, never fallback ---


def test_rejection_is_not_retried():
    envelope = _ok_envelope(_plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)]))
    transport = CountingTransport(envelope)
    sink = MemorySink()
    router = _router(transport, sink=sink, max_retries=5)
    with pytest.raises(PlanSchemaValidationError):
        router.plan_structured("Inspect this repository", [], {})
    assert transport.calls == 1  # a single attempt; no transport retries
    assert all(e.kind != "model.retry" for e in sink.events)


def test_byte_limit_rejection_is_not_retried():
    envelope = _ok_envelope(_plan())
    transport = CountingTransport(envelope)
    sink = MemorySink()
    router = _router(transport, sink=sink, max_retries=5, max_response_bytes=10)
    with pytest.raises(MalformedProviderResponseError):
        router.plan_structured("Inspect this repository", [], {})
    assert transport.calls == 1
    assert all(e.kind != "model.retry" for e in sink.events)


def test_rejection_does_not_invoke_fallback():
    # fallback is M3-owned; there is no model.fallback event kind today
    assert "model.fallback" not in EVENT_KINDS
    plan = _plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)])
    router = _router(CountingTransport(_ok_envelope(plan)))
    with pytest.raises(PlanningError):  # typed planning failure, nothing else
        router.plan_structured("Inspect this repository", [], {})


def test_retry_then_oversize_still_typed():
    # M1 retry runs for the transient 500; the subsequent oversized 200 is a
    # deterministic validation failure, not another retry
    envelope = _ok_envelope(_plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)]))
    transport = SequenceTransport([(500, "boom"), (200, envelope)])
    sink = MemorySink()
    router = _router(transport, sink=sink, max_retries=2)
    with pytest.raises(PlanSchemaValidationError):
        router.plan_structured("Inspect this repository", [], {})
    assert transport.calls == 2
    retries = [e for e in sink.events if e.kind == "model.retry"]
    assert len(retries) == 1  # the 500 was retried once; the oversize was not


# --- no sensitive payload leakage ---


def test_no_sensitive_payload_leakage():
    secret = "sk-super-secret-token"
    plan = _plan(
        intent=f"do {secret}",
        steps=[_step(0, intent=f"read {secret}", params={"path": secret})],
    )
    envelope = _ok_envelope(plan)
    transport = CountingTransport(envelope)
    sink = MemorySink()
    router = _router(transport, sink=sink, max_response_bytes=len(envelope) - 1)
    with pytest.raises(MalformedProviderResponseError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert secret not in str(ei.value)
    raw_events = json.dumps([e.to_dict() for e in sink.events])
    assert secret not in raw_events


def test_depth_rejection_message_has_no_content():
    plan = _plan(steps=[_step(0, params=_nested_params(MAX_JSON_DEPTH - 3))])
    transport = CountingTransport(_ok_envelope(plan))
    sink = MemorySink()
    router = _router(transport, sink=sink)
    with pytest.raises(PlanSchemaValidationError) as ei:
        router.plan_structured("Inspect this repository", [], {})
    assert "README.md" not in str(ei.value)
    raw_events = json.dumps([e.to_dict() for e in sink.events])
    assert "README.md" not in raw_events


# --- deterministic behavior ---


def test_deterministic_rejection_across_repeated_runs():
    plan = _plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)])
    results = []
    for _ in range(3):
        router = _router(CountingTransport(_ok_envelope(plan)))
        try:
            router.plan_structured("Inspect this repository", [], {})
        except PlanSchemaValidationError as exc:
            results.append((type(exc).__name__, str(exc)))
        else:
            results.append(None)
    assert all(r == results[0] for r in results)
    assert "exceeds maximum size" in results[0][1]


def test_repeated_adversarial_attempts_bounded_then_valid():
    bad = _plan(steps=[_step(i) for i in range(MAX_PLAN_STEPS + 1)])
    good = _plan()
    for _ in range(3):
        router = _router(CountingTransport(_ok_envelope(bad)))
        with pytest.raises(PlanSchemaValidationError):
            router.plan_structured("Inspect this repository", [], {})
    # the router is not corrupted by adversarial attempts
    transport = CountingTransport(_ok_envelope(good))
    router = _router(transport)
    schema = router.plan_structured("Inspect this repository", [], {})
    assert len(schema.steps) == 2


def test_generate_oversized_rejected():
    # generate shares _chat, so the byte bound applies there too
    transport = CountingTransport("x" * (MAX_MODEL_RESPONSE_BYTES + 1))
    router = _router(transport)
    with pytest.raises(MalformedProviderResponseError, match="exceeds maximum size"):
        router.generate("hi")


# --- constructor / config validation ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_response_bytes": 0},
        {"max_response_bytes": -1},
        {"max_json_depth": 0},
        {"max_plan_steps": -5},
        {"max_params_per_step": 0},
        {"max_step_string": -1},
        {"max_step_string": 1.5},
        {"max_plan_steps": True},
    ],
)
def test_router_constructor_rejects_invalid_limits(kwargs):
    with pytest.raises(ProviderConfigurationError):
        _router(CountingTransport("{}"), **kwargs)


def test_config_limits_wired_through_build_router():
    from arion.intelligence.config import ModelProviderConfig
    from arion.intelligence.providers import build_router

    config = ModelProviderConfig(
        provider="openai-compatible",
        model="m",
        api_key="",
        max_response_bytes=4096,
        max_json_depth=4,
        max_plan_steps=5,
        max_params_per_step=3,
        max_step_string=64,
    )
    router = build_router(config)
    assert router.max_response_bytes == 4096
    assert router.max_json_depth == 4
    assert router.max_plan_steps == 5
    assert router.max_params_per_step == 3
    assert router.max_step_string == 64
