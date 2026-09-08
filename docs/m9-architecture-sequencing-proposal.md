# M9 Architecture & Sequencing Proposal

**Status:** Read-only audit — no source/test/dependency modifications.  
**Scope:** M9-A (model-backed planner integration) as primary; M9-B (capability vocabulary) mapped but deferred.  
**Sequence:** M8 → docs close-out (committed `705f856`) → M9-A → M9-B.  
**Baseline:** `64a7332` (full suite: 1955 collected, 1953 passed, 2 skipped, 0 failed, 0 errors; exit 0).

---

## 1. Current model-path architecture (traced from source, not assumed)

### Components present and connected

| Component | File | Role in path | Connection proof |
|---|---|---|---|
| `Planner` (protocol) | `intelligence/planner.py` | Interface contract; `plan()` returns `list[PlanStep]` | Used by `engine.py` (`self.planner: Planner`) |
| `DeterministicPlanner` | `intelligence/planner.py` | Default implementation; no model | Constructor takes optional `router`; used when `router=None` or deterministic selected |
| `RealModelPlanner` | `intelligence/model_planner.py` | Structured model planning with M3 fallback + deterministic fallback | Uses `router.plan_structured()`; validates via `PlanValidator`; emits audit events |
| `ModelRouter` (protocol) | `intelligence/router.py` | Provider-neutral interface; `plan_structured()` returns `PlanSchema` | Implemented by `DeterministicRouter` (offline) and `OpenAICompatModelRouter` (provider) |
| `OpenAICompatModelRouter` | `intelligence/providers/openai_compat.py` | Real provider adapter; `urllib` transport; JSON structured output; bounded retry/bounds | Registered in `PROVIDER_REGISTRY` (`"openai-compatible"`); `build_router()` constructs it from `ModelProviderConfig` |
| `PlanSchema` / `PlanValidator` | `intelligence/plan_schema.py`, `plan_validator.py` | Schema definition + live registry validation | `RealModelPlanner.plan()` calls both in sequence |
| `PlanTransform` / guidance | `memory/guidance.py` (imported in model_planner) | Memory-driven step transformation AFTER validation | Applied after break from validation loop; informational only |
| `ModelReflector` | `memory/model_reflector.py` | Structured reflection via router + strict schema validation | Behind `Reflector` seam; deterministic default; validated before storage |
| `ModelProviderConfig` / `load_model_config` | `intelligence/config.py` | Env-driven opt-in (`ARION_LLM_*`); output bounds (`max_response_bytes`, etc.) | `enabled=False` when provider unset/empty/`"none"`; credentials never persisted/logged |
| `build_router()` | `intelligence/providers/__init__.py` | Factory: mapping config → adapter or `None` | Returns `None` for disabled configs; raises `ProviderConfigurationError` for unknown providers |

### Actual runtime flow (when model IS configured)

```
Engine.goal_cycle() / run_task()
  → planner.plan(goal_description, task_id, registry, context)  [RealModelPlanner if wired]
    → router.plan_structured(goal, catalog, router_context)
      → (network to /v1/chat/completions → JSON response)
    → PlanSchema (parsed + bounded)
    → PlanValidator.validate(schema)  [live registry-aware]
    → (retry loop for malformed/schema/capability errors; bounded by semantic_max_retries)
    → (fallback to DeterministicPlanner if budget exhausted and fallback_enabled)
  → steps = list[PlanStep]  [source = "model" or "deterministic"]
  → engine validates step verification (authorization-independent normalization)
  → authorization: policy.decide(request)  [scope from ActionSpec, not plan]
  → execution / observe / verify / checkpoint / complete
```

### Where deterministic takes over today (confirmed by code, not assumed)

