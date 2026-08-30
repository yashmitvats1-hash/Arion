# M4 Completion Report — ADR-057 Model Reflection Wiring + Provenance

- **Date:** 2026-08-30
- **Scope:** M4 of ADR-057 (`docs/adr/057-model-backed-intelligence-path.md`,
  approved). M5 (runtime/CLI opt-in + live harness) intentionally NOT
  implemented.
- **Baseline (M3, accepted):** 1,646 passed / 2 skipped / 21 demos / 87%
  coverage; PR #10 open (4 commits, mergeable). Authority model, persistence
  schema, scheduler, locks, registry semantics, authorization model,
  approval, execution and recovery unchanged by M4.

## 1. Architecture / wiring implemented

Model-reflector selection at the bootstrap/engine seam
(`arion/bootstrap.py::build_engine`, additive params `reflector` and
`model_config`):

```
explicit reflector= provided?
  ├─ yes -> used unconditionally (injection wins)
  └─ no, memory enabled?
       ├─ model_config is not None AND model_config.enabled
       │    AND model_config.reflection_enabled
       │     -> ModelReflector(build_router(model_config, sink=events))
       └─ otherwise -> DeterministicReflector()
  memory disabled -> reflector stays None
```

- `ARION_LLM_REFLECTION` is consumed through the existing
  `ModelProviderConfig.reflection_enabled` field (`load_model_config`
  parses it); when `reflection_enabled=False`, a provider is configured but
  the model reflector is never instantiated and zero model reflection calls
  occur. No new configuration system: the existing `ModelProviderConfig` /
  `build_router` infrastructure is reused.
- Deterministic behavior with no provider configured is unchanged (the
  prior default `DeterministicReflector() if memory else None`).
- No CLI flags, no runtime env-driven bootstrap wiring, no live harness
  (all M5 — absent).

## 2. Provenance (`source` marker)

- Additive `last_source` seam: `DeterministicReflector.last_source =
  "deterministic"` (init + reaffirmed per reflection);
  `ModelReflector.last_source = "model"` (init + on successful validation).
  `Reflector` protocol docstring documents it as optional.
- `reflection.created` detail now carries the additive `"source"` key:
  `{"reflection_id", "episode_id", "source"}` — existing fields preserved
  byte-for-byte.
- Engine-created deterministic fallback (any model reflection failure) is
  always marked `"deterministic"`. Custom reflectors without the seam
  default to `"deterministic"` via `getattr(..., None) or "deterministic"`.
- Provenance is audit metadata only: it never enters authorization or the
  deterministic guidance authority model (asserted by tests).

## 3. Failure / retry / fallback behavior (unchanged contract, now tested)

- Model reflection gets **exactly one provider call**; **no retries** on
  provider failure, malformed JSON, or forbidden authority fields.
- Any model reflection failure → immediate `DeterministicReflector`
  fallback; `reflection.validation.failed` retains `fallback:
  "deterministic"`; the task and memory path stay best-effort (task
  completes even when the model reflector fails).
- Reflection failures never route to human approval; no new durable
  blocker/state is introduced.

## 4. Replay / idempotency

- The durable reflection claim (`record_reflection` first-writer-wins) and
  the `existing.reflection_id` short-circuit in `_record_memory` are
  unchanged. Verified: a fully learned episode is never re-queried — a
  fresh process running `learn_from_terminal_tasks` and a direct
  `_record_memory` replay both produce **zero model calls**.

## 5. Files changed

| File | Change |
|---|---|
| `arion/bootstrap.py` | `build_engine` gains `reflector` + `model_config`; reflector selection logic (explicit wins → provider+enabled → ModelReflector → DeterministicReflector → None). |
| `arion/memory/reflector.py` | `DeterministicReflector.last_source = "deterministic"`; `Reflector` protocol docstring documents the optional seam. |
| `arion/memory/model_reflector.py` | `ModelReflector.last_source = "model"` (init + on success). |
| `arion/memory/reflection_schema.py` | `validate_reflection_dict` defaults `created_at` to `utcnow()` when the model output omits it (fixes a latent bug — see §8). |
| `arion/orchestration/engine.py` | `_record_memory` tracks `reflection_source`; `reflection.created` detail gains additive `"source"`; engine-created fallback marked `"deterministic"`. |
| `tests/test_model_reflection_wiring.py` (new, 17) | See §6. |
| `docs/adr/057-…` | Status → M1+M2+M3+M4; M4 section marked implemented. |

