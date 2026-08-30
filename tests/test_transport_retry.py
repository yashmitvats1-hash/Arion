"""M1: bounded transport-level retry (ADR-057 D1/D5, M1).

Retries live inside the provider adapter at the transport boundary:
bounded, deterministic backoff (no jitter), Retry-After honored, HTTP 429 ->
`ProviderRateLimitError`, auth/config failures never retried, observable via
`model.retry` events, and no credential leakage. Everything runs against
fake in-memory transports - no network, no credentials.
"""

import json

import pytest

from arion.intelligence.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION
from arion.intelligence.providers import OpenAICompatModelRouter
from arion.observability.events import EVENT_KINDS, AuditEvent

VALID_PLAN = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {"intent": "list", "capability": "filesystem.read", "action": "list",
         "params": {"path": "."}, "verification": {"policy": "non_empty"}},
    ],
}


def _ok_envelope(plan_dict):
    """OpenAI-compatible 200 envelope: content is the plan JSON string."""
    return json.dumps({"choices": [{"message": {"content": json.dumps(plan_dict)}}]})


class SequenceTransport:
    """Serves queued results in order; the last result repeats.

    Items are (status, text) tuples or Exception instances (raised).
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        item = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class RecorderSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _router(transport, *, sink=None, sleep=None, max_retries=2,
            retry_backoff_base=1.0, retry_backoff_max=8.0, api_key=""):
    return OpenAICompatModelRouter(
        model="test-model", base_url="https://example.test/v1", api_key=api_key,
        transport=transport, sink=sink, sleep=sleep or RecorderSleep(),
        max_retries=max_retries,
        retry_backoff_base=retry_backoff_base,
        retry_backoff_max=retry_backoff_max,
    )


def _retry_events(sink):
    return [e for e in sink.events if e.kind == "model.retry"]


# --- retryable failures -> bounded retry, then success ---


def test_transient_network_error_retried_then_success():
    transport = SequenceTransport([
        ConnectionError("refused"),
        TimeoutError("slow"),
        (200, _ok_envelope(VALID_PLAN)),
    ])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    schema = router.plan_structured("goal", [], {})
    assert schema.intent == "Inspect this repository"
    assert transport.calls == 3
    assert sleep.delays == [1.0, 2.0]  # deterministic exponential backoff


def test_http_5xx_retried_then_success():
    transport = SequenceTransport([
        (500, "boom"), (503, "busy"), (200, _ok_envelope(VALID_PLAN)),
    ])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    schema = router.plan_structured("goal", [], {})
    assert schema.intent == "Inspect this repository"
    assert transport.calls == 3
    assert sleep.delays == [1.0, 2.0]


def test_generate_retries_transport_failure():
    transport = SequenceTransport([
        (500, "boom"),
        (200, json.dumps({"choices": [{"message": {"content": "hello"}}]})),
    ])
    router = _router(transport, max_retries=1)
    assert router.generate("hi") == "hello"
    assert transport.calls == 2


# --- retry exhaustion -> typed failure ---


def test_retry_exhaustion_raises_unavailable():
    transport = SequenceTransport([TimeoutError("slow")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    with pytest.raises(ProviderUnavailableError) as ei:
        router.plan_structured("goal", [], {})
    assert ei.value.category == "provider_unavailable"
    assert transport.calls == 3
    assert sleep.delays == [1.0, 2.0]


def test_http_5xx_exhaustion_raises_unavailable():
    transport = SequenceTransport([(503, "busy")])
    router = _router(transport, max_retries=2)
    with pytest.raises(ProviderUnavailableError, match="HTTP 503"):
        router.plan_structured("goal", [], {})


def test_max_retries_zero_is_single_attempt():
    transport = SequenceTransport([TimeoutError("slow")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=0)
    with pytest.raises(ProviderUnavailableError):
        router.plan_structured("goal", [], {})
    assert transport.calls == 1
    assert sleep.delays == []


# --- HTTP 429 -> ProviderRateLimitError ---


def test_http_429_raises_rate_limit_after_retries():
    transport = SequenceTransport([(429, "rate limited")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    with pytest.raises(ProviderRateLimitError) as ei:
        router.plan_structured("goal", [], {})
    assert ei.value.category == "provider_rate_limit"
    assert "HTTP 429" in str(ei.value)
    assert transport.calls == 3
    assert sleep.delays == [1.0, 2.0]


def test_429_retry_after_honored():
    first = ProviderRateLimitError("rate limited", retry_after_seconds=3.0)
    transport = SequenceTransport([first, (200, _ok_envelope(VALID_PLAN))])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    schema = router.plan_structured("goal", [], {})
    assert schema.intent == "Inspect this repository"
    assert sleep.delays == [3.0]  # Retry-After wins over the 1.0s backoff


def test_retry_after_capped_by_backoff_max():
    first = ProviderRateLimitError("rate limited", retry_after_seconds=60.0)
    transport = SequenceTransport([first, (200, _ok_envelope(VALID_PLAN))])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2, retry_backoff_max=8.0)
    router.plan_structured("goal", [], {})
    assert sleep.delays == [8.0]


def test_retry_after_invalid_falls_back_to_backoff():
    first = ProviderRateLimitError("rate limited", retry_after_seconds=None)
    transport = SequenceTransport([first, (200, _ok_envelope(VALID_PLAN))])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2)
    router.plan_structured("goal", [], {})
    assert sleep.delays == [1.0]


def test_raised_rate_limit_exhaustion_propagates_typed():
    # the transport keeps raising ProviderRateLimitError (e.g. Retry-After
    # observed via headers); after the retry budget the typed error propagates
    first = ProviderRateLimitError("rate limited", retry_after_seconds=2.0)
    transport = SequenceTransport([first])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=2, retry_backoff_max=8.0)
    with pytest.raises(ProviderRateLimitError):
        router.plan_structured("goal", [], {})
    assert transport.calls == 3
    assert sleep.delays == [2.0, 2.0]  # Retry-After honored on each retry


# --- non-retryable failures -> exactly one attempt, no retry ---


@pytest.mark.parametrize("status,exc", [
    (401, ProviderAuthenticationError),
    (403, ProviderAuthenticationError),
])
def test_auth_errors_never_retried(status, exc):
    transport = SequenceTransport([(status, "nope")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=5)
    with pytest.raises(exc):
        router.plan_structured("goal", [], {})
    assert transport.calls == 1
    assert sleep.delays == []


def test_config_error_never_retried():
    transport = SequenceTransport([(400, "bad request")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=5)
    with pytest.raises(ProviderConfigurationError):
        router.plan_structured("goal", [], {})
    assert transport.calls == 1
    assert sleep.delays == []


def test_non_oserror_transport_exception_never_retried():
    # a programming error in the transport is not a transient failure
    transport = SequenceTransport([ValueError("bug")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=3)
    with pytest.raises(ProviderUnavailableError):
        router.plan_structured("goal", [], {})
    assert transport.calls == 1
    assert sleep.delays == []


# --- model.retry event observability ---


def test_retry_events_emitted_with_bounded_detail():
    transport = SequenceTransport([(500, "boom"), (200, _ok_envelope(VALID_PLAN))])
    sink = RecordingSink()
    router = _router(transport, sink=sink, max_retries=2, api_key="sk-topsecret")
    router.plan_structured("goal", [], {})
    retries = _retry_events(sink)
    assert len(retries) == 1
    assert retries[0].success is True
    assert retries[0].detail["attempt"] == 1
    assert retries[0].detail["delay_ms"] == 1000
    assert retries[0].detail["category"] == "provider_unavailable"
    assert retries[0].detail["provider"] == "openai-compatible"
    assert retries[0].detail["model"] == "test-model"
    # bounded: no prompt, no provider body, no credentials
    raw = json.dumps([e.to_dict() for e in sink.events])
    assert "sk-topsecret" not in raw
    assert "boom" not in raw


def test_retry_event_category_for_429():
    transport = SequenceTransport([(429, "rate limited")])
    sink = RecordingSink()
    router = _router(transport, sink=sink, max_retries=1)
    with pytest.raises(ProviderRateLimitError):
        router.plan_structured("goal", [], {})
    retries = _retry_events(sink)
    assert len(retries) == 1
    assert retries[0].detail["category"] == "provider_rate_limit"


def test_no_retry_events_on_success():
    transport = SequenceTransport([(200, _ok_envelope(VALID_PLAN))])
    sink = RecordingSink()
    sleep = RecorderSleep()
    router = _router(transport, sink=sink, sleep=sleep, max_retries=2)
    router.plan_structured("goal", [], {})
    assert transport.calls == 1
    assert sleep.delays == []
    assert _retry_events(sink) == []


def test_model_retry_event_kind_registered():
    assert "model.retry" in EVENT_KINDS
    event = AuditEvent(kind="model.retry", success=True, detail={
        "provider": "openai-compatible", "model": "m", "attempt": 1,
        "delay_ms": 1000, "category": "provider_unavailable",
    })
    assert event.kind == "model.retry"


# --- deterministic retry policy ---


def test_backoff_capped_at_max():
    transport = SequenceTransport([TimeoutError("slow")])
    sleep = RecorderSleep()
    router = _router(transport, sleep=sleep, max_retries=4,
                     retry_backoff_base=2.0, retry_backoff_max=5.0)
    with pytest.raises(ProviderUnavailableError):
        router.plan_structured("goal", [], {})
    assert sleep.delays == [2.0, 4.0, 5.0, 5.0]


def test_retry_policy_deterministic_across_identical_runs():
    def run_once():
        transport = SequenceTransport([
            ConnectionError("refused"), (500, "boom"), (200, _ok_envelope(VALID_PLAN)),
        ])
        sleep = RecorderSleep()
        sink = RecordingSink()
        router = _router(transport, sink=sink, sleep=sleep, max_retries=2)
        router.plan_structured("goal", [], {})
        return transport.calls, tuple(sleep.delays), [e.detail for e in _retry_events(sink)]

    assert run_once() == run_once()


# --- no credential leakage ---


def test_exception_messages_never_contain_api_key():
    transport = SequenceTransport([TimeoutError("sk-topsecret-in-exc")])
    router = _router(transport, max_retries=0, api_key="sk-topsecret")
    with pytest.raises(ProviderUnavailableError) as ei:
        router.plan_structured("goal", [], {})
    assert "sk-topsecret" not in str(ei.value)


def test_rate_limit_failure_metadata_event_category():
    transport = SequenceTransport([(429, "rate limited")])
    sink = RecordingSink()
    router = _router(transport, sink=sink, max_retries=0)
    with pytest.raises(ProviderRateLimitError):
        router.plan_structured("goal", [], {})
    meta = [e for e in sink.events if e.kind == "model.response.received"]
    assert len(meta) == 1
    assert meta[0].success is False
    assert meta[0].detail.get("category") == "provider_rate_limit"
    raw = json.dumps([e.to_dict() for e in sink.events])
    assert "rate limited" not in raw  # provider body never persisted
