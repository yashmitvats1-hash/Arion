# M9-A Opt-in Mechanism — Explicit Selection

**Status:** Verified from source; no code change required (bootstrap already correct).

## How model-backed planning is selected (ADR-057 M5)

The `build_engine()` bootstrap (`arion/bootstrap.py`) resolves `model_config` in this order:

1. **Explicit argument wins:** if the caller passes `model_config=` to `build_engine()`, it is used directly.
2. **Environment read:** if `model_config` is `None`, `load_model_config()` parses `ARION_LLM_*` environment variables.
3. **Fail-closed default:** if `provider` is unset/`""`/`"none"` or `enabled` is false, `build_router()` returns `None`; planner stays `DeterministicPlanner`; router stays `DeterministicRouter`.

Therefore: **no environment variable accidentally activates the model path** — an empty/default environment produces the exact deterministic spine. Only an explicit `ARION_LLM_PROVIDER=openai-compatible` (plus `ARION_LLM_MODEL`, `ARION_LLM_BASE_URL`, `ARION_LLM_API_KEY`) activates `RealModelPlanner`.

## Environment surface (from `arion/intelligence/config.py`)

| Variable | Purpose | Fail-closed default |
|---|---|---|
| `ARION_LLM_PROVIDER` | Provider adapter name (`openai-compatible`) | `""` / `"none"` → deterministic |
| `ARION_LLM_MODEL` | Model identifier | required only when provider enabled |
| `ARION_LLM_BASE_URL` | Endpoint URL | required only when provider enabled |
| `ARION_LLM_API_KEY` | Credential (env only, never logged/stored) | required only when provider enabled |
| `ARION_LLM_TIMEOUT_SECONDS` | Transport timeout | default |
| `ARION_LLM_MAX_RETRIES` | Transport retry budget | default |
| `ARION_LLM_FALLBACK` | `True` / `False` — deterministic fallback after model failure | default `True` (defensive) |
| `ARION_LLM_REFLECTION` | `True` / `False` — model-backed reflection | default `False` |
| `ARION_LLM_MAX_RESPONSE_BYTES` | Envelope cap (default 262,144) | bound enforced |
| `ARION_LLM_MAX_JSON_DEPTH` | JSON nesting cap (default 10) | bound enforced |
| `ARION_LLM_MAX_PLAN_STEPS` | Step count cap (default 100) | bound enforced |
| `ARION_LLM_MAX_PARAMS_PER_STEP` | Params key cap (default 32) | bound enforced |
| `ARION_LLM_MAX_STEP_STRING` | Per-step string cap (default 2,000) | bound enforced |

## Explicit opt-in for CLI / operators

No source edit to CLI was required — `build_engine()` is called by bootstrap with no arguments by default, which reads env optionally. To make opt-in **visible in CLI**, operators can invoke:

```bash
ARION_LLM_PROVIDER=openai-compatible ARION_LLM_MODEL=gpt-4 \
ARION_LLM_BASE_URL=https://... ARION_LLM_API_KEY=... \
  arion run "summarize this repository"
```

Or pass `model_config` programmatically:

```python
from arion.intelligence.config import load_model_config
from arion.bootstrap import build_engine
engine = build_engine(db_path, root, model_config=load_model_config())
```

**No hidden activation:** the deterministic path requires zero environment; model path requires all four env vars set correctly. If `provider` is misspelled, `build_router()` raises `ProviderConfigurationError` (fail-closed, never silent fallback to another provider).

## Verification (done by inspection / existing tests)

- `tests/test_model_config.py` verifies env parsing, enabled/disabled, credential masking.
- `tests/test_model_planner.py` verifies `RealModelPlanner` behaves correctly when router provided.
- `test_m9a_model_integration.py` proves the full path with `FakeRouter`.
- No new code needed to make opt-in explicit — architecture already requires explicit choice.
