"""OpenAI-compatible provider adapter (ADR-005, ADR-011).

Works against any OpenAI-compatible `/chat/completions` endpoint: OpenAI,
Azure OpenAI, Ollama, LiteLLM, vLLM, LM Studio, ... Uses only the standard
library (urllib), so there is no hard SDK dependency.

- Credentials come from the environment (`ARION_LLM_API_KEY`) or the
  constructor; they are NEVER stored in the repository or in audit events.
- Structured output is requested via `response_format: json_object` and then
  parsed + strictly validated into a PlanSchema; anything invalid is rejected
  with ModelPlanError (the provider cannot silently degrade to prose).
- The HTTP transport is injectable so tests run with a fake transport and
  never require credentials or network.
- Bounded transport-level retry (ADR-057 M1): transient failures only
  (network/timeout/HTTP 5xx/HTTP 429) with deterministic exponential
  backoff; auth/config errors are never retried; HTTP 429 raises the typed
  `ProviderRateLimitError` and honors Retry-After within the retry budget.
  Retries are observable via the `model.retry` audit event.
- Bounded model output (ADR-057 M2): raw response size and JSON nesting
  depth are enforced on the envelope before parsing, and again on the plan
  content; PlanSchema enforces step/params/string caps at parse time.
  Violations are deterministic typed failures — never retried, never
  fallback (M3 owns fallback).
- Observability: emits `model.response.received` with provider/model/latency/
  token metadata only. Raw prompts and raw responses are never persisted.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from arion.intelligence.errors import (
    MalformedProviderResponseError,
    PlanSchemaValidationError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from arion.intelligence.plan_schema import (
    MAX_JSON_DEPTH,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_PARAMS_PER_STEP,
    MAX_PLAN_STEPS,
    MAX_STEP_STRING,
    PLAN_SCHEMA_VERSION,
    PlanSchema,
    PlanValidationError,
    json_text_depth,
)
from arion.observability.error_boundary import (
    ErrorSource,
    ErrorSummary,
    summarize_error,
)
from arion.observability.events import AuditEvent

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# transport(url, headers, body) -> (status_code, response_body_text)
# Transports return the HTTP status + body text for any status they observe.
# They MAY raise ProviderRateLimitError (with optional retry_after_seconds)
# instead of returning 429 when they can observe the Retry-After header;
# the router honors it within the retry budget.
Transport = Callable[[str, dict[str, str], str], tuple[int, str]]


def _is_finite_number(value: Any) -> bool:
    """True for a real (non-bool) finite int/float; rejects NaN/Inf/strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds) into seconds.

    HTTP-date values and malformed input return None; the caller then falls
    back to the deterministic exponential backoff. Header text is bounded and
    never appears in errors or events.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _default_transport(url: str, headers: dict[str, str], body: str, timeout: float = 60.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Surface the Retry-After hint so the retry policy can honor it.
            raise ProviderRateLimitError(
                "provider rate limit exceeded (HTTP 429)",
                retry_after_seconds=_parse_retry_after(exc.headers.get("Retry-After")),
            ) from exc
        # Non-2xx bodies are never retained or surfaced (ADR-034); the typed
        # status mapping happens in the router's retry/status logic.
        return exc.code, ""


def _make_timeout_transport(timeout: float) -> Transport:
    """Bind the configured timeout into the default transport.

    The Transport protocol stays (url, headers, body) -> (status, text); the
    timeout is a constructor concern of the adapter, not of fake transports.
    """

    def _wrapped(url: str, headers: dict[str, str], body: str) -> tuple[int, str]:
        return _default_transport(url, headers, body, timeout=timeout)

    return _wrapped


PLANNING_SYSTEM_PROMPT = f"""You are Arion's structured planning component. You convert a user goal into a JSON plan.

Output ONLY a single JSON object with EXACTLY this shape:
{{
  "version": "{PLAN_SCHEMA_VERSION}",
  "intent": "<one-line restatement of the goal>",
  "steps": [
    {{
      "intent": "<what this step does>",
      "capability": "<capability name from the catalog>",
      "action": "<action name from the catalog>",
      "params": {{ "<param key>": <value>, ... }},
      "verification": {{ "policy": "non_empty" or "schema_keys", "args": {{ ... }} }},
      "depends_on": [ <indices of earlier steps, optional> ]
    }}
  ]
}}

RULES:
- Use ONLY capabilities and actions present in the provided capability_catalog.
- Each action's params must satisfy its declared param_schema: required keys present, correct types, and NO extra keys.
- Do NOT include any of these fields anywhere (steps or params): scope, resource_kind, resource_param, risk, side_effects, idempotent, retry_safe, reversible, permissions, actor, approve, grant, authorization, boundary, allowed. The system resolves all of them.
- verification is REQUIRED per step. policy must be "non_empty" or "schema_keys". For schema_keys, args must be {{"keys": ["<string>", ...]}}.
- Steps run in array order. depends_on may only reference earlier indices.
- If the goal cannot be achieved with the catalog, output {{"version": "{PLAN_SCHEMA_VERSION}", "intent": "<goal>", "steps": []}}."""


class OpenAICompatModelRouter:
    """Provider-neutral structured planner for OpenAI-compatible endpoints."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 8.0,
        max_response_bytes: int = MAX_MODEL_RESPONSE_BYTES,
        max_json_depth: int = MAX_JSON_DEPTH,
        max_plan_steps: int = MAX_PLAN_STEPS,
        max_params_per_step: int = MAX_PARAMS_PER_STEP,
        max_step_string: int = MAX_STEP_STRING,
        transport: Transport | None = None,
        sink: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not _is_finite_number(timeout) or not (timeout > 0):
            raise ProviderConfigurationError(f"timeout must be a finite number > 0 (got {timeout!r})")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ProviderConfigurationError(f"max_retries must be an int >= 0 (got {max_retries!r})")
        if not _is_finite_number(retry_backoff_base) or not (retry_backoff_base > 0):
            raise ProviderConfigurationError(
                f"retry_backoff_base must be a finite number > 0 (got {retry_backoff_base!r})"
            )
        if not _is_finite_number(retry_backoff_max) or retry_backoff_max < retry_backoff_base:
            raise ProviderConfigurationError(
                f"retry_backoff_max ({retry_backoff_max!r}) must be a finite number >= "
                f"retry_backoff_base ({retry_backoff_base!r})"
            )
        for name, value in (
            ("max_response_bytes", max_response_bytes),
            ("max_json_depth", max_json_depth),
            ("max_plan_steps", max_plan_steps),
            ("max_params_per_step", max_params_per_step),
            ("max_step_string", max_step_string),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ProviderConfigurationError(
                    f"{name} must be a positive int (got {value!r})"
                )
        self.model = model or os.environ.get("ARION_LLM_MODEL") or "gpt-4o-mini"
        self.base_url = (base_url or os.environ.get("ARION_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("ARION_LLM_API_KEY", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self.max_response_bytes = max_response_bytes
        self.max_json_depth = max_json_depth
        self.max_plan_steps = max_plan_steps
        self.max_params_per_step = max_params_per_step
        self.max_step_string = max_step_string
        self.sleep = sleep  # test seam: injectable clock/recorder; default time.sleep
        self.sink = sink  # duck-typed EventLogger: emits model.response.received / model.retry metadata only
        self.transport = _make_timeout_transport(timeout) if transport is None else transport

    # ---------- ModelRouter protocol ----------

    def generate(self, prompt: str, **kwargs: Any) -> str:
        body = {
            "model": self.model,
            "temperature": kwargs.get("temperature", 0),
            "messages": [{"role": "user", "content": prompt}],
        }
        data = self._chat(body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MalformedProviderResponseError(f"provider response missing message content: {exc}") from exc

    def plan_structured(self, goal: str, capabilities: list[dict[str, Any]], context: dict[str, Any]) -> PlanSchema:
        """Request a structured plan; parse + strictly validate; reject anything invalid."""
        t0 = time.monotonic()
        task_id = context.get("task_id")
        user_prompt = json.dumps(
            {"goal": goal, "context": context, "capability_catalog": capabilities}, indent=2, default=str
        )
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PLANNING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            data = self._chat(body)
        except Exception as exc:  # provider text is external: summarize, never persist it
            summary = summarize_error(
                exc,
                source=ErrorSource.EXTERNAL,
                category=getattr(exc, "category", "provider_unavailable"),
            )
            self._emit_meta(
                False,
                task_id=task_id,
                latency_ms=_latency(t0),
                error=summary,
            )
            raise

        latency_ms = _latency(t0)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            error = MalformedProviderResponseError(
                f"provider response missing message content: {exc}"
            )
            self._emit_meta(
                False,
                task_id=task_id,
                latency_ms=latency_ms,
                error=summarize_error(
                    error,
                    source=ErrorSource.EXTERNAL,
                    category=error.category,
                ),
            )
            raise error from exc

        usage = data.get("usage")
        tokens = None
        if isinstance(usage, dict):
            tokens = {k: v for k, v in usage.items() if isinstance(v, int)}

        try:
            # Content-level depth bound (ADR-057 M2): the plan text lives
            # inside the envelope string, so the envelope scan in `_chat`
            # cannot see it. Bound it here BEFORE json.loads(content) so a
            # pathologically nested plan cannot overflow the JSON parser.
            content_depth = json_text_depth(content)
            if content_depth > self.max_json_depth:
                raise PlanSchemaValidationError(
                    f"JSON nesting depth exceeds maximum ({content_depth} > {self.max_json_depth})"
                )
            obj = json.loads(content)
            schema = PlanSchema.from_dict(
                obj,
                max_steps=self.max_plan_steps,
                max_params_per_step=self.max_params_per_step,
                max_step_string=self.max_step_string,
                max_json_depth=self.max_json_depth,
            )
        except json.JSONDecodeError as exc:
            # The parse location is useful to direct callers, but the provider
            # content itself never enters observability.
            error = MalformedProviderResponseError(
                f"model returned an invalid structured plan: {exc}"
            )
            self._emit_meta(
                False,
                task_id=task_id,
                latency_ms=latency_ms,
                error=summarize_error(
                    error,
                    source=ErrorSource.EXTERNAL,
                    category=error.category,
                ),
            )
            raise error from exc
        except PlanValidationError as exc:
            # The detailed Arion validation template remains available to the
            # direct caller; provider metadata receives only the external
            # summary. Downstream planner/task boundaries redact and bound it.
            error = PlanSchemaValidationError(
                f"model returned an invalid structured plan: {exc}"
            )
            self._emit_meta(
                False,
                task_id=task_id,
                latency_ms=latency_ms,
                error=summarize_error(
                    error,
                    source=ErrorSource.EXTERNAL,
                    category=error.category,
                ),
            )
            raise error from exc

        self._emit_meta(True, task_id=task_id, latency_ms=latency_ms, tokens=tokens)
        return schema

    # ---------- internals ----------

    def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, text = self._request_with_retry(url, headers, json.dumps(body))
        # Provider bodies are untrusted and may echo credentials, prompts, or
        # completions. The typed category + HTTP status is sufficient for the
        # public exception and durable diagnosis (ADR-034).
        if status == 401 or status == 403:
            raise ProviderAuthenticationError(
                f"provider authentication failed (HTTP {status})"
            )
        if status == 429:
            raise ProviderRateLimitError(
                "provider rate limit exceeded (HTTP 429)"
            )
        if status >= 500:
            raise ProviderUnavailableError(
                f"provider unavailable (HTTP {status})"
            )
        if status >= 400:
            raise ProviderConfigurationError(
                f"provider configuration error (HTTP {status})"
            )
        # Size/depth bounds (ADR-057 M2): the untrusted response body is
        # bounded BEFORE json.loads. These checks run after retry exhaustion
        # in `_request_with_retry` — they are deterministic validation
        # failures, never retried, never fallback. Only the bounded size
        # figures enter the error text; the body itself never does.
        body_bytes = len(text.encode("utf-8"))
        if body_bytes > self.max_response_bytes:
            raise MalformedProviderResponseError(
                f"provider response exceeds maximum size ({body_bytes} bytes > {self.max_response_bytes})"
            )
        depth = json_text_depth(text)
        if depth > self.max_json_depth:
            raise PlanSchemaValidationError(
                f"provider response JSON nesting depth exceeds maximum ({depth} > {self.max_json_depth})"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedProviderResponseError(f"provider returned malformed JSON: {exc}") from exc

    def _request_with_retry(
        self, url: str, headers: dict[str, str], payload: str
    ) -> tuple[int, str]:
        """Execute one request with bounded transport-level retry (ADR-057 M1).

        Retry policy (deterministic, no jitter):
        - retryable: transport OSError (network/timeout), HTTP 5xx, HTTP 429
        - never retried: HTTP 401/403/other 4xx (auth/config are deterministic)
        - bounded by `max_retries` with exponential backoff
          `min(retry_backoff_base * 2**attempt, retry_backoff_max)`; a
          provider Retry-After hint is honored, capped by retry_backoff_max
        - each scheduled retry is observable via the `model.retry` event
        - after exhaustion the typed error propagates (ProviderUnavailableError
          for transport failures / 5xx, ProviderRateLimitError for 429)
        """
        attempt = 0
        while True:
            try:
                status, text = self.transport(url, headers, payload)
            except ProviderRateLimitError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(attempt, exc.retry_after_seconds)
                self._emit_retry(attempt + 1, delay, "provider_rate_limit")
                self.sleep(delay)
                attempt += 1
                continue
            except Exception as exc:  # request/header/body text is never retained
                # Only network-level failures (OSError family: connection,
                # timeout, DNS) are transient enough to retry. Anything else
                # fails immediately, wrapped as today.
                if not isinstance(exc, OSError):
                    raise ProviderUnavailableError(
                        f"provider unreachable ({type(exc).__name__})"
                    ) from exc
                if attempt >= self.max_retries:
                    raise ProviderUnavailableError(
                        f"provider unreachable ({type(exc).__name__})"
                    ) from exc
                delay = self._retry_delay(attempt)
                self._emit_retry(attempt + 1, delay, "provider_unavailable")
                self.sleep(delay)
                attempt += 1
                continue
            if status == 429:
                if attempt >= self.max_retries:
                    return status, text  # caller raises ProviderRateLimitError
                delay = self._retry_delay(attempt)
                self._emit_retry(attempt + 1, delay, "provider_rate_limit")
                self.sleep(delay)
                attempt += 1
                continue
            if status >= 500:
                if attempt >= self.max_retries:
                    return status, text  # caller raises ProviderUnavailableError
                delay = self._retry_delay(attempt)
                self._emit_retry(attempt + 1, delay, "provider_unavailable")
                self.sleep(delay)
                attempt += 1
                continue
            return status, text

    def _retry_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Deterministic backoff: base * 2**attempt, capped at max; a
        Retry-After hint wins but is still capped at retry_backoff_max."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.retry_backoff_max)
        return min(self.retry_backoff_base * (2 ** attempt), self.retry_backoff_max)

    def _emit_retry(self, attempt: int, delay_seconds: float, category: str) -> None:
        """Emit the bounded model.retry event when a retry is scheduled.

        Detail is metadata only (attempt, delay_ms, category) — never the
        provider body, prompt, or credentials (ADR-057 M1).
        """
        if self.sink is None:
            return
        detail: dict[str, Any] = {
            "provider": "openai-compatible",
            "model": self.model,
            "attempt": attempt,
            "delay_ms": round(delay_seconds * 1000),
            "category": category,
        }
        try:
            self.sink.emit(AuditEvent(kind="model.retry", success=True, detail=detail))
        except Exception:
            pass

    def _emit_meta(
        self,
        success: bool,
        task_id: str | None,
        latency_ms: int,
        tokens: dict[str, int] | None = None,
        error: ErrorSummary | None = None,
    ) -> None:
        """Emit provider metadata and structured safe errors only."""
        if self.sink is None:
            return
        detail: dict[str, Any] = {
            "provider": "openai-compatible",
            "model": self.model,
            "latency_ms": latency_ms,
        }
        if tokens:
            detail["tokens"] = tokens
        if error is not None:
            detail.update(error.to_event_detail())
        try:
            self.sink.emit(AuditEvent(kind="model.response.received", task_id=task_id, success=success, detail=detail))
        except Exception:
            pass


def _latency(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)