M1/M2/M3 files are **byte-identical to the M3 commit** (verified: config,
providers, plan_schema, model_planner, planner, goals, events, all M3
tests). No M5 functionality: CLI, authz, state/schema, scheduler, locks,
approval, recovery untouched.

## 6. Tests added (`tests/test_model_reflection_wiring.py`, 17)

- **Selection (6):** explicit `reflector=` wins over automatic selection;
  provider+enabled → `ModelReflector`; no provider → `DeterministicReflector`;
  `reflection_enabled=False` → `DeterministicReflector` + zero
  `reflection.requested` events; `ARION_LLM_REFLECTION` consumption via
  `load_model_config`; memory off → `None`.
- **Provenance (4):** model success → `reflection.created.source == "model"`
  with existing fields intact; deterministic → `"deterministic"`;
  engine-created fallback → `"deterministic"` + `reflection.validation.failed`
  keeps `fallback:"deterministic"`; custom reflector without the seam →
  defaults to `"deterministic"`.
- **Failure/no-retry (3):** provider failure exactly 1 call, no retry;
  malformed JSON → immediate fallback, 1 call; forbidden authority-bearing
  reflection → immediate fallback, 1 call.
- **Replay (2):** fully learned episode → zero model calls on
  `learn_from_terminal_tasks` and direct `_record_memory` replay; existing
  reflection claim never duplicated.
- **Security (2):** a valid model reflection whose text attempts authority
  claims cannot grant authorization / change actor / scope / registry /
  executed steps; reflection alone cannot cause execution (executed steps
  exactly equal the plan's).

## 7. Gate results

| Check | Result |
|---|---|
| M4 tests (`tests/test_model_reflection_wiring.py`) | 17 passed |
| Model-path/reflection/security suites (19 files incl. poisoning, memory security, guidance authority, belief invariant, cognition authority, consolidation invariant, learning, M3 fallback, transport retry, output limits, plan hardening, audit) | 331 passed |
| Full authoritative suite | **1,663 passed / 2 skipped / 0 failed** (1,646 M3 + 17 M4) |
| Demos | 21/21 passed |
| Coverage | **87%** (10,692 stmts / 1,360 missed) — gate ≥87% met |
| Diff review | Only the M4 delta vs `33febaf`; M1/M2/M3 byte-identical; no M5 artifacts |

## 8. Deviations / issues discovered

1. **Latent bug fixed (pre-existing, exposed by M4):** `validate_reflection_dict`
   set `created_at=d.get("created_at")` which is `None` when the model
   output omits the field, while the `reflections` table declares
   `created_at TEXT NOT NULL`. A successful model reflection therefore
   silently failed to persist (`IntegrityError` swallowed by
   `record_reflection`'s first-writer-wins branch), and `link_reflection`
   linked a phantom reflection id. Fixed by defaulting `created_at` to
   `utcnow()` in the validation layer. No existing test caught this
   because `ModelReflector` was never auto-selected before M4. This is a
   reflection-output normalization fix — no schema, authority, or
   persistence-semantics change.
2. **`ARION_LLM_REFLECTION` consumed via `load_model_config`, not read
   directly by `build_engine`:** M4 selects the reflector from a supplied
   `ModelProviderConfig`; env-driven bootstrap/CLI wiring (calling
   `load_model_config` at runtime) is M5 by design and stays out of scope.
3. **Provider registry name:** `openai-compatible` (not `openai-compat`) is
   the registered adapter name; tests use the correct name.
4. Minor (pre-existing, untouched): `ModelReflector._emit` uses a dynamic
   `__import__`; `hash()`-derived reflection ids vary across processes
   (harmless — the durable claim is episode-keyed).

## 9. Next steps (not started)

- **M5 — Runtime/CLI opt-in + live harness**: bootstrap/CLI env-driven
  wiring (`load_model_config` consumed at runtime), `ARION_LLM_FALLBACK=0`
  strict-mode opt-in, scripted live-provider harness replacing the smoke
  test. Requires explicit approval after M4 review.

**M5 remains unimplemented.** PR #10 stays open and unmerged; no history
rewritten.