- **Default:** engine initialized with `DeterministicPlanner` + `DeterministicRouter` (`router = None` or deterministic router passed). No provider configured → `build_router()` returns `None`; model path never invoked.
- **Explicit opt-in required:** `ARION_LLM_PROVIDER` must be non-empty/non-`none`; `ARION_LLM_BASE_URL` and `ARION_LLM_API_KEY` must be set for a real endpoint.
- **Fallback:** `RealModelPlanner` only activates when explicitly passed as `planner` parameter to engine; `fallback_enabled=True` (default) means any typed failure after retries falls back to deterministic — same pipeline, different source marker.
- **Reflection:** `reflector` parameter defaults to deterministic unless `ARION_LLM_REFLECTION` enabled and model router configured.
- **Config surface:** M1 implemented; M2 output bounds implemented; M3 runtime wiring, reflection wiring, opt-in engine plumbing are **NOT** complete (per `config.py` comments: "runtime composition ... is NOT part of M1/M2 and is owned by later milestones").

---

## 2. M9-A readiness assessment (14 precise questions answered from source)

### Q1. What happens today when a real model is configured?

If `ARION_LLM_PROVIDER=openai-compatible`, `ARION_LLM_MODEL` set, `ARION_LLM_BASE_URL`/`API_KEY` present, and `build_router()` is called with a valid config, `OpenAICompatModelRouter` is constructed. When `RealModelPlanner` is wired into the engine (`planner=` argument), `plan()` calls `router.plan_structured()`, which sends `json_object` structured request to the endpoint. The adapter enforces `MAX_RESPONSE_BYTES` / `MAX_JSON_DEPTH` on the envelope, parses JSON, validates against `PlanSchema`, and returns `PlanSchema` or raises typed `PlanValidationError` / `ModelPlanError`. If `RealModelPlanner.fallback_enabled=True` (default) and failure category is in `_FALLBACK_CATEGORIES`, it calls `_fallback_to_deterministic()` — same `PlanStep` pipeline, `last_source="deterministic"`.

**Confirmed by:** `model_planner.py`, `router.py`, `openai_compat.py`, `config.py`, `engine.__init__`.

### Q2. Is there a genuinely end-to-end executable path, or only isolated components/seams?

**Isolated components with a working seam, not a fully wired end-to-end production path.**

- **Seams fully implemented:** `ModelRouter` protocol, `PlanSchema`/`PlanValidator`, `RealModelPlanner` pipeline, `OpenAICompatModelRouter` adapter with bounded retry/bounds, `build_router()` factory, config surface, reflection validation (`ModelReflector` + `reflection_schema`).
- **Not wired by default:** engine defaults to `DeterministicPlanner`; CLI (`cli.py`) has no enforced opt-in; `memory/` reflection is optional; `model_fallback`/`reflection_wiring` are not integrated into the standard goal-cycle unless explicitly configured.
- **Smoke test only:** `tests/smoke/test_live_provider.py` (5 cases) requires `ARION_LLM_BASE_URL` or `API_KEY`; not run by default (`pytest -m smoke` skipped). No full end-to-end test proves model→plan→authorize→execute→verify with a live provider.
- **No production deployment path:** no `docker-compose` / Kubernetes / systemd configuration for the provider; no secret-management integration beyond env-only (good) but no documented operator procedure.

**Verdict:** The components form a defensible seam, but M9-A requires explicit wiring + validation + security hardening before it is a production path.

### Q3. Where does deterministic behavior currently take over?

1. `build_router()` returns `None` when `provider` unset/empty/`"none"` → deterministic router.
2. `RealModelPlanner` falls back to `DeterministicPlanner` on any typed failure when `fallback_enabled=True`.
3. `ModelReflector` falls back to `DeterministicReflector` if model reflection fails (validated after generation; never stored if invalid).
4. Default `engine.__init__` uses `DeterministicPlanner` unless caller explicitly passes `RealModelPlanner`.
5. CLI runs deterministic unless env configured; no automatic promotion.

**Confirmed by:** `config.py`, `providders/__init__.py`, `model_planner.py`, `memory/model_reflector.py`, `engine.py`.

### Q4. What inputs does the model actually receive?

From `model_planner.py` (`plan()` method):
- `goal_description`: raw goal text (truncated to 200 chars in `planning.requested` audit only; full passed to router).
- `catalog`: `registry.capabilities_summary()` — list of dicts describing registered capabilities/actions/scopes/risks.
- `router_context`: `{"task_id": task_id}` + optional `context.digest()` (information-only memory digest, bounded, never authoritative).
- No actor identity, no authorization context, no resource boundary rules, no approval queue — all excluded by design.

