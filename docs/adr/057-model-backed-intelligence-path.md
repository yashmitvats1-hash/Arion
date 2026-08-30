# ADR-057 — Model-Backed Intelligence Path

- **Status:** Approved (2026-08-29) — M1 (Provider Configuration + Bounded
  Transport Retry), M2 (Model Output Size / Depth Limits),
  M3 (Fallback Composition) and M4 (Reflection Wiring) implemented;
  M5 pending approval.
- **Date:** 2026-08-29
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)
- **Baseline checkpoint:** M0 accepted — 1,478 passed / 2 skipped / 23/23 demos /
  87% coverage; authority model, persistence and schema unchanged;
  `6f7e493a7a9c26b31697e42a86bcf9109c8ff5a4`.

## Context

Arion's deterministic spine is complete and proven: orchestration, authorization,
approval, recovery, mutation locks, cross-process scheduler, memory, cognition,
CLI. The model-backed pieces that already exist — `RealModelPlanner`,
`OpenAICompatModelRouter`, `ModelReflector` — are implemented behind the
`Planner` / `ModelRouter` / `Reflector` seams and unit-tested, but:

1. **They are not wired by default.** `bootstrap.build_engine` constructs the
   deterministic planner/router and a `DeterministicReflector`; the CLI passes
   no configuration; a live provider is exercised only by one gated smoke test
   (`tests/smoke/test_live_provider.py`).
2. **There is no configuration surface** (no provider/model/endpoint selection
   beyond `ARION_LLM_*` env vars read inside the adapter constructor), no
   retry policy, no rate-limit distinction (HTTP 429 currently maps into
   `ProviderConfigurationError`), no timeout knob at the router level, and no
   fallback semantics.
3. **There are no explicit size/depth limits** on model output (`PlanSchema`
   requires a non-empty step array but bounds neither step count, parameter
   count, string lengths, nor response size).
4. **There is no precise fallback contract.** Today a provider failure makes
   `_plan` fail the task durably ("planning failed: …"); whether Arion should
   retry, degrade to the deterministic planner, or fail is unspecified.

This ADR designs the complete boundary for a first-class, configurable
model-backed intelligence path. It is **design only**: it specifies what will
be implemented in the next phase and the invariants that implementation must
preserve.

## Governing invariant

> **The model may propose. Arion decides.**

The model is an intelligence component **inside** the intelligence layer. It
can influence *what* Arion considers doing (a structured proposal) and *what
Arion remembers* (an informational reflection). It can never become an
authority source: it cannot grant, deny, approve, bypass, escalate, own, or
record authority. Every downstream authority mechanism is unchanged:

**authorization ≠ coordination ≠ memory**

## The complete boundary

```
 user goal
   │  (1) CLI / API: text + actor (trusted caller)
   ▼
 orchestration  (engine owns the loop — deterministic)
   │  (2) Planner.plan(goal, task_id, registry, context) → list[PlanStep]
   ▼
 model/provider  (intelligence layer; the ONLY non-deterministic step)
   │  (3) router.plan_structured(goal, catalog, context) → PlanSchema
   ▼
 structured cognitive output
   │  (4) strict schema parse + size/depth limits (PlanSchema v1.0)
   ▼
 validation / normalization
   │  (5) PlanValidator vs LIVE registry + status normalization
   ▼
 existing planning representation
   │  (6) PlanStep[] → immutable plan version → exact-plan task claim
   ▼
 capability requirements
   │  (7) planner.required_capabilities() gate (deterministic heuristic)
   ▼
 authorization
   │  (8) PermissionPolicy on live ActionSpec (scope/risk/resource/boundary)
   ▼
 approval
   │  (9) durable approval queue; fingerprint re-validation; expiry
   ▼
 scheduler / locks / execution
   │ (10) durable scheduler claim + lease; mutation lock + FIFO; execution
   ▼
 verification
   │ (11) deterministic postcondition checks
   ▼
 memory / learning
   │ (12) episode + reflection (+model reflector) + beliefs + consolidation
```

### Boundary-by-boundary contract

