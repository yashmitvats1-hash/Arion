# M9-A Operator Guide — Model-Backed Planner Integration

**Status:** Completed proof slice (not full M9-A). Use for opt-in only.

## How to enable (explicit — never accidental)

Set all required environment variables (fail-closed if any missing):

```bash
export ARION_LLM_PROVIDER=openai-compatible
export ARION_LLM_MODEL=gpt-4
export ARION_LLM_BASE_URL=https://api.openai.com/v1
export ARION_LLM_API_KEY=<key>
```

Then run with the existing `arion` command; `build_engine()` detects the
configuration automatically. No CLI subcommand change is required — the
opt-in is visible because the environment must be explicitly set.

If you want determinism, unset `ARION_LLM_PROVIDER` (or set to `"none"`).
The deterministic spine activates with zero environment changes.

## Trust boundaries (must never change during M9-A)

- **Model proposes; planner/validator checks; authorization decides;**
  capability executes; verification establishes outcome.
- **Authorization uses `ActionSpec.required_scope` from registry, not the
  plan's claimed scope.** The model never grants permission.
- **Resource policy uses `ResourcePolicy` / `ADR-061` declarations, not
  model params.** The model never changes a boundary.
- **Verification uses `VerificationPolicy` from registry, not model output.**
- **Approval requires `REQUIRE_APPROVAL` from metadata + human action;**
  model cannot approve.
- **Reflection (`ModelReflector`) validates strictly against schema;**
  authority-bearing fields (`scope`, `permissions`, `authorize`, etc.) are
  rejected and never stored.

## What remains authoritative

All existing layers unchanged: `CapabilityRegistry`, `PermissionPolicy`,
`ResourcePolicy`, `ApprovalHandler`, `ActionSpec`, `PlanValidator`,
`VerificationPolicy`. M9-A only adds a planner option; nothing else
is replaced or weakened.

## Provider / configuration requirements

- Only `openai-compatible` adapter registered (`PROVIDER_REGISTRY`).
- Credentials read from env only; never persisted; never logged; never in
  audit events; never in `repr`/`str`.
- Transport injectable (`transport=` to adapter) for tests / proxies.
- Output bounded by `max_response_bytes`, `max_json_depth`, `max_plan_steps`,
  `max_params_per_step`, `max_step_string`. Violations = typed failure,
  never silent degradation.
- Retry separated: M1 transport retry (adapter) vs M3 semantic retry (planner).
  Semantic retries bounded by `semantic_max_retries` (default 2).
- Fallback (`fallback_enabled`) produces deterministic plan, emits
  `model.fallback` event with bounded metadata (`reason`, `attempts`,
  `fallback:"deterministic"` — never prompts, responses, credentials).

## Known limitations (M9-A proof slice — not production-complete)

- Only proof slice tested with `FakeRouter`; live endpoint smoke exists
  (`tests/smoke/test_live_provider.py`) but is excluded from default run.
- No CLI `--model` flag added; opt-in requires env only.
- No multi-provider routing.
- No persistent model-state integration (reflection is optional; guidance
  is informational only).
- No automatic replanning with model feedback (existing engine replanning
  is sufficient for M9-A).
- No vector DB / RAG / browser / agent / multi-agent.
- Capability vocabulary remains 7 actions (M9-B deferred).

## Security checks to verify before any production use

1. Confirm `ARION_LLM_API_KEY` is not in shell history / log files /
   environment dumps that persist.
2. Confirm `build_router()` raises on unknown provider (fail-closed).
3. Confirm `PlanValidator` rejects unknown capabilities / actions.
4. Confirm `ResourcePolicy` denies out-of-bound resources from model plans.
5. Confirm `ModelReflector` rejects reflections containing authority fields.
6. Confirm audit events contain no raw provider content.
7. Confirm `fallback_enabled` is `True` in production (defensive default).

## Related

- `docs/m9-architecture-sequencing-proposal.md` — full audit, 14 Q&A,
  M9-A definition of done, security model, sequencing.
- `docs/adr/ADR-061-resource-role-declaration-boundary-check.md` —
  multi-resource authorization (M8 C4) that model output must respect.
- `docs/m9a-opt-in.md` — verification of bootstrap mechanism.