**Confirmed by:** `model_planner.py` lines 135–150.

### Q5. What context/memory does it receive?

- `PlanningContext.digest()` (from `memory/`) if available and has `.digest()` method; exception swallowed silently (`except Exception: pass`).
- `context.guidance` (post-validation only): `apply_guidance_to_steps()` transforms already-validated steps; informational, non-mutating of authority; uses `registry_resource_param` and `action_meta` resolvers from registry.
- Memory is never used for authorization, never included in policy decision, never persisted in audit events beyond bounded metadata.

**Confirmed by:** `model_planner.py` (router_context construction + guidance application after break); `docs/architecture.md` memory layer description.

### Q6. What output schema does it produce?

`PlanSchema` (`intelligence/plan_schema.py`): structured JSON with `version`, `goal`, `steps` array (each with `index`, `intent`, `capability`, `action`, `scope`, `params`, `verify` policy, etc.). Enforced bounds at parse time: `MAX_PLAN_STEPS`, `MAX_STEP_STRING`, `MAX_PARAMS_PER_STEP`, `MAX_MODEL_RESPONSE_BYTES`, `MAX_JSON_DEPTH`. Nothing free-form passes through.

**Confirmed by:** `plan_schema.py` constants + `PlanValidator` usage.

### Q7. How is untrusted model output validated?

Three-stage validation, all after generation, none during:
1. **Envelope bounds:** `MAX_RESPONSE_BYTES`, `MAX_JSON_DEPTH` enforced by adapter before parsing (`openai_compat.py`).
2. **Schema parse + cap:** `PlanSchema` parse validates structure; `PlanValidator.validate()` checks against live `CapabilityRegistry` (`action_spec()`, `required_scope`, `param_schema`), resolves resources via `resolve_resources`, applies topologically ordered steps, normalizes verification policies.
3. **Authorization (separate layer):** even a structurally valid plan is decided by `PermissionPolicy.decide()` using `ActionSpec` metadata — not the model's claimed scope.

Failures emit typed `PlanningError` with categories (`malformed_response`, `schema_validation`, `capability_validation`, etc.). Retries bounded by `semantic_max_retries` (default 2) only for the three semantic categories; transport errors (`provider_unavailable`, etc.) never semantically retried (M1 adapter owns transport retry).

**Confirmed by:** `model_planner.py`, `plan_validator.py`, `authz.py`, `openai_compat.py`, `errors.py`.

### Q8. Can model output cause unauthorized capability/resource access?

**No — if the architecture is respected:**
- Model never touches `Policy` / `ResourcePolicy`; it only proposes steps.
- `PlanValidator` checks `capability`/`action` exist in live registry; unknown names fail.
- `scope` in plan is advisory; engine uses `spec.required_scope` from registry (`authz.py` `_build_authz_request`).
- `resource` boundaries (ADR-061 / M8 C4) apply to every declared resource in `ActionSpec`; model cannot declare a resource not in `ActionSpec.resources` because validation checks against registry.
- **Subtle risk:** if `PlanValidator` has a bug, or if `CapabilityRegistry` is manipulated (not through model but through another pathway), a malicious plan could reference a valid capability with parameters that pass schema but exceed intended boundary — but authorization checks params against configured `ResourcePolicy` boundary per kind. The model cannot change the boundary.
- **Actual risk is lower-layer:** if adapter is compromised (prompt injection via `goal_description`), it could cause the model to produce a structurally valid but malicious plan; validation catches structural issues, but an adversary could craft a goal that legitimately maps to a dangerous capability (e.g., `filesystem.write` with `dest=/etc/passwd`) — but authorization denies it if boundary configured. The security control is the authorization layer, not the model.

**Verified by:** `engine.py` authorization block (around line 4345+); `authz.py` `_resource_allowed`; `plan_validator.py`; `registry.py`.

### Q9. What happens on malformed / adversarial / ambiguous / hallucinated plans?