| # | Boundary | Input | Output | Trust | Validation | Failure behavior | Persistence | Model influence |
|---|---|---|---|---|---|---|---|---|
| 1 | Interface → orchestration | goal text, actor | `Goal` | caller-trusted | CLI parsing | CLI error | goal row | none |
| 2 | Orchestration → planner | `(goal, task_id, registry, PlanningContext)` | `list[PlanStep]` | planner is a seam | protocol contract; `required_capabilities` gate | typed planning error → task FAILED or fallback | task/plan rows | full (proposal only) |
| 3 | Planner → provider | `(goal, catalog, digest)` | `PlanSchema` | provider = untrusted | schema v1.0 strict parse | typed `ModelPlanError` subclasses | metadata event only | full (proposal only) |
| 4 | Raw model output → schema | JSON text | `PlanSchema` | untrusted | strict keys, types, forbidden fields, size/depth caps | `PlanSchemaValidationError` / `MalformedProviderResponseError` | nothing | cannot exceed schema |
| 5 | Schema → steps | `PlanSchema` | `PlanStep[]` | untrusted | `PlanValidator` vs live registry; status normalization | `PlanCapabilityValidationError` | nothing | cannot exceed registry |
| 6 | Steps → plan authority | steps | immutable plan version + canonical task | system | ADR-050/051 claim | plan persistence failure → task FAILED | `goal_plans`, task row | cannot bypass |
| 7 | Capability gate | goal text | required set | deterministic | heuristic | BLOCKED goal | blocker | none (heuristic) |
| 8 | Authorization | request | ALLOW/DENY/REQUIRE_APPROVAL | system | live `ActionSpec` + policy + boundaries | DENY fails step | `permission.*` events | none |
| 9 | Approval | request | PENDING/APPROVED/DENIED/EXPIRED | human | fingerprint + live re-validation | pause/deny/expiry | approval rows | none |
| 10 | Execution | step | observation | capability | lock/lease/CAS | recovery fencing | task/checkpoint/locks | none |
| 11 | Verification | observation | pass/fail | system | deterministic policies | retry-safe retry or failure | events | none |
| 12 | Learning | terminal task | episode/reflection/beliefs | informational | structured schemas | best-effort; deterministic fallback | memory tables | reflection text only |

---

## Decisions

### D1 — Provider abstraction

**Keep the `ModelRouter` protocol unchanged** (`generate`, `plan_structured`)
— it is already provider-neutral and correct. Add the missing plumbing around it:

