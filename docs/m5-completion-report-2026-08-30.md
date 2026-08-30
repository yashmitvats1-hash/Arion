# M5 Completion Report — ADR-057 Opt-in Runtime + Live Harness

- **Date:** 2026-08-30
- **Scope:** M5 of ADR-057 (`docs/adr/057-model-backed-intelligence-path.md`,
  approved; operator approved M5 with locked decisions 1–6).
- **Baseline (M4, accepted):** 1,663 passed / 2 skipped / 21 demos / 87%
  coverage; PR #10 open (5 commits, mergeable), head `e4e2ff0`. Authority
  model, persistence schema, scheduler, locks, registry semantics,
  authorization model, approval, execution and recovery unchanged by M5.

## 1. Architecture / wiring implemented (`arion/bootstrap.py`)

`build_engine(..., model_config=None)` now reads the environment:

```
resolved_config = model_config if model_config is not None
                  else load_model_config()          # decision 2 (env read)
if resolved_config.enabled:                          # D8: no provider -> None
    model_router = build_router(resolved_config, sink=events)
if planner is None:
    planner = RealModelPlanner(model_router, events=events,
                 fallback_enabled=resolved_config.fallback_enabled)
             if model_router is not None else DeterministicPlanner()
if router is None:
    router = model_router if model_router is not None
                          else DeterministicRouter(planner)
if reflector is None and memory:
    reflector = ModelReflector(model_router, events=events)
                if resolved_config.enabled and model_router is not None
                and resolved_config.reflection_enabled
                else DeterministicReflector()
memory off -> reflector stays None
```

- **Env-driven opt-in:** a configured+enabled provider selects the
  model-backed path (`RealModelPlanner` + shared `OpenAICompatModelRouter`
  + `ModelReflector` when `reflection_enabled`). **No provider keeps the
  deterministic spine byte-for-byte** (deterministic default regression
  test). `ARION_LLM_FALLBACK=0` → `fallback_enabled=False` (strict typed
  failure, no deterministic fallback). `ARION_LLM_REFLECTION=0` →
  `DeterministicReflector`, zero model reflection calls.
- **One shared model router** instance serves both `RealModelPlanner` and
  `ModelReflector` (decision 5).
- **Explicit injection wins:** `planner=` / `router=` / `reflector=`
  always override env-driven selection (decision 3).
- **No new env vars** (decision 6): only the existing `ARION_LLM_*`
  surface via `load_model_config()`. **No CLI changes, no startup
  logging** (decision 4). No duplicated config logic: `ModelProviderConfig`
  + `build_router` machinery reused as-is.
- **No second execution/authorization path:** authority, schema, adapter,
  planner, reflector, persistence and event contracts are untouched;
  M5 is wiring + tests only (verified by diff review, §7).

## 2. Harness B — smoke tests (`tests/smoke/test_live_provider.py`, decision 1)

The single env-gated live smoke was replaced with a two-tier harness:

- **Tier 1 — scripted local-server scenarios (offline, always run):** a
  local `ThreadingHTTPServer` plays the OpenAI-compatible
  `/chat/completions` protocol so the REAL adapter + transport +
  bootstrap run end-to-end with zero credentials and no network beyond
  loopback: happy path (provider → JSON → PlanSchema → PlanValidator →
  PlanSteps), malformed response → validation failure → deterministic
  fallback, provider down (connection refused) → `ProviderUnavailable` →
  deterministic fallback, and strict mode (`ARION_LLM_FALLBACK=0` + HTTP
  500) → FAILED with no fallback and no capability execution.
- **Tier 2 — live-provider scenario (externally gated):** runs only when
  `ARION_LLM_BASE_URL` / `ARION_LLM_API_KEY` are configured (legacy
  gating contract preserved). It exercises the same env-driven
  `build_engine` path against the real endpoint, with the reachability
  pre-check retained. The test completes the config surface for the
  env-driven runtime by defaulting `ARION_LLM_PROVIDER` to
  `openai-compatible` (the single registered provider) and
  `ARION_LLM_MODEL` to the legacy router default `gpt-4o-mini` when the
  operator set only the endpoint vars (§8).

## 3. Runtime security invariants (verified by tests)

- **Compromised model cannot self-authorize:** a forged plan demanding
  `shell.exec` is rejected by schema validation and falls back; only the
  deterministic plan's `filesystem.read` steps execute; `shell.exec` never
  appears in steps, checks, or the registry.
- **Fallback cannot bypass authorization:** a provider-500 fallback plan
  is DENIED by a custom boundary (no `capability.executed`).
- **Hostile model reflection cannot affect authority:** a reflection
  demanding `shell.exec`/grant leaves checked scopes at `filesystem:read`,
  actor `agent:system`, and the registry without `shell.exec`.
- **Secrets invariant:** `ARION_LLM_API_KEY` reaches the provider only in
  the Authorization header; it is absent from request bodies, events,
  JSONL, the durable SQLite store, memory, and `ModelProviderConfig`
  repr (`<redacted>`).
- **Strict mode fails closed:** `ARION_LLM_FALLBACK=0` + provider failure
  → durable typed task FAILED, no `model.fallback`, no `capability.executed`.
- **Reflection no-retry:** a failing model reflection makes exactly ONE
  provider call, then immediate deterministic fallback.
- **Malformed config fails typed:** unknown provider / non-integer
  `ARION_LLM_MAX_RETRIES` → `ProviderConfigurationError` naming the var.

## 4. Persistence / replay

- A model-produced plan persists; after restart the stored-plan fast path
  replays it with **zero plan re-queries** (plan request count frozen, no
  `planning.requested` / `model.response.received` events on the replayed
  task, `plan.produced` carries `source:"stored"`). The only new model
  call after restart is the by-design post-completion reflection of the
  freshly executed task (M3), not a re-query.