- **Malformed (bad JSON / wrong schema):** adapter rejects at envelope/parse; `MalformedProviderResponseError`; if after retries, `PlanValidationError` with category; fallback or durable failure.
- **Schema valid but unknown capability/action:** `PlanValidator` raises `PlanValidationError` (`capability_validation`); retry only if category matches; else fail/fallback.
- **Ambiguous resource declaration:** `PlanValidator` checks via `resolve_resources`; unresolved roles cause `PlanValidationError`; boundary check (C4) applies after authorization.
- **Hallucinated params / wrong scope claim:** validation checks against `param_schema`; authorization uses `spec.required_scope`. Plan's `scope` field is recorded but not authoritative.
- **Adversarial prompt injection in goal:** adapter passes `goal_description` verbatim; model produces structurally valid plan for malicious goal; authorization still applies. No sandbox escape for model (no shell, subprocess, dynamic execution in core). The adversarial impact is bounded to requesting actions the user could have requested directly.
- **Unexpected programming errors:** wrapped and raised durably (`except Exception` in `RealModelPlanner.plan()`); never retried or fallen back; prevents silent degradation.

**Confirmed by:** `model_planner.py` exception handling blocks; `plan_validator.py`; `errors.py`.

### Q10. What does fallback do, and can it silently change semantics?

`_fallback_to_deterministic()` produces a `list[PlanStep]` from `DeterministicPlanner`. Key properties:
- Same pipeline after return: same authentication (`_normalize_step_verification`, `policy.decide`, execution).
- Same `PlanStep` schema; different content (deterministic decomposition vs model proposal).
- `last_source` = `"deterministic"`; `plan.produced` / `plan.versioned` read this for audit.
- No change to authorization, resource boundaries, verification, execution policy.
- **Not silent:** `model.fallback` audit event emitted with `category`, `attempts`, `fallback:"deterministic"`. Source marker explicit.
- **Semantic change:** yes, the plan's steps differ (deterministic may decompose differently), but the engine treats both identically after planning. There is no second execution path.

**Confirmed by:** `model_planner.py`; `planner.py` (DeterministicPlanner); `engine.py` (plan versioning/audit).

### Q11. How are model failures represented in orchestration state?

- `plan.validation.failed` event with `success=False`, `detail` summary (category, message bounded, no raw prompts/responses/credentials).
- `model.fallback` event (if fallback triggered) with bounded metadata.
- If strict (`fallback_enabled=False`): durable exception (`PlanValidationError`) propagates; goal/task fails with error message; no silent consumption.
- Step status: if planning fails before step execution, the goal/task is blocked/failed; if planning succeeds but later authorization fails, step gets `StepStatus.FAILED` with `error` from authorization.
- Audit event `plan.produced` / `plan.versioned` includes `last_source`.

**Confirmed by:** `model_planner.py` event emissions; `engine.py` event emission patterns; `state/models.py` (Plan, StepStatus).

### Q12. What observability exists for model decisions?

- `planning.requested` (goal truncated to 200 chars; full goal sent to router but not in audit).
- `plan.validation.passed` (steps count; no content).
- `plan.validation.failed` (summary only — bounded message, category, source; NO prompts, raw responses, credentials, provider payloads per M2/ADR-057).
- `model.retry` (transport retry only — M1 adapter; observable via adapter; planner never emits this for semantic retries).
- `model.fallback` (bounded: reason/category, attempts, `fallback:"deterministic"`; never raw content).
- `model.response.received` (provider/model/latency/token metadata only — adapter).
- Source markers: `last_source` = `"model"` | `"deterministic"`; read by engine for `plan.produced` / `plan.versioned`.
- Reflection: `Reflector` events (if model reflector); validation schema rejects authority-bearing fields.

**Security for observability:** no persistence of raw prompts/responses/credentials in any event; `OpenAICompatModelRouter` explicitly never logs credentials or payloads (`repr`/`str` safe).

**Confirmed by:** `model_planner.py` emit calls; `openai_compat.py` docstring / event patterns; `observability/events.py`.

### Q13. What tests prove the model path today?

