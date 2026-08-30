# M2 Completion Report — ADR-057 Model Output Size / Depth Limits

- **Date:** 2026-08-29
- **Scope:** M2 of ADR-057 (`docs/adr/057-model-backed-intelligence-path.md`,
  approved). M3–M5 intentionally NOT implemented.
- **Baseline (M1, accepted):** 1,532 passed / 2 skipped / 21 demos / 87%
  coverage; authority model, persistence and schema unchanged.

## 1. Limits implemented (ADR-057 D2 defaults)

| Limit | Default | Enforced where | Error |
|---|---|---|---|
| `MAX_MODEL_RESPONSE_BYTES` | 262,144 bytes | router `_chat`, on the raw envelope text, **before** `json.loads` | `MalformedProviderResponseError` ("provider response exceeds maximum size (N bytes > M)") |
| `MAX_JSON_DEPTH` | 10 | (a) router `_chat` on raw envelope text before parsing; (b) router `plan_structured` on the plan-content text before `json.loads(content)`; (c) `PlanSchema.from_dict` on the parsed structure (defense in depth, direct callers) | `PlanSchemaValidationError` ("…JSON nesting depth exceeds maximum (N > M)") |
| `MAX_PLAN_STEPS` | 100 | `PlanSchema.from_dict` (steps array length) | `PlanSchemaValidationError` |
| `MAX_PARAMS_PER_STEP` | 32 | `StructuredStep.from_dict` (params dict size) | `PlanSchemaValidationError` |
| `MAX_STEP_STRING` | 2000 chars | `StructuredStep.from_dict` / `PlanSchema.from_dict`: step intent/capability/action, param keys and string values, verification keys, and the top-level `intent` (narrow application of the same "individual string size" bound, documented in the ADR) | `PlanSchemaValidationError` |

All bounds are enforced **iteratively** (`json_depth`, `json_text_depth` —
explicit stack / single-pass scanner, string-literal aware) — the local
application never relies on Python recursion behavior, SQLite constraints,
provider behavior, or model-advertised token limits.

## 2. Files changed

| File | Change |
|---|---|
| `arion/intelligence/plan_schema.py` | `MAX_*` constants; `json_depth` + `json_text_depth` helpers; `PlanSchema.from_dict` keyword-only limit params (`max_steps`, `max_params_per_step`, `max_step_string`, `max_json_depth`) with enforcement; `StructuredStep.from_dict` limit checks; docstring. |
| `arion/intelligence/providers/openai_compat.py` | Constructor params `max_response_bytes` / `max_json_depth` / `max_plan_steps` / `max_params_per_step` / `max_step_string` (validated positive ints, defaults = plan_schema constants); envelope byte+depth bounds in `_chat` before parse; content-depth bound before `json.loads(content)`; configured limits passed to `PlanSchema.from_dict`; docstring. |
| `arion/intelligence/config.py` | New fields + env vars `ARION_LLM_MAX_RESPONSE_BYTES`, `ARION_LLM_MAX_JSON_DEPTH`, `ARION_LLM_MAX_PLAN_STEPS`, `ARION_LLM_MAX_PARAMS_PER_STEP`, `ARION_LLM_MAX_STEP_STRING` (positive-int validation, `_env_positive_int`); repr extended. |
| `arion/intelligence/providers/__init__.py` | `build_router` wires the five limits into the adapter. |
| `arion/intelligence/__init__.py` | Public re-exports of the constants + depth helpers. |
| `tests/test_model_output_limits.py` (new, 47) | See §4. |
| `tests/test_model_config.py` (+35) | Output-bound env parsing, defaults match plan_schema constants, malformed/zero/negative/float/bool rejection, `build_router` wiring. |
| `docs/adr/057-…` | Status → M1+M2; D2 marked implemented with enforcement points; unresolved #3 (defaults) settled; #5 (catalog budget) re-scoped to M3+. |

## 3. Boundary / error semantics

Pipeline preserved: `provider response → size/depth bounds → strict schema
parse → PlanValidator / live registry → existing deterministic spine`.

- Enforcement happens at the router (raw envelope) and at schema parse time
  (structure) — **before** any model-produced content becomes an accepted
  plan. `PlanValidator`/authorization/approval/locks/execution are untouched.
- Failures are deterministic typed `PlanningError`s (`MalformedProviderResponseError`
  for byte size; `PlanSchemaValidationError` for structure/depth). They run
  **after** the M1 retry loop (post-exhaustion), so they are **never
  retried**; there is no fallback path (M3 owns fallback; the event kind
  `model.fallback` does not exist); nothing is persisted as a plan; no
  process crash.
- Error text carries only bounded figures (byte count, depth, count/limit) —
  never response content, credentials, or prompts. Verified by leakage tests.

## 4. Tests added

`tests/test_model_output_limits.py` (47 tests):
- Defaults match ADR-057; comfortably-below accepted; valid existing plan
  identical.
- Byte size: exactly-at-limit accepted; one-over rejected; 1 MB rejected
  (single transport call); large valid-looking plan rejected by byte cap.
- Steps: exactly 100 accepted; 101 rejected; custom-limit boundary.
- Params: exactly 32 accepted; 33 rejected.
- Strings: step intent/capability/action, param string value, param key,
  verification key, top-level intent — exactly 2000 accepted / 2001 rejected.
