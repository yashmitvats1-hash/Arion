# ADR-062 — Capability declaration contract enforced at registration (M9-B.2)

- **Status:** Approved — implemented in M9-B.2 on `arena/01a09aaa-arion`.
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)
- **Baseline:** `27a144b` (merge PR #14, `main`). Suite at baseline: **1979 collected, 1961 passed, 16 FAILED, 2 skipped, exit 1.**
- **Predecessor:** `docs/m9b.1-completion.md` claimed *"Full suite: PASS (exit 0) at HEAD `38c4574`"* and that `filesystem.search` was introduced *"through the existing authority boundary"*. Neither holds at `27a144b`; see the correction block appended to that document.
- **Related:** ADR-006 (capability/permission model), ADR-009 (resource-aware authorization), ADR-034 (sensitive error boundary), ADR-037 (resource presentation), ADR-060 D4/D5 (verification authority), ADR-061 D1/D2/D9 (resource-role declaration, fail closed at construction).

---

## Context

`Capability` is a `Protocol`:

```python
class Capability(Protocol):
    name: str
    description: str
    actions: list[ActionSpec]
    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...
```

A `Protocol` is a *type-checking* contract. Nothing at runtime verifies that an
object registered with `CapabilityRegistry.register()` satisfies it, and
`register()` was a bare dictionary insert:

```python
def register(self, capability: Capability) -> None:
    self._caps[capability.name] = capability
```

M9-B.1 added `filesystem.search` declaring `actions` as a list containing a
**raw dict** rather than an `ActionSpec`. That is structurally invisible to the
protocol and was admitted silently. Three production readers then dereferenced
`ActionSpec` attributes on a `dict`:

| Reader | Call site | Failure |
|---|---|---|
| `CapabilityRegistry.action_spec` | `registry.py:238` — `if a.name == action` | `AttributeError: 'dict' object has no attribute 'name'` |
| `CapabilityRegistry.capabilities_summary` | `registry.py:247` — `a.to_dict()` | `AttributeError: 'dict' object has no attribute 'to_dict'` |
| verification authority | `resolve_verification_policy(spec, ...)` | never reached — the spec could not be fetched |

Because `bootstrap.build_engine()` registers every shipped capability
unconditionally (`bootstrap.py:74–85`), one unreadable declaration took the
**whole catalog** down with it. Measured blast radius at baseline:

1. **The M9-A model path was dead in every default-built engine.**
   `RealModelPlanner.plan()` calls `registry.capabilities_summary()`
   (`model_planner.py:112`) to build the catalog handed to the provider. Every
   model-planned goal failed with `planning failed: 'dict' object has no
   attribute 'to_dict'`. A read-only, low-risk capability addition silently
   disabled the milestone that had just been wired.
2. **`arion capabilities` crashed** (`cli.py:398`) — the operator's own view of
   the vocabulary.
3. **`filesystem.search` could not transit the boundary at all.** Driving a real
   step through a real engine crashed in `_plan_steps_for_audit`
   (`engine.py:3897`) → `action_spec()`. On the deterministic planner path the
   `AttributeError` **escaped `run_goal()` unhandled** (process exit 1); on the
   model path the same defect was absorbed into a durable `planning failed:`
   task error. One fault, two failure disciplines — an ADR-034 concern
   independent of the declaration bug.
4. **16 tests failed at `main`**, across `test_model_runtime_wiring.py` (10),
   `test_live_provider.py` (3), `test_model_reflection_wiring.py` (2) and
   `test_cli.py` (1).

### Why the existing tests did not catch it

`tests/test_search.py` (8 tests, all passing at baseline) exercises
`FilesystemSearchCapability.execute()` **directly**, and its single authorization
test **hand-constructs** an `AuthorizationRequest` instead of deriving one from
the registry:

```python
reg = CapabilityRegistry()
reg.register(FilesystemSearchCapability(root))
req = AuthorizationRequest(..., resource=".", resource_kind="filesystem:path")
assert policy.decide(req).outcome.name == "ALLOW"
```

It never calls `reg.action_spec(...)` and never runs a step through the engine.
The suite tested **the capability**; M9-B.1 claimed to prove **the boundary**.
This is a test-*shape* gap — more direct-execution tests could never close it.

### Causal proof

Commenting out `bootstrap.py:81` (`registry.register(FilesystemSearchCapability(...))`)
alone, at baseline:

```
pytest tests/test_model_runtime_wiring.py tests/test_model_reflection_wiring.py \
       tests/test_cli.py tests/smoke -q     →  41 passed, 1 skipped
```

All 16 failures were attributable to that one registration.

---

## Decision

### D1 — The declaration contract is enforced at registration, fail closed

`CapabilityRegistry.register()` validates the capability **before** inserting it
and raises a typed `CapabilityDeclarationError` on any violation:

| Check | Refused when |
|---|---|
| identity | `capability is None` |
| `name` | absent, not a `str`, or blank |
| `description` | absent, not a `str`, or blank |
| `execute` | absent or not callable |
| `actions` | absent, not a `list`/`tuple`, or **empty** |
| each action | not an `ActionSpec` instance |
| each action `name` | not a non-empty `str` |
| action names | duplicated within one capability |
| each action `required_scope` | not a non-empty `str` |

`CapabilityDeclarationError` subclasses `ValueError`, deliberately mirroring
ADR-061's `ResourceDeclarationError(ValueError)`: both are *"the declaration is
malformed"* faults raised at construction, and both are caller bugs rather than
runtime capability failures (`CapabilityError`). The registry now refuses
ambiguity at the same layer ADR-061 D2 refuses it inside `ActionSpec`.

**Rationale for refusing rather than coercing.** A raw mapping could be
mechanically converted to an `ActionSpec`. That is rejected: coercion would
silently invent the metadata the mapping omitted (`risk`, `side_effects`,
`reversible`, `retry_safe`, `default_verification` all have defaults), so a
capability that declared nothing about its own danger would be admitted as
`risk="low"`, `side_effects="read_only"`, `reversible=True` — an authorization
decision made by a dataclass default rather than by the capability author.
ADR-061 D2's principle applies unchanged: *ambiguity is never silently resolved
by a precedence rule.*

**Rationale for refusing an empty `actions` list.** No step can ever target such
a capability, so it is discoverable noise in the planning catalog rather than a
capability. Fail closed.

### D2 — Registration implies transit

A capability is not "added" when it is in the registry; it is added when a step
targeting it can be driven
`plan → validate → authorize → execute → verify` through the real engine and
reach a **durable terminal status**. `tests/test_capability_boundary_transit.py`
makes that a permanent gate over the shipped vocabulary, in both directions:

- **Allowed actions must complete:** `filesystem.read` (read, list),
  `filesystem.search`, `filesystem.write`, `filesystem.append`, `git.log` (log,
  branches) each reach `TaskStatus.COMPLETED` with a succeeded step and a
  non-null observation. The mutating case additionally asserts the
  `mutation.lock.requested → acquired → released` window.
- **Fail-closed actions must be denied durably, never crash:** `http.get` with
  scope granted but no `url` boundary denies with *"no resource boundary
  configured for resource kind 'url'"* (ADR-009 step 4) and emits
  `permission.denied`; `filesystem.write` under the **default** bootstrap policy
  is refused and never acquires a lock; a traversal `directory` for `search`
  denies at authorization and the capability is never invoked.
- **The approval seam is part of transit:** a search step forced to
  `REQUIRE_APPROVAL` blocks durably, then completes after resolution.
- **`run_goal()` must not raise.** This is the exact path that leaked an
  unhandled `AttributeError` at baseline.

The transit tests drive steps through an explicit one-step planner. That is not
a shortcut: `DeterministicPlanner` can only emit 5 of the 8 registered actions
(`filesystem.read` read/list, `git.log` log/branches, `http.get` get —
`_ACTION_TEMPLATES` holds only `read`/`list`, and `planner_requirements()`
returns only `{http.get}`, `{git.log}` or `{filesystem.read}`). `filesystem.write`,
`append` and `search` are **unreachable deterministically**, so an injected plan
is the same route a model-produced plan takes.

### D3 — The default bootstrap must be self-consistent

`tests/test_bootstrap_registry_invariant.py` pins the invariants whose absence
let M9-B.1 land:

- `build_engine()` registers exactly the six shipped capabilities and eight
  actions.
- `capabilities_summary()` succeeds, is complete, and is **JSON-serializable**
  (it crosses a wire to a provider adapter).
- Every `action_spec(capability, action)` resolves to an `ActionSpec` with a
  non-empty `required_scope`.
- `filesystem.search` is discoverable *in the model catalog*, with its ADR-061
  D9 derived `resources` view present.
- `RealModelPlanner` reaches a recording router with the **full** catalog and
  returns a validated plan whose `scope` came from the registry, not the model —
  i.e. the M9-A path is alive.
- `arion capabilities` exits 0 and lists the vocabulary.

---

## Invariants

| # | Statement | Source |
|---|---|---|
| 22 | A capability whose declaration cannot be read is never admitted **at registration** | `registry.register` → `validate_capability_declaration` |
| 23 | A refused registration never half-populates the registry (`has`/`get`/`list`/`action_spec`/`capabilities_summary` all unchanged) | `register` validates before insert |
| 24 | Every action admitted at registration is an `ActionSpec`, so the ADR-061 D2 and ADR-060 D4/D5 guarantees that `ActionSpec` construction establishes hold for every admitted action | D1 |
| 25 | Every admitted action declares a non-empty `required_scope`, providing the authorization layer with a scope it can evaluate | D1 |
| 26 | No capability is admitted with duplicate action names (`action_spec` could only resolve the first) | D1 |
| 27 | Registered ⇒ transitable: every registered action reaches a durable terminal status through the engine, never an exception out of `run_goal()` | D2 |
| 28 | The default bootstrap yields a catalog that is complete and JSON-serializable | D3 |
| 29 | Declaration faults are construction-time `ValueError`s, distinct from runtime `CapabilityError`s | D1 |

**Temporal scope of invariants 22, 24 and 25.** These are **registration-time**
invariants, not immutability invariants. `Capability.actions` is a mutable
attribute, and the registry validates it once, at admission; nothing re-checks it
before each read. Mutating an admitted capability's `actions` in place — e.g.
`registry.get(name).actions.append({...})` — reintroduces exactly the fault class
this ADR closes, and `capabilities_summary()` fails again with the same
`AttributeError`. Measured, and deliberately accepted: see *Limitation* below.

---

## Ownership boundary: registry validity vs. policy permission

Invariant 25 puts the registry's opinion on a field authorization owns, so the
division is stated explicitly:

| Layer | Question it asks | Question it must NOT ask |
|---|---|---|
| `CapabilityRegistry.register` | *Did the action declare an authorization scope at all?* | Is that scope permitted? |
| `PermissionPolicy.decide` | *Is this scope permitted for this actor, action and resource?* | Did the capability declare itself well? |

The contract checks **presence and shape only**. It never reads `boundaries`,
`allowed_scopes`, `denied_scopes`, `risk`, `side_effects`, `resource_kind`,
approval configuration or actor identity — verified by inspection: with
docstrings and comments stripped, the only capability/action attributes
`validate_capability_declaration` dereferences are `name` and `required_scope`
(`description`, `execute` and `actions` are read via `getattr`).

It is also **read-only**: `register()` either admits the object unchanged or
raises. Measured — `ActionSpec.to_dict()` is identical before and after
registration and the registry stores the same object identity — so the contract
cannot rewrite `scope`/`risk`/`side_effects` and therefore **cannot widen what
policy later decides**. (The only mutation anywhere in the declaration path
remains ADR-061 D9's `ActionSpec.__post_init__`, which mirrors the primary
resource role into the singular fields.)

Where the two layers meet, the outcomes agree rather than compete: an action
declaring `required_scope=""` is refused at registration, and `policy.decide()`
independently returns `DENY — "scope '' not permitted by policy"`. The contract
moves an identical fail-closed outcome to an earlier layer with a better
diagnostic; it does not substitute its judgement for policy's.

---

## Declaration refusal is not an authorization denial

A `CapabilityDeclarationError` means the capability **was not admitted** because
its declaration was invalid. It must not be translated into, or used to
simulate, a policy denial. Runtime attempts to use an admitted capability remain
subject to the normal authorization path and produce authorization outcomes
independently of declaration validity.

The two are different events with different evidence, and conflating them is a
live hazard once plugin-shaped registration exists:

| | Declaration refusal | Authorization denial |
|---|---|---|
| When | at `register()`, before admission | at `policy.decide()`, per step |
| Object | the capability's *contract* | one *step's* request |
| Audit | none — no engine, no task, no step exists | `permission.denied` on a durable task |
| Operator read | "this capability is malformed" | "this action is not permitted" |

The failure mode to prevent is a loader that does
`invalid capability → silently skip → step fails "capability not found"`, which
*presents* as an authorization outcome while no authorization decision was ever
made. A caller that catches `CapabilityDeclarationError` must surface it as a
declaration fault, not absorb it into the deny path.

Note that the engine already fails closed on an unresolvable spec independently
of this ADR (`engine.py:4330–4340`): durable `StepStatus.FAILED` with
`"capability not found"` / `"unknown action … for capability …"`, no crash. So an
unadmitted capability is inert either way — the distinction above is about
*honest diagnostics*, not about safety.

---

## Consequences

**Positive**

- The fault class is closed **at the registration boundary** rather than three
  layers downstream. A future capability that misdeclares itself fails at
  import/registration with a diagnostic naming the capability, the action index,
  the type found and the `ActionSpec` requirement — not as an `AttributeError`
  inside planning.
- The suite is green again: **2042 collected, 2040 passed, 2 skipped, 0 failed,
  exit 0** at M9-B.2 HEAD. The 16 baseline failures were resolved by a two-file
  declaration fix (`registry.py` contract + `search.py` redeclaration); 63 new
  tests were added and **no pre-existing test was modified**.
- M9-A is restored: the model planner receives a live catalog from a
  default-built engine.
- ADR-061 D2's fail-closed discipline now extends one level up, from
  `ActionSpec` construction to `Capability` registration — the two declaration
  layers of the capability model refuse ambiguity the same way.
- `filesystem.search` is now genuinely authorizable, plannable and verifiable.
  Its declaration is unchanged in substance (same scope, resource role, bounds,
  default verification), so `execute()` behaviour is byte-identical and all 8
  pre-existing `test_search.py` tests pass unmodified.

**Newly visible (deliberately not fixed here)**

Declaring `directory` as both `required: False` in `param_schema` *and* the
resource role means a directly-called `execute()` may omit it (defaulting to the
sandbox root) while `PlanValidator._validate_resource` and the ADR-009 boundary
make it mandatory for any *planned* step. That asymmetry is fail-closed and is
now documented in `capabilities/search.py`: an implicit "the whole sandbox"
resource is exactly what resource-aware authorization exists to prevent.

**Limitation — registration-scoped, not an immutability guarantee**

Invariant 22 holds **at admission**, not forever. `Capability.actions` is a
mutable attribute and the registry validates it once; no reader re-checks it.
Measured bypass:

```python
reg.register(FilesystemSearchCapability(root))      # admitted, contract satisfied
reg.get("filesystem.search").actions.append({"name": "smuggled", ...})
reg.capabilities_summary()   # AttributeError: 'dict' object has no attribute 'to_dict'
```

Re-running `validate_capability_declaration` on the same object catches it, but
nothing calls it again. Two strengthenings were considered and **rejected**:

- *Freeze `actions` into a tuple at registration.* Rejected: it would make
  `register()` **mutate** the capability, forfeiting the read-only property that
  guarantees the contract cannot alter what authorization later sees.
- *Re-validate defensively inside `action_spec()` / `capabilities_summary()`.*
  Rejected: it puts an O(actions) check on the hot planning path to defend
  against a post-registration mutation that **no code in Arion performs**.

The residual risk is accepted and bounded: the D2 transit gate and the D3
bootstrap invariant both exercise the real registry at test time, so a mutation
introduced anywhere in Arion's own composition is caught by the suite. A future
plugin loader that hands the registry third-party objects *after* bootstrap is
the point at which this should be revisited.

**Cost**

- `register()` is no longer a bare insert. The validation is O(actions) and runs
  once per capability at bootstrap; no measurable runtime cost.
- **Registration is now a throwing operation, so `build_engine()` can raise where
  it previously did not.** Verified transactional: `bootstrap.py:231` is
  `except BaseException: lifecycle.shutdown(); raise`, so a refusal during
  composition closes the already-opened `SQLiteStorage` and no process resource
  leaks. Any future caller registering capabilities in a loop must handle
  `CapabilityDeclarationError` explicitly — and per *Declaration refusal is not an
  authorization denial* above, must not absorb it into the deny path.
- Third-party/test capabilities must now be honest declarations. Surveyed at
  baseline: every capability-shaped stub in `tests/` already declares `name`,
  `description`, `execute` and `ActionSpec` actions — **zero** existing stubs
  required changes, and no test was modified to accommodate this ADR.

---

## Rejected alternatives

- **R1 — Coerce mappings into `ActionSpec` at registration.** Rejected: invents
  risk/side-effect/verification metadata from dataclass defaults, making an
  authorization-relevant decision on the capability author's behalf (D1).
- **R2 — Duck-type the readers (`getattr(a, "name", None)` / `a.get("name")`).**
  Rejected: it would make `action_spec` and `capabilities_summary` tolerant of
  two representations, which is precisely the dual-representation problem
  ADR-061 D9 eliminated for resource declarations. It also defers the fault to
  authorization time, where a missing scope fails closed and the capability
  becomes silently unusable.
- **R3 — Fix only `search.py`, no registry contract.** Rejected: restores green
  but leaves the class open. The next capability can repeat it, and the invariant
  "registered ⇒ governable" remains untested.
- **R4 — A lint/type-check gate in CI instead of a runtime check.** Insufficient
  alone: the `Protocol` is structural, so a dict-in-list passes `mypy` unless
  the annotation is tightened, and CI cannot protect a caller who constructs a
  registry at runtime with a plugin capability. Runtime refusal is the layer that
  holds. (A tightened static annotation is complementary, not a substitute.)
- **R5 — Validate lazily, at first `action_spec()`/`capabilities_summary()`
  call.** Rejected: moves the failure back downstream and makes it
  data-dependent — the exact shape of the M9-B.1 incident.

---

## Follow-ups (owned by M9-B.3, not this ADR)

Recorded here because the M9-B.2 investigation measured them; none is in scope
for this ADR. See `docs/m9b.2-architecture-proposal.md` §3–§4 for the evidence.

- **P0 — two definitions of "mutating". ✅ LANDED (M9-B.3 P0, this branch).**
  `registry.is_mutating()` counts `irreversible`; `engine.py` compared
  `== "mutating"` literally at lines 365, 3039, 3422 and 4725. An `irreversible`
  action therefore ran end-to-end with **zero mutation locks** (measured),
  skipping the durable lock (ADR-021), bounded waiting (ADR-022), the FIFO waiter
  queue (ADR-023), same-resource dispatch gating (ADR-024/025), post-wait
  re-authorization and recovery mirroring (ADR-020) — while ADR-060 D5 *still*
  refused to let it run without a verification policy.

  All four sites now defer to `registry.is_mutating()`, so exactly one predicate
  classifies "changes the world". Behaviour is unchanged for the entire current
  vocabulary (every shipped action declares `read_only` or `mutating`, where the
  two definitions already agreed); the change is reachable only by an action
  declaring `irreversible`, which is what `filesystem.move` will do.

  Pinned by `tests/test_irreversible_mutation_locking.py` (13 tests), verified to
  fail 8/13 with the fix reverted — including a contention case showing the
  baseline let an `irreversible` mutation **run while another owner held the lock
  on that exact resource**, and a source-level gate that fails on *any* literal
  `side_effects` comparison against a taxonomy value (so a future
  `== "irreversible"` special case is caught too). Negative controls confirm the
  fix does not widen: `none`/`read_only` still take no lock and create no
  recovery record.
- **ADR-061 invariant 13 is unenforced.** `canonical_identities()` and
  `unresolved_roles()` have **no production consumers**; the lock layer M8
  deferred as "C7" does not exist. `_lock_canonical` locks the primary role
  only, so a two-resource move mutates its destination unlocked.
- **The approval fingerprint covers the primary resource only** — two moves with
  the same source and different in-boundary destinations fingerprint
  identically. `security_relevant_params` is not a valid channel for a second
  *resource*: it is stored raw, while resources go through `present_resource()`
  specifically so the exact identifier is not persisted (ADR-037).
- **`PlanValidator._validate_resource` validates only the primary role**, so a
  model omitting a secondary resource is denied at authorization instead of
  receiving a typed plan-time error it could retry against.
- **`MutationRecovery.resource` is scalar**, so a failed two-resource mutation
  cannot name where the data went.
- **No two-resource verification policy exists**; `KNOWN_VERIFICATION_POLICIES`
  is scalar by construction and ADR-060 D5 forbids running a mutation under
  shape-only verification.

---

## Tests

- `tests/test_capability_declaration_contract.py` (40) — every clause of D1,
  refusal atomicity (invariant 23), the `ValueError` parallel to ADR-061 D2, and
  the shipped vocabulary as a regression gate.
- `tests/test_capability_boundary_transit.py` (15) — D2, both directions,
  including the approval seam and the `run_goal()` no-raise path.
- `tests/test_bootstrap_registry_invariant.py` (8) — D3, including the live
  model-catalog round trip and the CLI surface.
- Pre-existing `tests/test_search.py` (8) — **unmodified**, still passing.
- Full suite at M9-B.2 HEAD: **2042 collected, 2040 passed, 2 skipped, 0 failed,
  0 errors, exit 0** (baseline was 1979 / 1961 / 2 / **16 failed** / exit 1).
  +63 tests, and **no pre-existing test was modified**.