- `tests/test_model_planner.py` (12) — planner behavior, fallback logic, retry categories.
- `tests/test_model_fallback.py` (20) — deterministic fallback semantics.
- `tests/test_model_output_limits.py` (40) — envelope/content bounds enforcement.
- `tests/test_model_config.py` (24) — env parsing, enabled/disabled, credential safety.
- `tests/test_model_router.py` (11) — router protocol, deterministic router.
- `tests/test_model_reflection_wiring.py` (17) — reflection seam, validation, deterministic default.
- `tests/test_model_runtime_wiring.py` (16) — engine integration with router/planner.
- `tests/smoke/test_live_provider.py` (5) — requires live endpoint; skipped by default.

**No test proves end-to-end with a real provider at HEAD** — only component and seam tests with mocks/deterministic routers. The smoke tests prove connectivity but are excluded from default runs.

**Confirmed by:** file listings + `pytest --collect-only`.

### Q14. What would be required for a minimal, production-quality M9-A slice?

Based on gaps identified (seams exist, wiring/validation/hardening missing):

1. **Explicit opt-in wiring:** CLI / bootstrap code that reads env and passes `RealModelPlanner` + configured `ModelRouter` to engine (not just default deterministic). Currently requires manual constructor call.
2. **Provider validation:** test `OpenAICompatModelRouter` with a real endpoint (smoke only today); verify `PlanSchema` output matches expectations; confirm error categories map correctly.
3. **Security hardening verification:** confirm authorization layer denies plans that pass validation but propose out-of-boundary resources (test with adversarial goal + valid capability); confirm reflection schema rejects authority-bearing fields; confirm audit events contain no credentials/raw content.
4. **Fallback audit:** verify `model.fallback` events emit; verify source marker changes; verify no silent semantic change in execution pipeline.
5. **Output bound verification:** confirm `max_response_bytes` / `max_json_depth` / `max_plan_steps` / `max_params_per_step` enforce deterministically; confirm no overflow paths.
6. **Memory context bounded:** confirm `context.digest()` is bounded and never authoritative; confirm exceptions swallowed don't suppress authorization.
7. **Reflection production:** confirm `ModelReflector` validates strictly; confirm malformed reflections are never stored; confirm deterministic reflector remains default.
8. **Observability completeness:** verify all 8 event types (`planning.requested` through `model.response.received`, `plan.produced`) are emitted correctly in both success and failure paths; verify no credential leakage.
9. **Documentation / operator procedure:** document `ARION_LLM_*` env surface, failure modes (typed categories), retry budget, fallback behavior, security model for untrusted planner.
10. **Definition of Done (see section 10 below).**

---

## 3. M9-A readiness assessment (summary)

| Dimension | Status | Evidence |
|---|---|---|
| Components / seams | **Present** | All listed above; protocols implemented |
| Default path | **Deterministic** | `build_router()` returns `None`; engine default `DeterministicPlanner` |
| End-to-end live-tested | **Partial** | 5 smoke tests (excluded); 140+ component tests; no full live end-to-end |
| Security boundary (model vs authority) | **Strong** | Model proposes; authorization decides; validation checks registry; reflection schema forbids authority fields; no credential persistence; audit bounded |
| Fallback transparency | **Good** | Explicit event + source marker; no silent path change; same pipeline |
| Output bounds | **Implemented** | Envelope + content caps enforced deterministically |
| Observability | **Partial** | Events defined; need verification all paths emit under live conditions |
| Production wiring | **Missing** | No CLI opt-in, no deployment docs, no secret management beyond env |

**Readiness: seams are defensible; production requires explicit wiring, live-human verification, security audit of adversarial goal scenarios, and operator documentation. Do not treat "components exist" as "ready to deploy."**

---

## 4. Minimal M9-A implementation slice (proposed, NOT implemented)

To move from seams to a defensible minimal slice without changing architecture:

**A. Opt-in wiring (low risk):**
- Modify CLI bootstrap / `arion/interfaces/cli.py` to check `ARION_LLM_PROVIDER`; if set, call `build_router()` and pass to engine; else default deterministic. No change to default behavior.
- Add `tests/test_model_integration.py` (new) with fake transport (injectable `Transport`) to test full `RealModelPlanner` → adapter → validation → authorization pipeline without network.

