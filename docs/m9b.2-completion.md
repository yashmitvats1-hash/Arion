# M9-B.2 Completion — Capability Declaration Contract

- **Decision record:** `docs/adr/ADR-062-capability-declaration-contract.md`
- **Investigation that produced it:** `docs/m9b.2-architecture-proposal.md`
- **Corrected predecessor:** `docs/m9b.1-completion.md` (correction block appended; original text preserved)
- **Baseline:** `27a144b` (merge PR #14, `main`) — **1979 collected, 1961 passed, 16 FAILED, 2 skipped, exit 1**
- **HEAD:** **2042 collected, 2040 passed, 2 skipped, 0 failed, 0 errors, exit 0**

---

## What M9-B.2 proves (and why it is not "another endpoint")

M9-B.1 proved a capability class can be *added to the registry* without touching
`authz.py`. M9-B.2 proves a different property:

> **Declaration → governance fidelity.** A capability that declares a contract the
> boundary can read is governed by it — authorized, locked, approved, verified —
> on every resource it declares; a capability that misdeclares itself is
> **refused at registration**, not discovered as a crash three layers downstream.

The property was not hypothetical. Measured at baseline, `filesystem.search`
declared `actions` as a raw dict, so `registry.action_spec()` and
`capabilities_summary()` raised `AttributeError`. Since `build_engine()` registers
every capability unconditionally, the whole catalog failed:

| Surface | Baseline behaviour |
|---|---|
| `RealModelPlanner.plan()` → `capabilities_summary()` (`model_planner.py:112`) | **Every model-planned goal failed.** The M9-A path was dead in every default-built engine. |
| `arion capabilities` (`cli.py:398`) | **Crashed.** |
| A planned `filesystem.search` step | **Could not execute** — crashed in `_plan_steps_for_audit` (`engine.py:3897`); on the deterministic path the exception escaped `run_goal()` unhandled. |
| Full suite | **16 failures** across 4 files. |

Causal proof: removing `bootstrap.py:81` alone turned all 16 failures into
`41 passed, 1 skipped`.

---

## Changes

### Source (2 files)

| File | Change |
|---|---|
| `arion/capabilities/registry.py` | Added `CapabilityDeclarationError(ValueError)` and `validate_capability_declaration()`; `CapabilityRegistry.register()` now validates **before** inserting. |
| `arion/capabilities/search.py` | Redeclared the `search` action as an `ActionSpec`. Declaration is substantively unchanged (same scope, resource role, bounds, default verification), so `execute()` behaviour is byte-identical. Module docstring records the `required: False` param vs. mandatory resource-role interaction. |

No change to `authz.py`, `engine.py`, `PlanValidator`, `ResolvedResource`,
`bootstrap.py`, or any other capability. No dependency additions.

### The contract enforced at `register()` (ADR-062 D1)

Refused with a typed `CapabilityDeclarationError` when:

- the capability is `None`;
- `name` / `description` is absent, not a `str`, or blank;
- `execute` is absent or not callable;
- `actions` is absent, not a `list`/`tuple`, or **empty**;
- any action is not an `ActionSpec`;
- an action `name` is blank, or action names are duplicated;
- an action's `required_scope` is blank.

Validation precedes insertion, so a refused registration never half-populates the
registry (`has`/`get`/`list`/`action_spec`/`capabilities_summary` all unchanged,
and previously registered capabilities are untouched).

Coercion was explicitly rejected: converting a mapping to an `ActionSpec` would
invent `risk`, `side_effects`, `reversible`, `retry_safe` and
`default_verification` from dataclass defaults — an authorization-relevant
decision made on the capability author's behalf.

### Tests (3 new files, 63 tests; 0 pre-existing tests modified)

| File | # | Covers |
|---|---|---|
| `tests/test_capability_declaration_contract.py` | 40 | Every clause of D1; refusal atomicity; the `ValueError` parallel to ADR-061 D2; positive controls; the shipped vocabulary as a regression gate; the exact `search` declaration preserved. |
| `tests/test_capability_boundary_transit.py` | 15 | D2 — every registered action driven `plan → validate → authorize → execute → verify` through the real engine to a durable terminal status, never an exception out of `run_goal()`. Allowed actions complete (7 parametrized cases incl. the mutation-lock window); fail-closed actions deny durably; approval seam included. |
| `tests/test_bootstrap_registry_invariant.py` | 8 | D3 — default catalog complete, `ActionSpec`-backed, JSON-serializable, reaches `RealModelPlanner` with `filesystem.search` present; model can propose a `search` step that validates; `arion capabilities` exits 0. |

All 8 pre-existing `tests/test_search.py` tests pass **unmodified**.

### Docs

- `docs/adr/ADR-062-capability-declaration-contract.md` — new (context, D1–D3,
  invariants 22–29, 5 rejected alternatives, M9-B.3 follow-ups).
- `docs/m9b.1-completion.md` — dated correction block appended; original text
  preserved verbatim (the convention set when the M7 audit was left intact and
  superseded by the M8 note).
- `docs/architecture.md` — new sections for M9-B.1 search and the ADR-062
  contract; security-boundary bullet updated to include `filesystem.search`
  containment.
- `docs/m9b.2-architecture-proposal.md` — the read-only investigation, with
  reproduction commands.

---

## Definition of Done

| Criterion | Status |
|---|---|
| Full suite green at M9-B.2 HEAD (0 failed, 0 errors, exit 0) | **2042 / 2040 passed / 2 skipped / exit 0** |
| `filesystem.search` executes through the engine and verifies | transit test `id=search` + `test_search_transit_returns_real_results` |
| `arion capabilities` works | `test_cli_capabilities_command_succeeds` |
| `RealModelPlanner` receives a non-empty catalog from a default-built engine | `test_model_planner_receives_a_live_catalog_from_the_default_bootstrap` |
| Permanent regression gates for both | boundary-transit + bootstrap-invariant suites |
| A deliberately misdeclared capability is refused at `register()` with a typed error | 20 refusal cases in the declaration-contract suite |
| No pre-existing test modified to accommodate the change | `git diff --stat` touches no existing test file |
| ADR recorded | ADR-062 |
| Predecessor doc corrected without erasing history | correction block appended |

---

## Deliberately out of scope (owned by M9-B.3)

Recorded in ADR-062's follow-ups with measured evidence in
`docs/m9b.2-architecture-proposal.md` §3–§4:

- **P0 — two definitions of "mutating".** `registry.is_mutating()` counts
  `irreversible`; `engine.py` compares `== "mutating"` literally at lines 365,
  3039, 3423, 4725. An `irreversible` action ran end-to-end with **zero mutation
  locks** (measured). Must land before any capability declares `irreversible` —
  and `filesystem.move` will.
- **ADR-061 invariant 13 is unenforced.** `canonical_identities()` and
  `unresolved_roles()` have **no production consumers**; the lock layer M8
  deferred as "C7" was never built. `_lock_canonical` locks the primary role
  only, so a two-resource move mutates its destination **unlocked** (measured:
  `write(dest)` has a disjoint lock identity — no mutual exclusion).
- **The approval fingerprint covers the primary resource only** — two moves with
  the same source and different in-boundary destinations fingerprint
  **identically** (measured). `security_relevant_params` is not a valid channel
  for a second *resource*: it is stored raw, while resources go through
  `present_resource()` so the exact identifier is never persisted (ADR-037).
- **`PlanValidator._validate_resource` checks only the primary role** — a model
  omitting `dest` is denied at authorization instead of getting a typed
  plan-time error it could retry against (measured: `dest=""` accepted at plan
  time).
- **`MutationRecovery.resource` is scalar** — a failed two-resource mutation
  cannot name where the data went.
- **No two-resource verification policy exists** — `KNOWN_VERIFICATION_POLICIES`
  is scalar by construction, and ADR-060 D5 forbids running a mutation under
  shape-only verification.

`filesystem.move` remains the right **target** for these (it is the only M9-B
candidate that makes the M8 substrate load-bearing) and the wrong next commit at
baseline. With the contract now enforced and the suite green, M9-B.3 can start
from P0.
