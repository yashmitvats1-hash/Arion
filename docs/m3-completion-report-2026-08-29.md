# M3 Completion Report — ADR-057 Fallback Composition

- **Date:** 2026-08-29
- **Scope:** M3 of ADR-057 (`docs/adr/057-model-backed-intelligence-path.md`,
  approved). M4 (reflection wiring) and M5 (runtime opt-in + live harness)
  intentionally NOT implemented.
- **Baseline (M2, accepted):** 1,614 passed / 2 skipped / 21 demos / 87%
  coverage; PR #10 open (3 commits, mergeable). Authority model, persistence
  schema, scheduler, locks, registry semantics, authorization model,
  approval, execution and recovery unchanged by M3.

## 1. Architecture

Fallback composition lives **entirely in the planner layer** — the engine is
planner-agnostic and unchanged in its decision flow:

```
RealModelPlanner.plan(goal) ->
  attempt model plan_structured (bounded semantic retry for
    malformed_response / schema_validation / capability_validation)
    ├─ success -> PlanValidator -> steps (last_source = "model")
    ├─ typed PlanningError (any of the 7 categories):
    │     fallback_enabled=True -> DeterministicPlanner.plan
    │                              (last_source = "deterministic")
    │     fallback_enabled=False (strict) -> typed durable failure
    └─ unexpected exception (not a PlanningError):
          never retried, never fallen back ->
          wrapped PlanValidationError (category "unknown") -> durable failure
```

The fallback plan returns to the engine as an **ordinary plan** and enters
the identical downstream pipeline: status normalization → immutable plan
version → live authorization → scheduler/locks → execution → verification →
memory. There is **no second execution path**.

## 2. Files changed

| File | Change |
|---|---|
| `arion/intelligence/model_planner.py` | M3 core: `_SEMANTIC_RETRY_CATEGORIES` (`malformed_response`, `schema_validation`, `capability_validation`), `_FALLBACK_CATEGORIES` (all 7 ADR-057 categories); ctor gains `fallback_enabled=True`, `semantic_max_retries=2`; bounded semantic-retry loop (same goal + catalog); `_fallback_to_deterministic()` emits `model.fallback` and delegates to `DeterministicPlanner`; `last_source` ("model"/"deterministic"); unknown exceptions wrapped, never retried/fallen back. |
| `arion/intelligence/planner.py` | `DeterministicPlanner.last_source = "deterministic"` (set in `__init__` and reaffirmed at end of `plan()`) — additive D3 source marker. |
| `arion/observability/events.py` | `"model.fallback"` added to `EVENT_KINDS` (after `"model.retry"`). |
| `arion/cognition/goals.py` | `record_plan_version(..., source: str | None = None)` — `plan.versioned` detail gains the additive `"source"` key when provided; legacy callers unchanged. |
| `arion/orchestration/engine.py` | `_plan` computes `plan_source = getattr(self.planner, "last_source", None) or "deterministic"` and threads it into `record_plan_version(..., source=...)` and the `plan.produced` detail; stored-plan fast path (`_plan_for_goal`) emits `"source": "stored"`; `planning.memory.influence` detail gains `"source": "deterministic"` while preserving legacy `"deterministic": True`. |
| `tests/test_model_fallback.py` (new, 32) | See §4. |
| `tests/test_model_planner.py` | Adversarial model-failure tests opt into strict mode (`fallback_enabled=False`) so their "typed durable failure, nothing executes" assertions remain exact; helper updated with a comment. |
| `tests/test_planning_errors.py` | Typed-category engine tests build the planner in strict mode (their contract is durable typed failure); fallback path covered in `test_model_fallback.py`. |
| `tests/test_model_output_limits.py` | `test_rejection_does_not_invoke_fallback` updated to the M3 contract: `model.fallback` is now a registered kind, but the **router** still never emits it — rejection surfaces as a typed planning failure to the planner layer. |
| `tests/test_planner_contract.py` | Evil-router contract updated to the M3 contract: the model's `shell.exec` is discarded by validation; the deterministic fallback authority also fails closed (goal not decomposable) → durable FAILED; zero `permission.checked` / `capability.executed`; `model.fallback` emitted. |
| `docs/adr/057-…` | Status → M1+M2+M3; D3/D5 marked implemented. |