**B. Security verification (low risk, high value):**
- Test adversarial goal (`"delete /etc/passwd"`) against registered capabilities with `filesystem.write` boundary; verify authorization denies; verify plan validation passes structurally but execution denied. Confirm model never grants permission.
- Test malformed adapter response (invalid JSON, wrong schema, unknown capability); verify typed failure, retry budget, fallback.
- Test reflection with authority-bearing content (`"approve this"`); verify `ReflectionValidationError` and non-storage.

**C. Observability verification:**
- Confirm all events emitted in success/failure paths using injectable `EventLogger` mock.
- Confirm `model.response.received` contains only metadata; confirm `plan.validation.failed` contains no raw content.

**D. Documentation / operator:**
- Document `ARION_LLM_*` env surface and failure categories.
- Document security model: model is untrusted planner; authorization is authoritative; capability containment is defence-in-depth.

**Not included in minimal slice (M9-A only):** vocabulary expansion (M9-B), model-provider diversity beyond openai-compatible, multi-model routing, persistent plan caching from model, automatic replanning with model feedback — these extend M9-A but aren't required for the first production-quality integration.

---

## 5. Security model for model-generated plans

The security architecture explicitly prevents the model from becoming an authority. Key rules (all confirmed by source):

1. **No authorization authority:** `PermissionPolicy.decide()` uses `ActionSpec.required_scope`, `resource` boundaries (ADR-061), `actor` identity — never reads `PlanSchema.scope` or model output for policy.
2. **No resource boundary authority:** `_resource_allowed()` reads `ActionSpec.resources` (ADR-061) and `ResourcePolicy.boundaries`; model-proposed `params` are validated but boundary is fixed.
3. **No execution policy authority:** `retry_safe`, `reversible`, `side_effects` come from `ActionSpec`; model cannot declare them.
4. **No verification authority:** `VerificationPolicy` from `ActionSpec`; verification is independent of plan origin.
5. **No approval authority:** `REQUIRE_APPROVAL` is from metadata + policy; model cannot grant approval.
6. **Reflection non-authority:** `reflection_schema` explicitly forbids `scope`, `permissions`, `authorize`, `boundary`, `allowed`, etc. `ModelReflector` validates strictly; invalid reflections are never stored or applied.
7. **No credential exposure:** `OpenAICompatModelRouter` never logs/persists `api_key`; audit events never include prompts/responses/credentials.
8. **No silent fallback:** `model.fallback` event emitted; source marker explicit; same pipeline.
9. **No programming-error masking:** unexpected exceptions in `RealModelPlanner.plan()` raised durably (never retried/fallback).

**Attack surface bounded:** the only adversarial path is through `goal_description` (prompt injection to produce structurally valid malicious plan). But malicious plan for a dangerous capability is denied by authorization + resource boundaries — the user could have requested the same directly via CLI. There is no escalation path from model output to unauthorized execution.

---

## 6. Current capability vocabulary (confirmed by source + audit)

| Capability | Actions | Resource kind(s) | Notes |
|---|---|---|---|
| `filesystem.read` | `read`, `list` | `filesystem` | Read-only; `_resolve_inside` sandbox |
| `filesystem.write` / `append` | `write`, `append` | `filesystem` | Mutating; requires authorization + mutation lock + approval if configured |
| `git.log` | `log`, `branches` | `git` (metadata) | Read-only `.git` inspection; no shell |
| `http.get` | `get` | `url` | Network access; `UrlBoundary` required; no POST/other methods |

No `delete`, `move`, `mkdir`, `shell`, `process`, `search`, `post`, `email`, `scheduled`, `vector_db`, `browser`. Total: 7 actions (per M7 audit, `docs/m7-capability-audit-2026-09-05.md`).

---

## 7. M9-B candidate matrix (read-only inventory; not implemented)

