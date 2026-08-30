# M1 Completion Report — ADR-057 Provider Configuration + Bounded Transport Retry

- **Date:** 2026-08-29
- **Scope:** M1 of ADR-057 (`docs/adr/057-model-backed-intelligence-path.md`,
  approved). M2–M5 intentionally NOT implemented.
- **Baseline (M0, accepted):** 1,478 passed / 2 skipped / 23 demos / 87%
  coverage; authority model, persistence and schema unchanged.

## 1. What was implemented

| File | Change |
|---|---|
| `arion/intelligence/config.py` (new) | `ModelProviderConfig` (frozen, validated, credential-redacting repr), `load_model_config()` reading the `ARION_LLM_*` env surface, `enabled` semantics (unset/empty/`none` = deterministic). |
| `arion/intelligence/providers/__init__.py` | `PROVIDER_REGISTRY` (`{"openai-compatible": OpenAICompatModelRouter}`) and `build_router(config, sink=None) -> ModelRouter \| None`. None when disabled; unknown provider fails closed with a typed `ProviderConfigurationError` (bounded, no credentials). |
| `arion/intelligence/providers/openai_compat.py` | Bounded transport-level retry: constructor params `max_retries` (default 2), `retry_backoff_base` (1.0), `retry_backoff_max` (8.0), `sleep` (injectable test seam); deterministic exponential backoff `min(base*2^attempt, max)`; retries only for OSError (network/timeout), HTTP 5xx, HTTP 429; Retry-After honored (capped at max) via transport-raised `ProviderRateLimitError`; 429 → typed `ProviderRateLimitError` (previously collapsed into `ProviderConfigurationError`); `model.retry` event (attempt/delay_ms/category); configured `timeout` now actually plumbed into the default urllib transport; default transport surfaces HTTPError status codes (429 → `ProviderRateLimitError` with Retry-After; other non-2xx → `(code, "")`); constructor value validation (finite, typed, ordered). |
| `arion/intelligence/errors.py` | `ProviderRateLimitError(ModelPlanError)` with category `provider_rate_limit` and optional `retry_after_seconds`; taxonomy docstring updated. |
| `arion/observability/events.py` | `EVENT_KINDS` + `"model.retry"` (additive, after `model.response.received`). |
| `arion/intelligence/__init__.py` | Public surface + `ProviderRateLimitError`, `ModelProviderConfig`, `load_model_config`, `build_router`. |
| `tests/test_model_config.py` (new) | 31 tests: config parsing, missing provider → deterministic, valid config, malformed env (timeout/retries/bool), invalid ranges (timeout ≤ 0, max_retries < 0, base ≤ 0, max < base, non-finite, wrong types), unknown provider fails closed, credential redaction, re-exports. |
| `tests/test_transport_retry.py` (new) | 23 tests: transient network error → bounded retry then success; 5xx retry then success; generate() retries; exhaustion → `ProviderUnavailableError`; 429 → `ProviderRateLimitError` (with/without Retry-After, capped, exhausted); auth/config never retried (single attempt); non-OSError never retried; `model.retry` event emission + bounded detail + no credential leakage; deterministic policy (identical runs, capped backoff); event kind registered. |
| `tests/test_model_router.py` | Shared `_router` helper: `max_retries=0` (preserves single-attempt error-mapping semantics; retries have their own suite). |
| `tests/test_planning_errors.py` | Helpers `_router_with_status` / `_router_with_raising_transport`: `max_retries=0`; `ProviderRateLimitError` added to the categories-distinguishability test (construction now passes a message arg). |
| `docs/adr/057-model-backed-intelligence-path.md` | Status → Approved; M1 implemented noted. |

## 2. Implementation decisions (within ADR-057 M1)

1. **Retry lives in `_request_with_retry` inside the adapter** (transport
   boundary), wrapping the injectable transport call; the engine and planner
   are untouched.
2. **Retryable = OSError (network/timeout/DNS), HTTP 5xx, HTTP 429.**
   Non-retryable = 401/403/other 4xx (deterministic auth/config) and
   non-OSError transport exceptions (programming errors — fail immediately,
   wrapped as before).