M1 files (`config.py`, `providers/__init__.py`, `intelligence/__init__.py`,
transport retry) and M2 files (`plan_schema.py`, output-limit enforcement in
`openai_compat.py`) are **byte-identical to the M2 commit** — verified by
`git diff 1e9059e` showing only the M3 delta.

## 3. Retry / fallback policy (M1 vs M3 separation)

| Failure | Transport retry (M1, adapter) | Semantic retry (M3, planner) | Fallback (M3) | Durable outcome |
|---|---|---|---|---|
| network / timeout / 5xx | yes (`model.retry`) | **no** | yes (immediately) | `provider_unavailable` |
| HTTP 429 | yes (`model.retry`, Retry-After) | **no** | yes (immediately) | `provider_rate_limit` |
| HTTP 401/403/4xx, unknown provider | **no** | **no** | yes (immediately) | `provider_auth` / `provider_config` |
| malformed JSON / oversize / depth | **no** (M2: single attempt) | yes (bounded) | yes (after budget) | `malformed_response` |
| schema validation failure | **no** | yes (bounded) | yes (after budget) | `schema_validation` |
| capability validation failure | **no** | yes (bounded) | yes (after budget) | `capability_validation` |
| unexpected exception | **no** | **no** | **no** | wrapped `PlanValidationError` ("unknown") |