| Candidate | Why needed | Layer changes | Authz implications | Resource declaration | Verification | Rollback / recovery | Test burden | Security risk | Depends on M9-A? |
|---|---|---|---|---|---|---|---|---|---|
| `delete` | Remove files; needed for cleanup, temp files | Cap + registry; `ActionSpec` with `side_effects=irreversible`; authorization must deny by default or require approval | High — irreversible; must require `REQUIRE_APPROVAL` and mutation lock | `filesystem:delete` kind + path boundary | Non-empty check after delete; audit event | No rollback; checkpoint records pre-state for forensic | High (destructive) | **High** — must never be default-allowed; requires explicit operator config + approval | No |
| `move` / `rename` | Organize / archive; common workflow | Cap + registry | Same as write; `reversible` if same filesystem; must check destination boundary (ADR-061 C4 applies) | Two resources (`source`, `dest`) — ADR-061 fully required | Verify both source/dest after; fingerprint change | Can rollback by reverse move if destination still exists | Medium | **Medium** — source/dest both boundary-checked; no escalation if policy strict | No |
| Structured filesystem search | Find targets for read/write; improves planning quality | Could be capability or planner feature; if capability: new action `search` / `find` | Read-only if `search`; if combined with `write` it needs authz on results | `filesystem` kind; result set could be large | Non-empty results; bounded result count | No mutation | Low | **Low** — read-only; risk if results used for unvalidated write (but authz checks write separately) | Partially (better planning) |
| HTTP `post` / `put` / `patch` / `delete` | Write to APIs; webhooks (ADR-059 already uses notifications) | New `url` action; `side_effects=mutating`; `retry_safe` depends on idempotency | Must have `url` boundary; POST implies mutation; must require approval or explicit allowlist | `url` kind + path/method constraints | Response status + body check; bounded | May need retry/rollback; depends on endpoint idempotency | Medium | **Medium-High** — external system mutation; must never auto-approve; boundary must restrict method + domain | No |
| Scheduled triggers | Recurring goals / time-based execution | Engine + scheduler; new capability or engine feature | Must not bypass authorization at trigger time; authorization must be re-checked at execution time | `schedule` kind or no resource | Execution completed | Checkpoint / resume at scheduled time | High (durable state + time) | **Medium** — trigger does not grant authority; authorization re-checked at run | No |
| Additional Git (`branch`, `commit`, `checkout`) | Source control integration | Cap + registry; `git` kind | Read-only mostly; `checkout` changes working tree — must check boundary | `git` kind + repo root | Metadata verification | Can restore via `git` itself | Low-Medium | **Low** — read-only; `checkout` would need write-like authorization | No |

**Dependency analysis:** None of M9-B candidates require M9-A to function. The authorization, resource declaration (ADR-061), verification, and capability registry layers already support them. M9-A improves planning quality (model can propose `delete` or `search` steps correctly), but M9-B can be implemented with the deterministic planner (current default) just as well. **Sequence confirmed: M9-A first improves intelligence quality; M9-B can follow independently or be interleaved after M9-A proves end-to-end.**

---

## 8. Recommended sequencing (confirmed by audit, not assumption)

**Phase 1 — M9-A minimal slice (proposed):**
1. Explicit opt-in wiring (CLI/bootstrap + `build_router()` + `RealModelPlanner`).
2. Fake-transport integration test (end-to-end with mock adapter, no credentials).
3. Security verification (adversarial goals + authorization denial + reflection validation + audit content check).
4. Observability verification (all events emit correctly; no leaks).
5. Operator documentation + env surface docs.

**Phase 2 — M9-A live validation:**
6. Real endpoint smoke test (with real `ARION_LLM_API_KEY`; bounded, temporary). Confirm `PlanSchema` matches expectations; confirm failure categories map; confirm fallback event.
7. Harden output bounds under real load (test with long goals, large catalogs).

**Phase 3 — M9-B planning (after M9-A stable):**
8. Decide which capability expansions are needed (start with `delete` or `search` or `post` depending on user use cases; not all at once).
9. Implement one at a time with full authz/resource/verification/test/security cycle.

**Not in M9 (explicit non-goals — see section 9):** multi-model routing, vector DB, RAG, browser automation, unrestricted shell, large tool catalogs, multi-agent, persistent model state changes to authorization.