- Depth: exactly 10 accepted; 11 rejected; near-boundary 9/10/11; 100k-deep
  envelope rejected without `RecursionError`; 100k-deep content rejected
  without `RecursionError`; `json_depth`/`json_text_depth` agreement;
  `from_dict` depth enforcement iterative.
- Malformed + size/depth interaction: envelope-level malformed → existing
  "malformed JSON" path; content-level malformed → existing "invalid
  structured plan" path; oversized-malformed → size error first.
- Failure semantics: rejection never retried (max_retries=5, 1 call, no
  `model.retry`); retry-then-oversize still typed (500 retried once, oversize
  not); no `model.fallback` kind; typed `PlanningError` only.
- Leakage: secrets in intent/params/body absent from exception text and all
  emitted events; depth-rejection messages contain no plan content.
- Determinism: identical rejection type+message across repeated runs;
  repeated adversarial attempts bounded then a valid plan still accepted;
  `generate` oversized rejected (shared `_chat`).
- Constructor/config: invalid limit values rejected (0/negative/float/bool);
  `build_router` wires config limits.

`tests/test_model_config.py` additions (35): full-env parsing with limits,
defaults == plan_schema constants, 5 env vars × malformed values ("abc"/"0"/"-5"/"1.5")
rejected with the env var named, empty = default, invalid direct-config values
rejected, `build_router` wiring assertions.

## 5. Gate results

| Gate | Result |
|---|---|
| 1. New targeted tests | `test_model_output_limits.py` (47) + `test_model_config.py` (66 incl. M1) — all green |
| 2. Complete authoritative suite | **1,614 passed, 2 skipped, exit 0** (1,616 collected; +78 vs M1's 1,534). First run captured exit 0 with the same 2 baseline skips (live-provider smoke, symlink platform skip) |
| 3. All standalone demos | **21/21 pass, exit 0** — including `demo_adr024` (no flake this run) |
| 4. Coverage | **87% overall** (10,643 stmts / 1,367 missed); new code: `plan_schema.py` 97%, `config.py` 100%, `errors.py` 100%, `providers/__init__.py` 100%, `openai_compat.py` 87% (remaining misses = live-urllib HTTPError paths + two defensive sink-swallows, unchanged from M1) |
| 5. Complete diff | Inspected — only the M1/M2 files listed in §2 changed; engine, CLI, bootstrap, memory, cognition, state, scheduler, authz untouched |
| 6. M1 behavior unchanged | M1 suites green (`test_transport_retry.py` 23, `test_model_router.py`, `test_planning_errors.py`, config M1 sections); M2 only ADDED bounds after the retry loop and new config fields/env vars (defaults preserve existing semantics) |
| 7. No M3/M4/M5 | No fallback, no `model.fallback`, no source markers, no strict fallback, no reflection wiring, no engine/CLI/bootstrap wiring, no live harness — grep of the diff confirms only doc-comment mentions of "fallback (M3 owns fallback)" |
| 8. Runtime artifacts | `.coverage`, `__pycache__`, `.pytest_cache`, `arion_data`, demo logs removed |
| 9. Git status / scope | 11 modified + 8 untracked, all intentional (§2 + M0/M1 files); no stray files |
| 10. ADR-057 status | Updated: status → M1+M2 implemented; D2 marked implemented with enforcement points; defaults settled (unresolved #3); catalog budget re-scoped (#5) |
| 11. M2 completion report | This document |

## 6. Deviations from ADR-057

- **Top-level `intent` bounded at `MAX_STEP_STRING`**: the ADR named
  "step strings"; the top-level intent is not a step field. Bounded anyway as
  a narrow application of the same individual-string-size bound (a 200 KB
  intent would otherwise be accepted into planning/checkpoints). Documented
  in the ADR D2 text and this report.
- **Verification keys bounded at `MAX_STEP_STRING`**: same rationale
  (strings inside a step). Documented.
- No default values changed; all ADR-057 proposed defaults are effective.
- The content-level depth scan (before `json.loads(content)`) is an
  additional enforcement point beyond the ADR's "parse time" wording: the
  envelope-level scan cannot see inside the stringified plan, so this closes
  the `RecursionError` gap for pathological plan nesting. Documented in D2.

## 7. Newly discovered questions

1. **Enforcement-point message variance:** the same violation (depth) can be
   reported from the router ("provider response JSON nesting depth exceeds
   maximum …") or from `from_dict` ("plan: JSON nesting depth exceeds
   maximum …") depending on which layer trips first. Both are
   `PlanSchemaValidationError`; operators get the same category. Acceptable;
   noted for any future error-message unification pass.
2. **`generate` shares the byte bound** with `plan_structured` (both go
   through `_chat`): a >256 KB free-form generation now fails. Intended
   (same untrusted-input boundary) and tested.
3. **Byte bound counts the envelope**, not just the plan content — a 200 KB
   plan inside a 260 KB envelope is rejected even though the plan alone
   would fit. Intentionally conservative; the content-level structural caps
   still apply within the envelope budget.
4. **Config surface grew by five env vars** (output bounds) beyond the
   original M1 list — this is the ADR-057 D2 "overridable by config" surface,
   not a second config system; documented in the ADR.

## 8. Next steps (not started)

M3 (fallback composition + `model.fallback`), M4 (reflection wiring), M5
(runtime opt-in + live harness). Awaiting explicit approval.