3. **Deterministic policy, no jitter:** `delay = min(base * 2**attempt, max)`;
   Retry-After (only observable when the transport raises
   `ProviderRateLimitError`, e.g. the real urllib transport on 429) is
   honored but capped at `retry_backoff_max`. `sleep` is injectable for
   deterministic tests.
4. **Exhaustion semantics:** transport-failure/5xx exhaustion →
   `ProviderUnavailableError`; 429 exhaustion (status-returned or raised) →
   `ProviderRateLimitError`. `generate` and `plan_structured` share `_chat`,
   so both inherit the same policy.
5. **`model.retry` detail is metadata only** (`provider`, `model`, `attempt`
   (1-based), `delay_ms`, `category`) — never the prompt, provider body, or
   credentials; emitted when a retry is scheduled (before the wait).
6. **`timeout` is now actually applied** to the real urllib transport via a
   closure bound at construction (`_make_timeout_transport`); the
   `Transport` protocol signature is unchanged so fake transports are
   unaffected.
7. **Default transport now returns `(code, "")` for non-2xx** (typed status
   mapping in the router) and raises `ProviderRateLimitError` on 429 with the
   Retry-After hint — this is what makes Retry-After honor production-real
   and makes the live path consistent with fake transports.
8. **No runtime opt-in wiring:** `bootstrap.build_engine`, the CLI, the
   engine, `RealModelPlanner`, and memory are untouched — M3/M5 own runtime
   composition. `fallback_enabled`/`reflection_enabled` are parsed into the
   config (the approved surface) but consumed by no code yet.
9. **No `model.fallback`** anywhere; no reflection wiring; no provider beyond
   the existing OpenAI-compatible adapter; no new event kinds beyond
   `model.retry`.

## 3. Tests added

- `tests/test_model_config.py` — 31 tests.
- `tests/test_transport_retry.py` — 23 tests.
- Modified: `tests/test_model_router.py` (1 helper line), `tests/test_planning_errors.py`
  (helpers + distinguishability list).
- Net new tests: 53 (baseline 1,478 → 1,532). All existing tests unchanged in
  intent; two shared helpers opt out of retry (`max_retries=0`) so their
  single-attempt typed-error assertions stay exact.

## 4. Gate results

