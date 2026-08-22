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
- Observability: emits `model.response.received` with provider/model/latency/
  token metadata only. Raw prompts and raw responses are never persisted.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Callable

from arion.intelligence.errors import (
    MalformedProviderResponseError,
    PlanSchemaValidationError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema, PlanValidationError
from arion.observability.error_boundary import (
    ErrorSource,
    ErrorSummary,
    summarize_error,
)
from arion.observability.events import AuditEvent

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# transport(url, headers, body) -> (status_code, response_body_text)
Transport = Callable[[str, dict[str, str], str], tuple[int, str]]


def _default_transport(url: str, headers: dict[str, str], body: str, timeout: float = 60.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


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
        transport: Transport | None = None,
        sink: Any | None = None,
    ):
        self.model = model or os.environ.get("ARION_LLM_MODEL") or "gpt-4o-mini"
        self.base_url = (base_url or os.environ.get("ARION_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("ARION_LLM_API_KEY", "")
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.sink = sink  # duck-typed EventLogger: emits model.response.received metadata only

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
            obj = json.loads(content)
            schema = PlanSchema.from_dict(obj)
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
        try:
            status, text = self.transport(url, headers, json.dumps(body))
        except Exception as exc:  # request/header/body text is never retained
            raise ProviderUnavailableError(
                f"provider unreachable ({type(exc).__name__})"
            ) from exc
        # Provider bodies are untrusted and may echo credentials, prompts, or
        # completions. The typed category + HTTP status is sufficient for the
        # public exception and durable diagnosis (ADR-034).
        if status == 401 or status == 403:
            raise ProviderAuthenticationError(
                f"provider authentication failed (HTTP {status})"
            )
        if status >= 500:
            raise ProviderUnavailableError(
                f"provider unavailable (HTTP {status})"
            )
        if status >= 400:
            raise ProviderConfigurationError(
                f"provider configuration error (HTTP {status})"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedProviderResponseError(f"provider returned malformed JSON: {exc}") from exc

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