1. **Configuration object.** New `ModelProviderConfig` dataclass
   (`arion/intelligence/config.py`):
   - `provider: str | None` — `None`/`""`/`"none"` means **no model path**
     (deterministic spine, exactly today's behavior); `"openai-compatible"`
     selects the existing adapter. Registered adapter map:
     `PROVIDER_REGISTRY = {"openai-compatible": OpenAICompatModelRouter}` —
     a new provider is one adapter + one registry entry; nothing outside
     `intelligence/` changes.
   - `model: str`, `base_url: str | None`, `api_key: str | None`
     (never persisted, never logged, repr-redacted).
   - `timeout_seconds: float = 60.0`, `max_retries: int = 2`,
     `retry_backoff_base: float = 1.0`, `retry_backoff_max: float = 8.0`.
   - `fallback_enabled: bool = True` (D5), `reflection_enabled: bool = True`.
   - `load_model_config()` reads `ARION_LLM_PROVIDER`, `ARION_LLM_MODEL`,
     `ARION_LLM_BASE_URL`, `ARION_LLM_API_KEY`, `ARION_LLM_TIMEOUT_SECONDS`,
     `ARION_LLM_MAX_RETRIES`, `ARION_LLM_FALLBACK`, `ARION_LLM_REFLECTION`.
     Unknown provider name → **typed `ProviderConfigurationError`** (fail
     closed, never silently fall back to another provider).
2. **Factory.** `build_router(config, sink=None) -> ModelRouter | None` in
   `arion/intelligence/providers/__init__.py`. Returns `None` when no provider
   is configured — this is the deterministic default.
3. **Transport-level retries** live inside the provider adapter (the only
   place that knows the transport): bounded retries with deterministic
   exponential backoff for **transient** failures only (network error, timeout,
   HTTP 5xx, HTTP 429). Never retry 4xx config/auth errors. Retries are
   idempotent-safe: a plan request is a pure read/propose operation.
4. **Rate limits.** New typed error `ProviderRateLimitError` (category
   `provider_rate_limit`) raised on HTTP 429; honors `Retry-After` within the
   retry budget, then propagates. (Today 429 lands in
   `ProviderConfigurationError` — this is an additive correction.)
5. **Observability.** `model.response.received` (existing) carries
   provider/model/latency_ms/tokens/summarized error. Add the event kind
   `model.retry` (bounded: attempt, delay_ms, category) so retries are
   auditable. Raw prompts/responses never enter events (unchanged, ADR-011/034).

### D2 — Structured model output

**Wire contract stays `PlanSchema` v1.0** (`arion/intelligence/plan_schema.py`)
— no schema change, no migration. The ADR adds the missing *limits* and makes
the contract's guarantees explicit:

1. **Schema (unchanged, reaffirmed):** top-level `{version, intent, steps}`;
   per step `{intent, capability, action, params, verification, depends_on}`;
   strict unknown-field rejection; `FORBIDDEN_STEP_FIELDS` and
   `RESERVED_PARAM_KEYS` (scope, resource_kind, resource_param, risk,
   side_effects, idempotent, retry_safe, reversible, permissions, actor,
   approve, grant, authorization, boundary, allowed) rejected with a
   specific message; verification policies limited to
   `non_empty | schema_keys`; `depends_on` forward-only, no duplicates;
   version must match exactly.
2. **Size/depth limits (implemented, M2):** defaults —
   `MAX_PLAN_STEPS = 100`, `MAX_PARAMS_PER_STEP = 32`, `MAX_STEP_STRING = 2000`
   chars, `MAX_JSON_DEPTH = 10`, `MAX_MODEL_RESPONSE_BYTES = 262_144`.
   Over-limit input is rejected with the existing typed error family
   (`PlanSchemaValidationError` for structure, `MalformedProviderResponseError`
   for byte-size). Limits are constants in `plan_schema.py`, overridable by
   config (`ARION_LLM_MAX_RESPONSE_BYTES`, `ARION_LLM_MAX_JSON_DEPTH`,
   `ARION_LLM_MAX_PLAN_STEPS`, `ARION_LLM_MAX_PARAMS_PER_STEP`,
   `ARION_LLM_MAX_STEP_STRING`), and covered by tests.
   Enforcement points (before a plan can be accepted): the router bounds the
   raw envelope byte size and raw nesting depth before parsing; the router
   bounds the plan-content nesting depth before `json.loads(content)`; and
   `PlanSchema.from_dict` bounds depth, step count, params count, and
   step-level string lengths (step intent/capability/action, param keys and
   string values, verification keys, and the top-level `intent` — the latter
   two are narrow applications of `MAX_STEP_STRING` to the same "individual
   string size" bound). Violations are deterministic typed failures: never
   retried, never fallback, never persisted, never a crash. All bounds are
   enforced iteratively (no reliance on Python recursion behavior).
3. **Unknown-field behavior (unchanged):** reject. Forbidden-field behavior:
   reject with the explicit "model cannot set …" message. Malformed JSON:
   `MalformedProviderResponseError`. None of these ever reach execution.
4. **Normalization:** the engine already normalizes any forged step status to
   `PENDING`/`SKIPPED` (ADR-025 Phase H) — reaffirmed; the model can never
   mint execution state.
5. **Versioning strategy:** `PLAN_SCHEMA_VERSION` is strict-match (unchanged).
   Evolution policy: additive optional fields → new schema version with a
   backward-compatible reader; breaking changes → new version; unknown
   versions are rejected with a typed error. The wire contract and the
   persisted `plan_summary` format share the version.

### D3 — Planner integration

`RealModelPlanner` remains the **single** model-backed `Planner`
implementation. Integration decisions:

1. **Interface (unchanged):** `plan(goal_description, task_id, registry,
   context) -> list[PlanStep]` and `required_capabilities(goal_description)`.
   The engine does not know whether the planner is model-backed — this keeps
   orchestration, scheduler, locks, recovery and CLI untouched.
2. **Capability requirements gate (unchanged):** `required_capabilities`
   stays the **deterministic heuristic** (`planner_requirements`). The model
   is never asked to declare its own requirements — a model cannot whitelist
   itself. The gate therefore stays fail-closed and provider-independent.
3. **Live-registry validation (unchanged):** every model plan passes
   `PlanValidator` against the **live** registry before becoming `PlanStep`s;
   hallucinated capabilities/actions/params/resources are rejected with typed
   `PlanCapabilityValidationError`.
4. **Plan authority (unchanged):** validated steps flow through the existing
   `_plan` path — immutable `goal_plans` version claimed first (ADR-050),
   exact-plan task claim (ADR-051), checkpoint on publication, revision/CAS
   semantics, terminal-row immutability, superseded-plan fences
   (ADR-048/049/053), plan-version-fenced completion/failure (ADR-054/055/056).
   A model plan is a plan like any other from this point on.
5. **Replay without the model (unchanged, now explicit):** model-produced
   plans persist as immutable `plan_summary`; the stored-plan fast path
   (`_plan_for_goal`) reconstructs steps **without re-querying the model**
   after restart or resume. A reconstructed task that diverges from the
   stored plan fails closed (`_task_matches_latest_plan`).
6. **Source marking (additive):** `plan.produced` and `plan.versioned` detail
   gain `"source": "model" | "deterministic" | "stored"`; the hardcoded
   `planning.memory.influence` field `"deterministic": True` gains a matching
   `"source"` key (the legacy boolean stays for compatibility). This makes
   plan provenance observable without any schema change.
7. **Model plans require exactly the validation above** — the schema parse,
   the registry validation, the status normalization, and the authoritative
   publication. No additional per-plan gate is introduced.

### D4 — Reflection / learning

`ModelReflector` stays behind the `Reflector` seam. Decisions:

1. **Input (unchanged):** the structured, privacy-safe episode summary
   (`_build_prompt`): goal[:300], outcome, plan capability/action/status
   list, failure errors[:200], authorization denials, recovery, tags.
   **The model never receives** raw prompts, raw responses, file contents,
   capability outputs, credentials, or secrets.
2. **Output (unchanged):** a strictly validated `Reflection`
   (`reflection_schema` v1.0): only informational fields; forbidden
   authority fields rejected top-level and nested; confidence/importance
   bounded; all strings bounded.
3. **Effect on future plans (unchanged):** informational only. Reflections
   enter the retrieval pipeline (`MemoryRetriever` → `PlanningContext` →
   `MemoryGuidance` → `apply_guidance_to_steps`), which is advisory and
   non-mutating; authorization, approval, locks and recovery never consult
   reflections. Poisoning tests already prove this invariant and remain.
4. **Persistence (unchanged):** the `reflections` table via
   `record_reflection` — the durable one-reflection-per-episode claim
   (ADR-013) applies to model reflections identically.
5. **Failure (specified):** reflection is best-effort by design. On any model
   reflection failure (provider, malformed, forbidden fields) the engine
   **immediately falls back to `DeterministicReflector`** (already
   implemented) and emits `reflection.validation.failed` with
   `fallback: "deterministic"`. **No retries** in the default configuration:
   reflection never blocks the task loop, and retrying a non-critical
   learning step adds latency without authority value. `ARION_LLM_REFLECTION`
   can disable the model reflector entirely (always deterministic).
6. **Observability (unchanged + additive):** `reflection.requested`,
   `reflection.validation.passed/failed` exist; `reflection.created` detail
   gains `"source": "model" | "deterministic"`.

### D5 — Fallback semantics (critical)

Fallback is **explicit, bounded, and audited** — never implicit. The decision
procedure lives inside `RealModelPlanner` (planner-level composition, so the
engine stays planner-agnostic):

```
plan(goal) →
  attempt model plan_structured
    ├─ success: PlanSchema → validate → return steps            [model path]
    ├─ malformed/schema/validation failure (incl. refusal —
    │     an empty `steps` array is a PlanSchemaValidationError):
    │     semantic retry (reprompt, max_retries, same goal+catalog)
    │       ├─ success → return steps
    │       └─ exhausted → fallback or durable failure
    ├─ transient provider failure (network/timeout/5xx/429):
    │     transport retries (D1) → exhausted →
    │       fallback or durable failure
    ├─ auth/config failure (401/403/4xx, unknown provider):
    │     NO retry (misconfiguration is deterministic) →
    │       fallback or durable failure
    └─ model refusal ("cannot propose" — e.g. an empty steps array,
          which `PlanSchema.from_dict` rejects as `PlanSchemaValidationError`):
          handled by the schema-failure row above
```

| Condition | Retry? | Fallback? (fallback_enabled=True) | Durable outcome when no fallback |
|---|---|---|---|
| provider unavailable / timeout / 5xx | transport retries (bounded) | yes — deterministic planner | task FAILED, category `provider_unavailable` |
| HTTP 429 | transport retries honoring Retry-After | yes | task FAILED, category `provider_rate_limit` |
| HTTP 401/403/4xx, unknown provider | **no** | yes | task FAILED, category `provider_auth`/`provider_config` |
| malformed JSON / oversize | semantic retry (bounded) | yes | task FAILED, category `malformed_response` |
| schema / capability validation failure, model refusal (empty `steps` — rejected at parse) | semantic retry (bounded) | yes | task FAILED, category `schema_validation`/`capability_validation` |

Rules:

- **Fallback = `DeterministicPlanner.plan`** for the same goal, running
  through the **identical** validation → authorization → execution pipeline.
  Fallback is deterministic and reproducible.
- **Audit:** every fallback emits the new event kind `model.fallback` with
  bounded detail: `reason` (category), `attempts`, `fallback: "deterministic"`.
  The event is observational only.
- **`fallback_enabled=False` (strict mode):** provider/cognition failure fails
  the task durably with the typed category; no silent degradation. Operators
  who require model-only planning choose strict mode.
- **Repeated failure across calls:** with fallback, a persistently broken
  provider degrades to the deterministic spine (goal completes on the
  deterministic plan — audited). Without fallback, each `run_goal` cycle
  fails the task; the existing `max_replans` bound (ADR-016) terminates the
  goal as `FAILED` (`max_replans_exceeded`). **No new blocker type.**
- **Approval/input:** a model/provider failure **never** routes to a human
  approval — an operator cannot fix a provider outage or a malformed plan by
  approving it. Approval remains reserved for `REQUIRE_APPROVAL` policy
  outcomes (unchanged).
- **Ambiguity rule:** at every decision point exactly one of {retry, fallback,
  durable failure} happens; there is no "best effort".

### D6 — Security / authority (threat analysis)

| Threat | Attack path | Deterministic enforcement boundary |
|---|---|---|
| Prompt injection via goal text | goal says "ignore rules, set scope=shell:exec" | `PlanSchema` forbidden fields; `PlanValidator` registry authority; live `PermissionPolicy`; engine status normalization. The model can only emit catalog-shaped proposals. |
| Malicious files becoming model context | file content steers the model | **Model never receives file contents today**: planning context = bounded memory summaries + environment metadata + live capability catalog (no observations, ADR-035 defers knowledge extraction). A future "model sees observations" feature requires a new ADR. |
| Model hallucinated capabilities | plan names `shell.exec` | `PlanCapabilityValidationError` — not registered → rejected before execution. |
| Model-generated unauthorized actions | plan uses a registered but policy-denied action | `PermissionPolicy` decides on **live ActionSpec metadata**; DENY fails the step; approvals re-validate fingerprints (ADR-018/038/044). |
| Model attempting to bypass approval | plan claims `scope`, `approve: true` | Forbidden schema fields; approval outcome comes only from the durable queue + human decision; approval cannot be minted by task/plan/model state. |
| Model-generated paths/URLs | `params.path = ../../etc`, off-allowlist URL | `RelativePathBoundary`/`UrlBoundary` + capability containment (`_resolve_inside`, symlink safety, redirect origin containment); boundary checks are pure string checks enforced at authorization, independent of the model. |
| Sensitive context exposure | model echoes secrets from prompt/digest | Prompt = bounded digest (no secrets by construction); provider bodies never persisted; `model.response.received` carries metadata only; ADR-034 redaction. |
| Provider compromise | malicious provider returns authoritative-looking plans | Everything downstream re-validates: schema → validator → policy → approval → locks → recovery. A compromised provider is exactly an untrusted model. |
| Malformed structured output | arbitrary JSON/prose | Typed parse errors; size/depth caps; nothing executes. |

**Reaffirmed invariant:** a model cannot authorize itself, cannot change
`resource_kind`, cannot create capabilities, cannot set actor/scope/risk/
side-effects, cannot approve or deny, cannot own a lock/lease, cannot clear
recovery, cannot mint execution state, and cannot persist anything beyond the
validated `PlanStep`/`Reflection` objects and bounded metadata.

### D7 — Determinism

- **Deterministic execution:** everything downstream of `PlanStep` production
  (scheduler, locks, execution, verification, checkpoint, recovery, approval,
  memory recording) is unchanged and fully deterministic.
- **Non-deterministic cognition:** the model call itself may differ across
  runs. Arion's guarantees when cognition is probabilistic:
  1. **Model responses are never persisted** — only bounded metadata events
     (`model.response.received`, `model.retry`, `model.fallback`,
     `plan.validation.*`).
  2. **Plans are replayable without re-querying the model** — immutable
     `plan_summary` + stored-plan fast path; a resumed/restarted goal
     reconstructs the exact plan version.
  3. **Fallback is deterministic** and reproducible (D5).
  4. **Provider metadata is recorded** (provider, model, latency, tokens,
     outcome, error category) for every interaction.
  5. **Divergence fails closed** — a task that does not reproduce its stored
     plan version is never executed.
  6. **Tests exercise model behavior deterministically** via fake providers
     and fake transports (no network, no credentials, ADR-008).

### D8 — Configuration

- `ModelProviderConfig` + `load_model_config()` (env) + `build_router(config)`
  (factory) as in D1. `bootstrap.build_engine` gains optional
  `model_config: ModelProviderConfig | None = None` and (for symmetry)
  `reflector` passthrough; when `model_config` is `None`, `build_engine`
  reads the environment, and if no provider is configured the result is
  **byte-for-byte today's deterministic engine**.
- **Enable/disable:** provider unset/`none` → model path disabled
  (deterministic). `ARION_LLM_REFLECTION=0` → deterministic reflector only.
  `ARION_LLM_FALLBACK=0` → strict mode (D5).
- **Secrets:** `ARION_LLM_API_KEY` is read from the environment only; it is
  never written to the DB, events, checkpoints, memories, cognitive records,
  or JSONL; `ModelProviderConfig.__repr__` redacts it; provider errors never
  carry bodies (ADR-034). Tests assert absence of the key in all persisted
  surfaces.
- **Limits:** timeout, retries, backoff, response-size and plan-size caps are
  configurable with the proposed defaults (D1/D2). The planning-context
  budget (`ContextBudget`: 5 episodes / 3 reflections / 4,000 chars) remains
  the context mechanism — no RAG, no vector DB.

### D9 — Testing strategy (design; executed with the milestones)

1. **Fake-provider tests:** `FakeModelRouter` (exists) + fake HTTP transport
   (exists) drive success/malformed/adversarial plan cases.
2. **Provider failure tests:** fake transports returning timeouts, 5xx, 429
   (with/without `Retry-After`), 401/403, garbage — assert typed error
   categories, retry counts, backoff delays (injectable clock).
3. **Size-limit tests:** oversize response, >100 steps, >32 params, deep JSON,
   long strings — rejected before execution.
4. **Fallback tests:** every row of the D5 table — provider down → fallback
   plan runs to completion; strict mode → durable typed failure; empty steps
   → fallback; repeated failure → `max_replans_exceeded`; `model.fallback`
   event asserted.
5. **Authorization-boundary tests:** model plan with spoofed scope/approve/
   actor → rejected; out-of-boundary resource → DENY; approval still required
   for `REQUIRE_APPROVAL` steps; fingerprint changes still invalidate
   approvals.
6. **Prompt-injection/adversarial tests:** hostile goal text and hostile
   memory summaries cannot make the model path grant, escalate, or bypass
   anything; the existing poisoning/authority suites stay green.
7. **"A model cannot authorize itself":** adversarial test driving a
   compromised fake provider that emits `scope=…`, `approve=true`,
   `actor=…`, `permissions=[…]`, forged statuses, unregistered capabilities —
   assert the task fails before any capability executes and no authority
   state changes.
8. **End-to-end model → plan → authorization → execution tests:** goal →
   fake model → valid plan → policy → (approval) → lock → execute → verify →
   completion; and the live smoke test extended into a scripted harness.
9. **Persistence/recovery tests:** model plan persisted as immutable version;
   restart resumes from the stored plan **without re-querying the model**
   (fake router asserts zero calls); divergence fails closed.
10. **Deterministic replay tests:** stored plan reconstruction yields
    identical steps; `planning.memory.influence`/`plan.versioned` carry the
    correct `source`.

### D10 — Backward compatibility

- **Default behavior unchanged:** no provider configured ⇒ the exact current
  deterministic engine (same defaults, same tests pass untouched).
- **Opt-in:** configuring a provider (env or constructor) enables the model
  path; nothing existing changes.
- **No provider configured:** deterministic planner/router/reflector — current
  behavior.
- **Migration:** none — no DDL, no table, no column, no `PlanSchema` version
  change, no checkpoint format change. Additive surfaces only: new event
  kinds registered in `EVENT_KINDS`, new typed error class, new detail keys,
  new config module.
- **Existing goals/plans/history:** model-produced and deterministic plans are
  indistinguishable in storage (same `goal_plans`/`tasks`/`checkpoints`
  shape); old rows remain readable; replays are source-agnostic.

---

## Trust-boundary diagram

```
                    ┌──────────────────────────────────────────────┐
                    │                 INTERFACES                    │
                    │   CLI (text, actor)  ·  (voice/vision/API:    │
                    │                       future, out of scope)   │
                    └───────────────┬──────────────────────────────┘
                                    │ trusted: goal text + actor chain
                    ┌───────────────▼──────────────────────────────┐
                    │             ORCHESTRATION (engine)            │
                    │  owns the loop: state machine, scheduler,     │
                    │  locks, approvals, recovery, verification,    │
                    │  checkpoints, memory lifecycle                 │
                    │  AUTHORITY: policy · approval · locks ·       │
                    │  recovery · plan lineage (unchanged)           │
                    └───────┬──────────────────────────┬────────────┘
                            │ Planner.plan             │ Reflector.reflect
              ┌─────────────▼──────────────┐   ┌────────▼─────────────┐
              │      INTELLIGENCE          │   │   INTELLIGENCE       │
              │  RealModelPlanner          │   │   ModelReflector     │
              │   │ fallback:              │   │   │ strict schema    │
              │   │ DeterministicPlanner   │   │   ▼                  │
              │   ▼                        │   │ deterministic fallback
              │  PlanSchema v1.0 (strict)  │   └─────────┬────────────┘
              │   │ PlanValidator (live    │             │ informational
              │   │ registry)              │             ▼
              │   ▼                        │     memory (episodes,
              │  PlanStep[] (normalized)   │     reflections, beliefs)
              └─────────────┬──────────────┘
                            │ UNTRUSTED from here down: the model can
                            │ only have produced validated PlanSteps
              ┌─────────────▼─────────────────────────────────────────┐
              │   CAPABILITIES  (live ActionSpec, sandboxed exec)      │
              └─────────────┬─────────────────────────────────────────┘
                            │
              ┌─────────────▼─────────────────────────────────────────┐
              │   STATE  (SQLite: goals, tasks, plans, checkpoints,   │
              │            audit, approvals, locks, scheduler,        │
              │            memories, beliefs)                         │
              └───────────────────────────────────────────────────────┘

  MODEL BOUNDARY: the only non-deterministic edge. Everything the model
  emits crosses (a) schema parse, (b) registry validation, (c) status
  normalization, (d) live authorization, before it can touch the world.
  Authorization ≠ coordination ≠ memory — unchanged.
```

## Fallback state machine

```
                    ┌─────────────┐
                    │   plan()    │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐   valid          ┌──────────────┐
                    │ model call  ├─────────────────►│ return steps │
                    └──────┬──────┘                  └──────────────┘
              error / empty│
                    ┌──────▼──────┐  attempts < max  ┌──────────────┐
                    │ retry?      ├─────────────────►│ (back to top)│
                    └──────┬──────┘                  └──────────────┘
                           │ exhausted
                    ┌──────▼──────┐  fallback_enabled
                    │ fallback?   ├──────────────────► DeterministicPlanner
                    └──────┬──────┘                    (same pipeline; audit)
                           │ no
                    ┌──────▼──────┐
                    │ durable     │  task FAILED (typed category)
                    │ failure     │  → goal stays ACTIVE → max_replans bound
                    └─────────────┘
```

---

## Architectural invariants (implementation must not violate)

1. **The model may propose; Arion decides.** No model output grants, denies,
   approves, cancels, escalates, or bypasses any authority decision.
2. **Authorization ≠ coordination ≠ memory** — unchanged; model output and
   model-derived reflections remain informational.
3. **The engine owns the loop.** The model is a callable component behind the
   `Planner` / `ModelRouter` / `Reflector` protocols; orchestration never
   imports a provider.
4. **Every plan crosses the strict schema + live-registry validation** before
   becoming `PlanStep`s; every step is re-authorized against live `ActionSpec`
   metadata before execution.
5. **Execution state cannot come from the model** — forged statuses are
   normalized to `PENDING`/`SKIPPED` (ADR-025 Phase H).
6. **Immutable plan versions; replay never re-queries the model** — stored
   plans reconstruct deterministically; divergence fails closed.
7. **Fail closed:** no provider → deterministic; unknown provider → typed
   error; malformed/oversized output → typed error; nothing partial executes.
8. **Secrets never enter logs, events, checkpoints, memories, or cognitive
   records** — env-only credentials, redacted repr, ADR-034 error policy.
9. **Fallback is explicit and audited** (`model.fallback`), never implicit;
   every decision point is exactly {retry, fallback, durable failure}.
10. **Downstream determinism preserved:** only the cognition step is
    non-deterministic; its inputs (bounded digest + live catalog) and outputs
    (validated plans/reflections + metadata) are bounded and replay-safe.
11. **No schema/DQL change** — additive surfaces only (event kinds, typed
    errors, detail keys, config module).
12. **The capability catalog is the live registry summary** — no hardcoded
    tool list, no unregistered capability can be proposed into execution.

## Rejected alternatives

1. **Free-form model output + natural-language parsing.** Rejected: ADR-011
   already chose a strict structured contract; NL parsing is ambiguous,
   non-verifiable, and lets the model smuggle authority-shaped text.
2. **Model-in-the-loop authority** (model fills scope/risk/resource). Rejected:
   violates the governing invariant; scope must come from live `ActionSpec`.
3. **Fallback inside the engine.** Rejected: the engine stays
   planner-protocol-only; composing retry+fallback in `RealModelPlanner`
   keeps orchestration agnostic and the deterministic path untouched.
4. **Async runtime / threads for provider calls.** Rejected: synchronous
   stdlib calls match ADR-002; the scheduler already bounds concurrency.
5. **Persisting model responses for audit/replay.** Rejected: privacy
   (ADR-011/034); immutable plan summaries already give replay.
6. **Vector DB / RAG for planning context.** Rejected (deferred): the bounded
   deterministic `PlanningContext` digest is the context mechanism for this
   phase.
7. **Multi-provider routing (per-goal model selection).** Rejected for this
   phase: one configured provider; the `ModelRouter` protocol and
   `PROVIDER_REGISTRY` leave the door open.
8. **Durable circuit breaker / provider-down goal blocker.** Rejected for this
   phase: the fallback + `max_replans` bound already produce concrete
   outcomes; a durable breaker needs its own ADR (see unresolved).
9. **Config file (TOML/YAML).** Rejected for this phase: env + constructor
   injection match the zero-config codebase; a file-based config is a future
   option.

## Implementation plan (independently verifiable milestones)

Each milestone leaves the suite green and the baseline behaviors unchanged;
none touches the authority model or schema.

- **M1 — Provider configuration surface.** `ModelProviderConfig`,
  `load_model_config()`, `build_router()`, `PROVIDER_REGISTRY`,
  `ProviderRateLimitError`, transport retries with backoff, `model.retry`
  event.
  *Test strategy:* fake-transport tests for timeout/5xx/429(+Retry-After)/
  401/403/garbage; retry counts and backoff with injectable clock; unknown
  provider fails closed; no-provider ⇒ `build_router` returns `None`;
  credential-redaction repr test.
  *Gate:* new tests green; full suite green; deterministic default byte-identical.
- **M2 — Structured-output hardening.** Size/depth caps enforced at parse
  (constants + config), response byte cap.
  *Test strategy:* oversize/over-depth/over-count/over-length rejection;
  existing valid-plan tests unchanged.
  *Gate:* new tests green; full suite green.
- **M3 — Fallback semantics.** `RealModelPlanner` retry + fallback composition;
  `model.fallback` event; `source` markers on plan events; strict mode.
  *Test strategy:* D5 table-driven tests; adversarial "compromised provider
  cannot authorize itself"; deterministic replay tests (stored plan → no
  re-query); `max_replans_exceeded` path; source markers asserted.
  *Gate:* new tests green; full suite green; demos green.
- **M4 — Reflection wiring (implemented).** `reflector` passthrough in
  `build_engine`; explicit `reflector=` wins; ModelReflector selected only
  when a provider is configured AND `reflection_enabled` (consumes
  `ARION_LLM_REFLECTION` via `load_model_config`); deterministic reflector
  otherwise (memory on) / None (memory off); additive `last_source`
  provenance seam ("model" | "deterministic"); `reflection.created` carries
  the additive `"source"` marker; engine-created deterministic fallback is
  always marked "deterministic". Model reflection: exactly one provider
  call, no retries, immediate deterministic fallback on any failure.
  *Test strategy:* model reflection success; malformed/forbidden → immediate
  deterministic fallback; `ARION_LLM_REFLECTION=0`; engine offline with no
  provider.
  *Gate:* new tests green; full suite green.
- **M5 — Opt-in runtime + live harness.** `bootstrap`/CLI env-driven wiring;
  replace the single smoke test with a scripted live-provider harness
  (offline-skippable scenarios: happy path, malformed, provider down,
  fallback, strict mode); end-to-end model → plan → authorization →
  approval → lock → execute → verify → memory.
  *Test strategy:* harness scenarios; adversarial no-self-authorization;
  persistence/recovery of model plans; coverage gate ≥ 87% (baseline);
  full suite + all demos green.
  *Gate:* M5 acceptance criteria met; report to operator.

## Unresolved questions

1. **Durable circuit breaker:** should repeated provider failures durably
   mark a goal BLOCKED (`provider_unavailable` blocker) instead of relying on
   `max_replans`? Design choice for this phase: no (existing bound suffices);
   a breaker would be a separate ADR.
2. **Per-goal model selection / multi-provider routing:** deferred; the
   protocol supports it but no configuration surface is designed for it yet.
3. **Exact defaults — settled by M1/M2 implementation** (documented here as
   the ADR addendum): timeout=60 s, max_retries=2, backoff base=1.0 /
   max=8.0 (M1); MAX_MODEL_RESPONSE_BYTES=262_144, MAX_JSON_DEPTH=10,
   MAX_PLAN_STEPS=100, MAX_PARAMS_PER_STEP=32, MAX_STEP_STRING=2000 (M2).
   All remain configurable; no evidence surfaced during M1/M2 to adjust them.
4. **CLI flags vs env-only:** env-only chosen; a `--llm` flag family may be
   added if operators need per-invocation overrides.
5. **Catalog size budget:** `capabilities_summary()` grows with the catalog;
   a bounded `max_catalog_chars` truncation in `RealModelPlanner` remains
   open (not part of M2 — M2 bounded model OUTPUT, not planner INPUT;
   reconsider with M3 fallback composition).
6. **`model.response.received` correlation IDs** (request_id/trace):
   provider-dependent; defer until a distributed deployment needs them.

## Related

ADR-001 (layers), ADR-005 (ModelRouter), ADR-008 (deterministic testing),
ADR-011 (structured intelligence boundary), ADR-012 (memory), ADR-013
(learning), ADR-016 (goals/replan), ADR-018 (planner contract),
ADR-025/026/027/028/029/030/031 (scheduler), ADR-032 (lifecycle),
ADR-033 (event contract), ADR-034 (error boundary), ADR-035 (observation
retention), ADR-037 (resource presentation), ADR-040/041 (task fencing /
crash consistency), ADR-048…056 (plan authority fences).