- Semantic retries re-issue the **same goal + same catalog**, are bounded by
  `semantic_max_retries=2` (the smallest budget consistent with the ADR's
  "bounded" requirement and matching M1's `max_retries=2` default), and
  **never emit `model.retry`** (that event is M1 transport-retry
  observability, owned by the provider adapter).
- `model.fallback` is emitted **only** after the semantic budget is exhausted
  (semantic categories) or immediately (provider categories). Exactly one
  fallback per planning call.
- Strict mode (`fallback_enabled=False`) is deterministic fail-closed: the
  typed category is preserved end-to-end (`plan.validation.failed` →
  engine `error` event), nothing executes, no new blocker type, and the
  existing `max_replans` bound (ADR-016) still terminates the no-fallback
  loop — verified by `test_strict_mode_max_replans_boundary_untouched`
  (task FAILED, one router call, goal stays ACTIVE for the caller to decide,
  exactly as pre-M3).

## 4. Tests added (`tests/test_model_fallback.py`, 32)

- **7-category failure matrix × mode (14):** every typed category in default
  mode completes via the deterministic fallback (exactly one bounded
  `model.fallback` with `{reason, attempts, fallback:"deterministic"}`,
  `source:"deterministic"` on `plan.produced`, registry-scope-only
  authorization); in strict mode each category fails durably with the typed
  category preserved, zero fallback, zero `capability.executed`.
- **Retry separation (6):** semantic retries re-issue identical goal+catalog,
  succeed within budget (`source:"model"`, no fallback), exhaust budget then
  fall back once; provider categories are never semantically retried (one
  router call); no category ever produces `model.retry` from the
  planner/fallback path; unexpected exceptions are never retried or fallen
  back (wrapped `PlanValidationError`, category "unknown").
- **Adversarial provider (3):** a provider forging `scope=shell:exec` /
  `capability=shell.exec` is rejected by validation; the fallback plan is
  independent (planning-shape equal to a fresh `DeterministicPlanner` run);
  authorization decisions use only registry scopes — the model cannot
  authorize itself.
- **Replay (1):** stored-plan fast path reconstructs the plan with **zero
  router calls** (`calls == []`), no planning/fallback/retry events,
  `source:"stored"`, and the stored plan still passes live authorization.
- **Source markers (3):** `plan.versioned` carries `"source"` for both
  fallback and model paths; `planning.memory.influence` carries
  `"source":"deterministic"` while preserving legacy `"deterministic": True`
  (both model-success and fallback runs).
- **Event boundedness (2):** `model.fallback` detail is exactly
  `{reason, attempts, fallback}` — no prompts, responses, credentials
  (an `sk-…` secret in the router never appears), or goal text.
- **Single pipeline (2):** one `plan.produced`, one `plan.validation.failed`
  (on the final model-path failure; retries emit nothing), one fallback, no
  replan loop, standard step lifecycle.

## 5. Gate results

| Check | Result |
|---|---|
| M3 tests (`tests/test_model_fallback.py`) | 32 passed |
| Model-path suites (planner, router, errors, limits, config, transport retry, reflection validation, hardened stored-plan, audit, learning, plan CLI, goal-run fencing, planner contract) | all passed |
| Full authoritative suite | **1,646 passed / 2 skipped / 0 failed** (1,614 M2 + 32 M3) |
| Demos | 21/21 passed |
| Coverage | **87%** (10,673 stmts, 1,361 missed) — gate ≥87% met |
| Diff review | Only the M3 delta vs `1e9059e`; M1/M2 files byte-identical; no M4/M5 artifacts |
| Working tree | clean after commit; no stray artifacts |

Timing note: three full-suite runs each had exactly one flaky
timing-sensitive lease/concurrency test (`test_bounded_worker_shutdown_waits_for_active`,
`test_active_long_mutation_renews_lease_and_prevents_overlap`) that pass
reliably in isolation and whose files are untouched by M3; a fourth run was
fully green. This is the same class of wall-clock flake as `demo_adr024`
(previously characterized, out of M3 scope).

## 6. Deviations / decisions

- **`semantic_max_retries=2` default:** the ADR requires semantic retry to be
  "bounded" but does not fix the number; 2 was chosen as the smallest budget
  consistent with the requirement and with M1's `max_retries=2` default.
  Configurable via the planner constructor; no new config system.
- **`plan.validation.passed` is model-path-only:** the deterministic
  fallback plan is constructed from the live registry (same construction as
  the no-model deterministic path) and does not pass through `PlanValidator`
  (which validates model-shaped `PlanSchema`, where `scope` etc. are
  forbidden — deterministic steps carry registry-derived scopes). The
  fallback plan enters the identical downstream pipeline (normalization →
  immutable version → live authorization → execution), which is the
  authority boundary. Documented; no behavior change to the deterministic
  spine.
- **`attempts` on `model.fallback` counts planner-level attempts** (transport
  retries inside the adapter are accounted separately in `model.retry`
  events). For provider categories attempts=1; for semantic categories
  attempts=3 (2 retries + final).
- M2's `test_rejection_does_not_invoke_fallback` was updated: the **router**
  still never emits `model.fallback` (fallback is planner-owned); the kind is
  now registered because M3 introduces it.

## 7. Newly discovered questions (unchanged from M2)

- Durable circuit breaker (provider blocker instead of `max_replans`) —
  deferred, would be a separate ADR.
- Per-goal model selection / multi-provider routing — deferred.
- Catalog-size budget (`capabilities_summary()` truncation) — open; M3
  fallback composition does not change planner INPUT sizing.
- `model.response.received` correlation IDs — deferred.

## 8. Next steps (not started)

- **M4 — Reflection wiring** (model reflection through the reflector,
  `ARION_LLM_REFLECTION`, `reflection.created` source marker): requires
  explicit approval after M3 review.
- **M5 — Runtime/CLI opt-in + live harness** (bootstrap env-driven wiring,
  scripted live-provider scenarios): requires explicit approval after M3
  review. `ARION_LLM_FALLBACK=0` strict-mode opt-in is part of M5, not M3.