---

## 9. Explicit non-goals for M9-A (and M9 overall, unless decided separately)

- **No replacement of authorization by model:** model proposes; policy decides. No change to `PermissionPolicy`, `ResourcePolicy`, or authorization event structure.
- **No change to capability containment:** capability sandbox (`_resolve_inside`) remains primary defense; policy remains additional layer per ADR-009.
- **No change to verification authority:** `VerificationPolicy` from `ActionSpec`; model never approves/rejects verification.
- **No change to approval requirements:** `REQUIRE_APPROVAL` from metadata; model cannot grant approval.
- **No persistence of raw model outputs:** prompts/responses/credentials never in audit, storage, or logs.
- **No automatic promotion from deterministic to model:** must remain opt-in (`env` + explicit wiring).
- **No vocabulary expansion in M9-A:** M9-B is separate; M9-A only validates the existing 7-action pipeline with a real model.
- **No architectural redesign:** `Planner` protocol, `ModelRouter` protocol, `Engine` loop unchanged.
- **No dependency additions:** `urllib` (stdlib) already used; no new packages required for M9-A.

---

## 10. Definition of Done — M9-A minimal slice

M9-A is complete when **all** of the following are true (not just component presence):

- [ ] `RealModelPlanner` is explicitly wired (opt-in, not default) into engine/CLI with real `OpenAICompatModelRouter`.
- [ ] A full end-to-end test (fake transport or real endpoint) proves: goal → model → `PlanSchema` → `PlanValidator` → authorization → execution → observation → verification → checkpoint, with both success and typed failure paths.
- [ ] Security audit confirms: adversarial goal → structurally valid plan → authorization denies → no unauthorized execution; reflection with authority content → validation fails → not stored; audit events contain no credentials/raw content.
- [ ] Fallback is verified: model failure → `model.fallback` event + `last_source="deterministic"` + same pipeline + no silent execution change.
- [ ] Output bounds enforced deterministically under test (long goal, large catalog, malicious JSON depth/size).
- [ ] Memory context bounded and never authoritative (digest bounded; exceptions don't suppress authz).
- [ ] All required observability events emit correctly in both paths (verified with injectable mock logger).
- [ ] Operator documentation exists: env surface, failure categories, retry budget, fallback behavior, security model.
- [ ] No source/test/dependency changes required beyond the opt-in wiring + tests + docs.
- [ ] Full suite at M9-A HEAD remains green (0 failures, 0 errors).

**Not required for M9-A Done:** real provider endpoint always available; multi-model routing; vocabulary expansion; persistent model feedback loops; automatic replanning with model suggestions (existing replanning via engine is sufficient).

---

## Appendix: References (all confirmed from repository at `64a7332`)

- Source files traced: `intelligence/model_planner.py`, `planner.py`, `router.py`, `plan_schema.py`, `plan_validator.py`, `intelligence/providers/openai_compat.py`, `providers/__init__.py`, `config.py`, `memory/model_reflector.py`, `memory/reflection_schema.py`, `orchestration/engine.py`, `orchestration/authz.py`, `capabilities/registry.py`, `capabilities/registry.py`, `observability/events.py`, `state/models.py`
- Tests traced: `test_model_planner.py`, `test_model_fallback.py`, `test_model_output_limits.py`, `test_model_config.py`, `test_model_reflection_wiring.py`, `test_model_router.py`, `test_model_runtime_wiring.py`, `test_boundary_every_resource.py`, `test_authz_resource_set.py`, `test_api_authz_unification.py`
- Documents: `docs/ADR-009-resource-aware-authorization.md`, `docs/ADR-057-model-backed-intelligence-path.md`, `docs/ADR-060-m7-verified-outcome-execution.md`, `docs/m7-capability-audit-2026-09-05.md`, `docs/architecture.md` (edited), `docs/adr/ADR-061-resource-role-declaration-boundary-check.md`
- Full-suite baseline: `pytest tests/ -q` at `64a7332` → 1953 passed / 2 skipped / 0 failed / exit 0 / ~205 s.