## 5. Files changed

| File | Change |
|---|---|
| `arion/bootstrap.py` | M5 env-driven selection: `load_model_config()` when `model_config is None`; shared `model_router`; auto `RealModelPlanner(fallback_enabled=…)`; router/reflector follow the resolved config; explicit `planner=`/`router=`/`reflector=` win. |
| `tests/test_model_runtime_wiring.py` (new, 16) | Offline runtime wiring matrix (§6). |
| `tests/smoke/test_live_provider.py` | Harness B rewrite: 4 scripted offline scenarios + env-gated live scenario (§2). |
| `docs/adr/057-model-backed-intelligence-path.md` | Status → M1–M5 implemented; M5 milestone marked implemented; unresolved-question 4 updated (env-only locked). |

All other tracked files are **byte-identical to `e4e2ff0`** (verified by
`git status` + full-tree `cmp` scan — see §7). No M6/scope creep: no new
env vars, no CLI flags, no startup logging, no second execution path, no
authority/schema/persistence/event-contract changes.

## 6. Tests added

`tests/test_model_runtime_wiring.py` (16, fully offline via a scripted
local HTTP server speaking the OpenAI-compatible protocol):

- env provider → `RealModelPlanner` + shared router + `ModelReflector`
  (one instance shared by planner and reflector);
- no provider → deterministic planner/router/reflector (byte-for-byte);
- explicit `planner=`/`router=`/`reflector=` overrides env; explicit
  `model_config=None` → env read;
- `ARION_LLM_FALLBACK=0` strict (500 → FAILED, no `model.fallback`, no
  `capability.executed`);
- `ARION_LLM_REFLECTION=0` → deterministic reflector, zero reflection
  requests, plan still model-backed;
- malformed config → typed `ProviderConfigurationError` naming the var;
- model-plan persistence + restart → zero plan re-query (§4);
- full model → validation → authorization → execution → verification →
  memory → model reflection chain (`plan.produced source:"model"`,
  `reflection.created source:"model"`, permission scopes `filesystem:read`);
- approval-required model task → AWAITING_APPROVAL → approve → complete;
- compromised model cannot self-authorize; fallback cannot bypass
  authorization; hostile reflection cannot affect authority;
- secrets absent from events/JSONL/checkpoints/memory/repr;
- reflection no-retry (exactly one call on failure);
- deterministic default regression (no `planning.requested` / model
  responses; `plan.produced source:"deterministic"`).

`tests/smoke/test_live_provider.py` (4 offline + 1 gated): §2.

## 7. Gate results

| Check | Result |
|---|---|
| M5 tests (`tests/test_model_runtime_wiring.py`) | 16 passed |
| M5 smoke harness (`tests/smoke/`) | 4 passed, 1 skipped (live env-gated); tier-2 live scenario verified separately against a real HTTP endpoint (1 passed) |
| M1–M4/model-path suites (20 files: model_backed_planner, model_config, model_fallback, model_output_limits, model_planner, model_router, model_reflection_wiring, transport_retry, memory_reflection, memory_security, reflection_validation, reflection_invariant, poisoning, audit, plan_hardening, belief_invariant, cli_approvals, approval, memory_lifecycle) | 307 passed |
| Full authoritative suite | **1,683 passed / 2 skipped / 0 failed** (1,663 M4 + 16 wiring + 4 smoke tier-1) |
| Demos | 21/21 passed |
| Coverage | **87%** (10,700 stmts / 1,349 missed) — gate ≥87% met |
| Diff review | Only the 4 M5 files differ from `e4e2ff0`; all other tracked files byte-identical (full-tree `cmp` scan); no scope creep |
| Tree | `git status` shows exactly the 3 M5-modified files + 1 new test file; no stray tracked artifacts |

## 8. Deviations / notes

1. **Explicit `model_config=` now drives planner/router selection too**
   (previously it influenced only the reflector). This is the natural
   consequence of the approved wiring (`resolved_config = model_config if
   model_config is not None else load_model_config()`): the config object
   IS the selection input, the environment is just its default source, and
   explicit `planner=`/`router=`/`reflector=` still win. No existing
   caller relied on the old behavior (full suite green).
2. **Live tier-2 env completion:** the externally gated scenario keeps the
   legacy `ARION_LLM_BASE_URL`/`ARION_LLM_API_KEY` gating but routes
   through the env-driven `build_engine` path; the test defaults
   `ARION_LLM_PROVIDER` → `openai-compatible` and `ARION_LLM_MODEL` →
   `gpt-4o-mini` (the legacy router default) when the operator set only the
   endpoint vars. Documented in the test docstring.
3. **`plan.produced source:"stored"`** (M4/D3 additive provenance) is
   asserted by the M5 replay test — no change to the marker, only coverage.
4. **Carry-forwards unchanged:** 21-vs-23 demo discrepancy,
   `demo_adr024` flake, expanded `generate` retry behavior, redirected
   pytest-summary loss — all still apply; nothing in M5 affects them.
5. **Bootstrap edit hygiene:** one transient blank-line removal during the
   wiring edit was restored; final `bootstrap.py` diff is the approved
   selection block only (plus import lines and comment updates).

## 9. Conclusion

ADR-057 M1–M5 is complete. The model-backed intelligence path is now
opt-in at runtime purely through the existing `ARION_LLM_*` environment
surface, shares one model router across planner and reflector, honors
explicit DI over environment selection, preserves the deterministic spine
byte-for-byte when no provider is configured, fails closed in strict mode,
and cannot grant authority to the model under any tested adversarial
condition. All gates pass; committed as a single logical M5 commit on top
of `e4e2ff0` and pushed to PR #10 (not merged).