| Gate | Result |
|---|---|
| 1. New targeted tests | `tests/test_model_config.py` + `tests/test_transport_retry.py` — 54 pass, 0 fail |
| 2. Complete authoritative suite | **1,532 passed, 2 skipped, exit 0** (~160–200 s). First full run captured verbatim: `1531 passed, 2 skipped in 160.29s`; one test added afterwards (all green, exit 0 on two further full runs; pytest's summary line is intermittently swallowed under `> file` redirect — exit code authoritative) |
| 3. All demos | 21 standalone demo scripts re-run: **21/21 pass, exit 0** — 20 on first pass; `demo_adr024_concurrency` failed once on the first batch (E-section cancellation timing flake, unrelated to M1 — demo touches no modified module), then **6/6 consecutive clean passes (31 checks)** |
| 4. Coverage | **87% overall** (10,534 stmts / 1,368 missed, same as baseline); new code: `config.py` 100%, `errors.py` 100%, `providers/__init__.py` 100%, `openai_compat.py` 86% (remaining misses = live-urllib HTTPError paths only, coverable via the offline-skipped smoke test, plus two defensive sink-swallow branches mirroring pre-existing `_emit_meta`) |
| 5. Complete git diff | Inspected in full — see §1 table; M0 files untouched |
| 6. Scope | Only `arion/intelligence/*`, `arion/observability/events.py`, and the two model-path test files changed (plus the ADR doc). Engine, CLI, bootstrap, memory, cognition, state, scheduler: **untouched** |
| 7. No M2–M5 | No fallback, no reflection wiring, no engine/CLI opt-in, no size caps (M2), no `model.fallback` event (M3), no `ARION_LLM_REFLECTION` behavior (M4) — confirmed by diff |
| 8. Runtime artifacts | Working tree clean of artifacts: `.coverage`, `__pycache__`, `.pytest_cache`, demo/scratch DBs removed; only source/test/doc changes remain |
| 9. ADR-057 invariants | See §5 |

## 5. Invariant check (ADR-057 M1 list)

1. No provider configured = deterministic unchanged: `build_router(None)` and
   disabled configs return `None`; nothing else wired. ✅
2. Engine provider-agnostic: engine/CLI/bootstrap untouched. ✅
3. Retry at transport boundary: `_request_with_retry` inside the adapter;
   orchestration/planner untouched. ✅
4. Retries bounded (`max_retries`), deterministic (no jitter; injectable
   sleep), observable (`model.retry`), restricted to retryable failures,
   auth/config never retried. ✅
5. HTTP 429 → typed `ProviderRateLimitError` (category `provider_rate_limit`),
   honors Retry-After within budget. ✅
6. No `model.fallback` implementation. ✅ (no such event kind or code)
7. No reflection wiring. ✅
8. No runtime opt-in engine wiring beyond router-factory construction. ✅
9. Authority model unchanged. ✅ (no authz/approval/policy files touched)
10. Persistence schema unchanged. ✅ (no store/schema files touched)
11. No new provider/vendor coupling in the core engine. ✅ (adapter stays
    inside `intelligence/providers`; only one provider registered)
12. No unrelated refactors. ✅ (diff limited to M1 surface + necessary test
    helper adjustments)

Configuration discipline: exactly the ADR-057 env surface (`ARION_LLM_PROVIDER`,
`ARION_LLM_MODEL`, `ARION_LLM_BASE_URL`, `ARION_LLM_API_KEY`,
`ARION_LLM_TIMEOUT_SECONDS`, `ARION_LLM_MAX_RETRIES`, `ARION_LLM_FALLBACK`,
`ARION_LLM_REFLECTION`); malformed values fail with typed, actionable
`ProviderConfigurationError`s naming the offending variable. Credentials: API
key appears in no log, event, exception message, persisted record, or test
output (asserted by `test_config_repr_redacts_api_key`,
`test_exception_messages_do_not_contain_api_key`,
`test_exception_messages_never_contain_api_key`,
`test_retry_events_emitted_with_bounded_detail`).

## 6. Deviations from ADR-057

None in behavior. Two notes:
- **Demo count:** ADR-057/M0 docs say "23 demos"; the repo contains 21
  standalone `scripts/demo_*.py` scripts (+3 `_*_worker.py` subprocess
  helpers). The M1 demo gate ran all 21. This pre-existing documentation
  count discrepancy is carried forward from M0, not introduced by M1.
- **`test_categories_are_distinguishable`** now constructs errors with a
  message argument (required by `ProviderRateLimitError`'s signature).

## 7. Newly discovered architectural questions

1. **Summary-line flake:** under `pytest -q ... > file`, the final "N passed"
   line is intermittently absent from the redirect (exit code remains
   authoritative). Cosmetic; worth noting if any CI harness greps the log.
2. **`demo_adr024_concurrency` E-section flake:** one run showed the
   cancelled queued item as run (`ran == ["one", "three"]` failed once),
   then 6/6 clean. Pre-existing timing sensitivity in the demo's
   cancellation check (worker-start race), unrelated to M1; scheduler
   behavior covered by the deterministic test suite.
3. **Live-only coverage:** `_default_transport` HTTPError branches (429 →
   `ProviderRateLimitError` + Retry-After, other non-2xx → `(code, "")`) are
   coverable only with a live endpoint (the offline-skipped smoke test).
   Acceptable per ADR-008; a scripted live harness is planned in M5.
4. **Retry semantics for `generate`:** free-form generation now retries
   transient failures identically to structured planning (shared `_chat`).
   This is intended (same transport boundary) but is a behavior expansion of
   the pre-M1 `generate` path.

## 8. Next steps (not started)

M2 (output size/depth caps), M3 (fallback + `model.fallback`), M4 (reflection
wiring), M5 (runtime opt-in + live harness). Awaiting explicit approval.
