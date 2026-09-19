# M9-B.2 Architecture Proposal — What The Next Slice Must Prove

**Status:** Read-only investigation + proposal. No source, test or dependency
changes were made to produce *this document* (two throwaway probe scripts were
run outside the repo tree; reproduction commands are inlined in §10).
**Outcome:** §8's **M9-B.2 slice has since been implemented** on
`arena/01a09aaa-arion` — see `docs/adr/ADR-062-capability-declaration-contract.md`
and `docs/m9b.2-completion.md`. Suite went from 1979/1961/**16 failed** to
**2042/2040 passed/2 skipped/0 failed, exit 0**. §8's **M9-B.3 slice
(`filesystem.move`) is NOT implemented** and remains a proposal; the §3–§4
measurements below still describe `main` exactly as they did at baseline.
**Baseline:** `27a144b` (merge PR #14, `main`), branch `arena/01a09aaa-arion`.
**Predecessors:** `docs/m9b.1-completion.md`, `docs/m9-architecture-sequencing-proposal.md`,
`docs/m8-completion-note-2026-09-06.md`, ADR-061.

---

## 0. Bottom line

The question "what should M9-B.2 prove that M9-B.1 didn't?" has an evidence-backed
answer, and it is not the answer the sequencing document anticipated.

**M9-B.1 did not prove what its completion note claims.** `filesystem.search` was
added to the *registry*, but it never crossed the *authority boundary*: it cannot
be planned, authorized, or executed through the engine, and its presence breaks
the model-planning catalog for every other capability. The suite at `main` is
**red — 16 failures of 1979** — and all 16 trace to a single line.

So M9-B.2 should prove a different architectural property than "another capability
works":

> **Declaration → governance fidelity.** A capability that declares a contract the
> boundary can read is governed by it (authorized, locked, approved, verified,
> recoverable on **every** resource it declares); a capability that misdeclares
> itself is **refused at registration**, not discovered as a crash at planning time.

`filesystem.move` is the right *target* for that property — it is the only
candidate that makes M8's multi-resource substrate load-bearing — but it is the
wrong *next commit*, because five of the six subsystems it needs are
single-resource by assumption (§3) and the baseline is red (§1).

**Recommended sequencing: M9-B.2 = declaration contract + boundary-transit test
class (restores green, ~small). M9-B.3 = `filesystem.move` (multi-resource
governance, ~large).** Details in §6–§8.

---

## 1. Measured baseline state

| Measurement | Value | Method |
|---|---|---|
| Tests collected | **1979** | `pytest tests/ --collect-only -q` |
| Passed | **1961** | `pytest tests/ -q --tb=no` |
| **Failed** | **16** | same |
| Skipped | 2 | same |
| Exit code | **1** | same |

This contradicts `docs/m9b.1-completion.md`: *"Full suite: PASS (exit 0) at HEAD
`38c4574`."* Whatever was true at `38c4574`, it is not true at the merge commit
that landed on `main`.

Failing files: `tests/smoke/test_live_provider.py` (3), `tests/test_cli.py` (1),
`tests/test_model_reflection_wiring.py` (2), `tests/test_model_runtime_wiring.py` (10).

---

## 2. Finding A — `filesystem.search` never crossed the boundary

### A1. The capability declares dicts, not `ActionSpec`

Every other capability builds `ActionSpec(...)` objects. `arion/capabilities/search.py`
declares a **plain dict**:

```python
class FilesystemSearchCapability:
    actions = [
        {                                   # <-- dict, not ActionSpec
            "name": "search",
            "required_scope": "filesystem:read",
            "resource_kind": "filesystem:path",
            "resource_param": "directory",
            ...
        }
    ]
```

Measured consequence (`CapabilityRegistry` is typed against `Capability.actions:
list[ActionSpec]`, but nothing enforces it):

```
write actions types : ['ActionSpec']
search actions types: ['dict']
action_spec(search) RAISES:  AttributeError 'dict' object has no attribute 'name'
capabilities_summary RAISES: AttributeError 'dict' object has no attribute 'to_dict'
```

Two `ActionSpec` guarantees are therefore silently forfeited for `search`:
the ADR-061 D2 construction-time resource validation (`__post_init__`), and the
ADR-060 D4/D5 verification-authority resolution (which is keyed on `spec`).

### A2. Blast radius: the default engine's model catalog is dead

`bootstrap.build_engine()` registers search **unconditionally** (`bootstrap.py:81`).
`capabilities_summary()` has exactly two production consumers:

| Consumer | Effect |
|---|---|
| `intelligence/model_planner.py:112` — `catalog = registry.capabilities_summary()` | **`RealModelPlanner.plan()` always fails.** The entire M9-A model path is dead in any default-built engine. |
| `interfaces/cli.py:398` — `for cap in engine.registry.capabilities_summary()` | **`arion capabilities` crashes.** |

Observed in the failing tests: `planning failed: 'dict' object has no attribute 'to_dict'`.

This is the sharpest irony in the current state: **M9-B.1 broke M9-A.** A
read-only, low-risk, "no changes required to `authz.py`/`engine.py`/`PlanValidator`"
capability addition disabled the model-backed planner that M9-A had just wired.

### A3. `filesystem.search` cannot execute through the engine at all

Driving a step that targets it (`capability="filesystem.search"`, `action="search"`)
crashes in `engine._plan_steps_for_audit` (`engine.py:3897`) →
`registry.action_spec()` → `AttributeError`.

Worse, the failure mode is **inconsistent**:

- **Deterministic planner wired:** the `AttributeError` **escapes `run_goal()` as an
  unhandled exception** — the engine crashes rather than failing the task durably
  (measured: traceback out of `run_goal`, process exit 1). That is an ADR-034
  sensitive-error-boundary concern, not just a bug.
- **Model planner wired:** the same defect is caught inside planning and becomes a
  durable `planning failed:` task error.

One defect, two different failure disciplines, depending on which planner is wired.

### A4. Why 8 passing tests missed it

`tests/test_search.py` (8 tests) exercises `FilesystemSearchCapability.execute()`
**directly**, and its one authorization test **hand-constructs** an
`AuthorizationRequest` rather than deriving it from the registry:

```python
reg = CapabilityRegistry()
reg.register(FilesystemSearchCapability(root))
req = AuthorizationRequest(..., resource=".", resource_kind="filesystem:path")
assert policy.decide(req).outcome.name == "ALLOW"
```

It never calls `reg.action_spec(...)` and never runs a step through the engine. So
the suite tested **the capability**, not **the boundary** — and the boundary is
exactly the property M9-B.1 claimed to prove (*"introduce a new capability through
the existing authority boundary without creating a new authorization path"*).

This is a **test-shape** gap, not a test-count gap. Adding more direct-execution
tests for `search` cannot catch it; only a test that drives a registered capability
*through* the engine can.

### A5. Causal proof (single line)

Commenting out `bootstrap.py:81` alone:

```
pytest tests/test_model_runtime_wiring.py tests/test_model_reflection_wiring.py \
       tests/test_cli.py tests/smoke -q
→ 41 passed, 1 skipped   (was: 16 failed)
```

All 16 failures are attributable to one registration line. Restored afterwards;
working tree is clean.

---

## 3. Finding B — the M8 multi-resource substrate has **zero** production consumers

M8 (C1–C4) built the machinery for actions with more than one resource. Measured
against `main`, no shipped capability uses it and no shipped code path consumes
half of it:

| Capability | Resource declaration | Roles |
|---|---|---|
| `filesystem.read` (read, list) | `resource_kind`/`resource_param` sugar | 1 |
| `filesystem.write` | sugar | 1 |
| `filesystem.append` | sugar | 1 |
| `filesystem.search` | dict (see §2) | 1 (nominally) |
| `git.log` (log, branches) | sugar | 1 |
| `http.get` | sugar | 1 |

**No capability declares `resources=[ResourceRole(...), ResourceRole(...)]`.**
The only multi-role specs in the repo are synthetic fixtures in tests
(`tests/test_resource_views.py::_move_spec`).

And the derived canonical view is unconsumed in production:

| `resource_set.py` export | Production consumers | Test consumers |
|---|---|---|
| `resolve_resources` | `engine.py:1374`, `engine.py:4715` (→ primary only) | yes |
| `primary_resource` | `engine.py:4715` | yes |
| **`canonical_identities`** | **none** | `test_resource_views.py` only |
| **`unresolved_roles`** | **none** | `test_resource_views.py` only |

ADR-061 invariant 13 — *"Lock acquisition uses deterministic canonical ordering"* —
is **declared, derived, tested in isolation, and unenforced**. `resource_set.py`'s
own docstring defers enforcement to "the boundary check (C4) and the lock layer
(**C7**)". M8 shipped C1–C4. **C7 does not exist.**

Measured consequences for a hypothetical two-role `filesystem.move` (`source`,
`dest`), probed with a real engine, real approval flow and real lock store:

### B1. Lock coverage gap — the destination is mutated unlocked

```
declared roles            : [('source','filesystem:path'), ('dest','filesystem:path')]
ADR-061 canonical view    : [('filesystem:path','a.txt'), ('filesystem:path','archive/b.txt')]
engine._lock_canonical()  : ('filesystem:path', 'a.txt')        <-- ONE identity
step status               : SUCCEEDED      moved on disk? : True
write(dest) lock identity : ('filesystem:path', 'archive/b.txt') <-- disjoint
```

`_lock_canonical` (`engine.py:881`) reads `spec.resource_kind`/`spec.resource_param`,
which ADR-061 D9 mirrors to the **primary role only**. So a move locks `source` and
mutates `dest` with no lock at all. A concurrent `filesystem.write` to that same
destination shares no lock identity with the move: **no mutual exclusion exists**.
This is precisely the "approve A, lock B" divergence class D1 was written to
foreclose — alive in the lock layer.

Scope of the fix: `_lock_canonical` has **8 references** in `engine.py` (1 def +
7 call sites: 959, 1425, 3026, 3040, 3270, 3423, 3450), spanning acquisition,
release, post-wait revalidation, contention recheck and waiter-queue paths.
Multi-resource locking is a **lock-set** problem: all-or-nothing acquisition,
canonical ordering for deadlock avoidance (invariant 13 finally load-bearing),
and rollback of already-held locks on partial failure.

### B2. Approval fingerprint gap — one approval covers any destination

```
fingerprint(dest=archive/b.txt) : {... 'resource': 'a.txt', 'resource_fingerprint': '65b55ee7...'}
fingerprint(dest=secrets/leak)  : {... 'resource': 'a.txt', 'resource_fingerprint': '65b55ee7...'}
IDENTICAL?                      : True
```

`_authz_fingerprint_base` (`engine.py:4646`) hashes capability/action/scope/risk/
side_effects/`resource_kind` + **the primary resource only**. So an operator
approval granted for `move a.txt → archive/b.txt` satisfies `_fingerprint_matches`
for `move a.txt → secrets/leak.txt`. ADR-017/018's "stale approvals never
authorize" invariant is scoped to one resource.

C4 still does its job — out-of-boundary destinations are denied, naming the role:

```
policy(dest='../outside.txt') -> deny   resource '../outside.txt' outside boundary ... for role 'dest'
policy(dest='/etc/passwd')    -> deny   resource '/etc/passwd' outside boundary ... for role 'dest'
```

So exposure is confined to **in-boundary** destinations. But confining *where* a
mutation may go is the boundary's job; binding *which specific* mutation was
approved is the fingerprint's job, and today it doesn't.

There is a superficial workaround — `security_relevant_params=["dest"]` would pull
`dest` into the fingerprint, as `overwrite` does for `write`. It is the wrong fix:
`_authz_fingerprint_base` stores those params **raw**, while resources go through
`present_resource()` (bounded display + hash) specifically so the exact identifier
is **not** persisted (ADR-037). The fingerprint has two channels with different
privacy semantics, and a multi-resource action has **no correct channel** for its
second resource today. That is a genuine design decision for M9-B.3, not a detail.

### B3. Plan-validation gap — the model gets no plan-time signal

```
dest='b.txt'  validator=ACCEPTED   unresolved=[]        policy=require_approval
dest=''       validator=ACCEPTED   unresolved=['dest']  policy=deny
dest=42       validator=rejected   unresolved=['dest']  policy=deny
```

`PlanValidator._validate_resource` (`plan_validator.py:211`) validates only
`spec.resource_param` — the primary role. An empty `dest` passes plan validation
and is caught only later, at authorization. `unresolved_roles()`, the helper
ADR-061 built for exactly this, is unused.

M9-A consequence: a model emitting `dest: ""` receives a **DENY** (goal durably
blocked, operator intervention) instead of a typed `PlanCapabilityValidationError`
that would drive `RealModelPlanner`'s bounded retry loop with a corrective
diagnostic. The model path loses its self-correction for secondary resources.

### B4. Recovery record is single-resource

`engine.py:432` builds the recovery record from
`step.params.get(spec.resource_param)` — primary only — and
`MutationRecovery.resource` is a scalar `str | None`. For a failed non-retry-safe
move, the durable record would name `source` and be silent on `dest`. An operator
acknowledging that recovery (ADR-020/043) **cannot tell from the record whether the
file landed at the destination or vanished.** For a move, that is the whole
question.

### B5. Verification vocabulary is single-resource

`KNOWN_VERIFICATION_POLICIES = {non_empty, schema_keys, write_verified,
append_verified}`. The two real postcondition policies are scalar-by-construction:
`write_verified` compares `result["size"]` to `len(params["content"])`;
`append_verified` compares `prior_size + appended == size`.

A move's postcondition is inherently **two-resource** (source absent ∧ dest present
∧ content preserved). `schema_keys` would pass it on shape alone — and ADR-060 D5
explicitly refuses to let a mutation run under shape-only verification:
*"refusing to execute a mutation under `shape-only` verification (fail closed)"*.
So an honest `move` needs a new policy (`move_verified`) in both the `KNOWN` set
and `_verify` (`engine.py`). Note that M9-B.1 chose `schema_keys` for search —
legitimate for a read-only action, unavailable for a mutation.

---

## 4. Finding C — two definitions of "mutating" (latent trap)

```
registry.is_mutating(side_effects="irreversible")   : True
engine lock gate (side_effects == "mutating")       : False
ADR-060 D5 with no default_verification             : FAILS CLOSED (VerificationResolutionError)
```

`registry.is_mutating()` treats `irreversible` as mutating. The engine's execution
path uses a **literal comparison** in four places — `engine.py:365`, `3039`, `3422`,
`4725` (`mutating = getattr(spec, "side_effects", "read_only") == "mutating"`).

Measured end-to-end with `side_effects="irreversible"`:

```
capability invocations : 1
step status            : SUCCEEDED
dest exists / source gone : True / True
durable lock rows      : []
lock events            : NONE      <-- an irreversible mutation ran with zero locks
```

Today this is **unreachable** — no shipped capability declares `irreversible`
(all are `read_only` or `mutating`). But `move` is exactly the action whose honest
declaration *is* `irreversible` when it overwrites an existing destination
(`shutil.move` onto an existing file destroys the prior content). A well-intentioned
M9-B.3 that declares `irreversible` would silently opt out of mutation locking,
FIFO waiter queues, contention blockers and post-wait re-authorization — while
still being forced to carry a verification policy. Two authorities, one word,
different meanings.

This must be collapsed to a single predicate (`registry.is_mutating`) before any
capability declares `irreversible`.

---

## 5. Finding D — the vocabulary has outgrown the deterministic planner

`DeterministicPlanner` can emit exactly **5 of the 8 registered actions**:

| Reachable deterministically | Not reachable |
|---|---|
| `filesystem.read` (`read`, `list`) | `filesystem.write` |
| `git.log` (`log`, `branches`) | `filesystem.append` |
| `http.get` (`get`) | `filesystem.search` |

`_ACTION_TEMPLATES` holds only `read`/`list`; `planner_requirements()` returns only
`{http.get}`, `{git.log}` or `{filesystem.read}`.

So `filesystem.move` would be the **fourth** capability that no default planner can
produce. Any end-to-end proof of it must go through `RealModelPlanner` (fake
transport) or an explicitly injected/stored plan — which is why §2 matters so much:
**the only planner that can reach new capabilities is the one M9-B.1 broke.**

This is also the honest answer to *"what M9-A model planning can actually produce
against it"*: today, **nothing** — `capabilities_summary()` raises before the model
is ever consulted.

---

## 6. Answer: what M9-B.2 should prove

M9-B.1 proved: *a capability class can be added to the registry without touching
`authz.py`.* (True, and still worth having — but far narrower than claimed, and it
did not survive contact with the engine.)

**M9-B.2 should prove: the boundary enforces its own declaration contract, and
"registered" implies "governable".**

Three properties, each currently false and each measured above:

1. **Fail closed at registration.** A capability whose `actions` are not
   `ActionSpec`, or whose declared resource roles are unresolvable, is **refused by
   `CapabilityRegistry.register()`** with a typed error — the same discipline
   ADR-061 D2 already applies inside `ActionSpec.__post_init__`. Today the registry
   accepts anything and the failure surfaces three layers away, as an
   `AttributeError`, in one case escaping `run_goal()` unhandled.
2. **Registration implies transit.** Every registered capability can be driven
   plan → validate → authorize → execute → verify through the real engine. This is
   a *test class* that does not exist today, and it is the class that would have
   caught §2 with 8 tests already written.
3. **The default engine is self-consistent.** `build_engine()` produces a registry
   whose `capabilities_summary()` succeeds — i.e. the model catalog and the CLI
   surface are alive. A one-test bootstrap invariant that would have caught §2 at
   merge time.

Deliberately **not** what M9-B.2 should prove: that multi-resource mutation works.
That is §7, and it is a much larger slice. Conflating them repeats the M9-B.1
mistake — bundling a contract question with a capability question until neither is
answered.

---

## 7. Is `filesystem.move` the right next step?

**As a target: yes, unambiguously.** It is the only candidate in the M9-B matrix
that makes M8 load-bearing, and the probe shows exactly why: it is the capability
that turns invariant 13, `canonical_identities`, `unresolved_roles`, the
multi-role fingerprint and the recovery schema from *declared* into *used*.
Nothing else on the candidate list does that — `delete` is single-resource
(irreversible, but one role), `http.post` is single-resource, git mutations are
single-resource.

**As the next commit: no.** Three reasons, all measured:

- The baseline is red (§1). Landing a mutation capability on a broken registry
  means its own tests cannot distinguish "move is wrong" from "search is wrong".
- Move is not one capability, it is **six coupled changes**: capability + registry
  contract (§2), lock-set acquisition across 8 call sites (§3 B1), fingerprint over
  the full resource set with an ADR-037-compatible channel (§3 B2), plan validation
  of every role (§3 B3), recovery record shape (§3 B4), and a new two-resource
  verification policy (§3 B5) — plus collapsing the mutating predicate (§4) as a
  prerequisite.
- It cannot be exercised end-to-end at all until the model catalog is restored (§5).

---

## 8. Proposed slices

### M9-B.2 — Declaration contract + boundary transit *(small; restores green)*

| # | Change | Files | Risk |
|---|---|---|---|
| 1 | `CapabilityRegistry.register()` validates the `Capability` contract: `name`/`description` non-empty strings, `actions` a non-empty `list[ActionSpec]`; raise typed `CapabilityDeclarationError` (fail closed at construction, mirroring ADR-061 D2) | `capabilities/registry.py` | Low |
| 2 | Redeclare `filesystem.search` with `ActionSpec` (+ `ResourceRole`-equivalent sugar), preserving exact runtime behaviour: scope, boundary, `max_results ≤ 100`, `pattern ≤ 200`, sorted/deduped results, `schema_keys` verification | `capabilities/search.py` | Low |
| 3 | **New test class — boundary transit:** for every capability `build_engine()` registers, drive one step through the real engine (plan → validate → authorize → execute → verify) and assert a durable terminal status, never an exception escaping `run_goal()` | `tests/test_capability_boundary_transit.py` | Low |
| 4 | **Bootstrap invariant test:** `build_engine()` → `capabilities_summary()` succeeds; `action_spec()` resolves for every registered action; `arion capabilities` exits 0 | `tests/test_bootstrap_registry_invariant.py` | Low |
| 5 | Error-boundary check: a registry/declaration fault surfaces as a **typed, durable** task failure on both planner paths — never an unhandled exception out of `run_goal()` (§2 A3) | `orchestration/engine.py` (only if needed) | Medium |
| 6 | Close-out: correct `docs/m9b.1-completion.md`'s "Full suite: PASS" claim; ADR for the registration contract | docs | Low |

**Definition of Done:** full suite green (0 failed / 0 errors) at M9-B.2 HEAD;
`filesystem.search` executes through the engine and verifies; `arion capabilities`
works; `RealModelPlanner` receives a non-empty catalog from a default-built engine;
items 3 and 4 exist as permanent regression gates; a deliberately misdeclared
capability is refused at `register()` with a typed error.

Explicitly out of scope: any change to lock acquisition, fingerprints, recovery
schema, verification vocabulary, or the mutating predicate. Those are M9-B.3.

### M9-B.3 — `filesystem.move`: multi-resource mutation governance *(large)*

Prerequisite (must land first, independently testable):

- **P0.** Collapse the four literal `== "mutating"` comparisons in `engine.py`
  (365, 3039, 3422, 4725) onto `registry.is_mutating()`. Add a test that an
  `irreversible` action acquires a mutation lock (§4 currently proves it does not).

Then, in dependency order:

- **L1 — lock set.** `_lock_canonical` → canonical lock **set** from
  `canonical_identities()`; all-or-nothing acquisition in canonical order
  (invariant 13); release/rollback of held locks on partial failure; extend the
  post-wait re-authorization and contention-blocker paths. Refuse when
  `unresolved_roles()` is non-empty.
- **A1 — approval binds every role.** Fingerprint derived from the canonical view.
  Decide the ADR-037 channel question explicitly (per-role `present_resource`
  hashes vs. a role-set digest) — **not** raw `security_relevant_params`.
- **V1 — plan validation of every role.** `_validate_resource` iterates
  `spec.resources`; a missing/empty secondary role is a typed
  `PlanCapabilityValidationError`, so the model path gets a retry signal (§3 B3).
- **R1 — recovery names every role.** `MutationRecovery` carries the role set (or a
  bounded projection of it) so an operator acknowledging a failed move can tell
  where the file is (§3 B4).
- **V2 — two-resource verification.** `move_verified` added to
  `KNOWN_VERIFICATION_POLICIES` and `_verify`: source absent ∧ dest present ∧ size
  preserved, without a second mutation (ADR-060 discipline).
- **C1 — the capability.** Sandboxed `filesystem.move`, both roles
  `_resolve_inside`-checked, refuses to overwrite an existing destination unless an
  explicit security-relevant `overwrite` is set (write's precedent), honest
  `side_effects` declaration informed by P0.
- **T1 — concurrency proof.** Two-process test: `move a→b` vs `write b` must
  contend (today §3 B1 proves they do not); FIFO fairness across the lock set;
  crash mid-move leaves a durable, explainable recovery record naming both roles.

**Definition of Done:** every M8 derived view has a production consumer;
`canonical_identities`/`unresolved_roles` are load-bearing; ADR-061 invariant 13 is
enforced rather than declared; a move cannot race a write to its own destination
across processes; an approval for one destination does not authorize another; a
failed move is recoverable with both roles named; suite green.

---

## 9. Non-goals for both slices

- No change to `PermissionPolicy` decision semantics, scope resolution, or the
  capability/policy separation (ADR-009).
- No new authorization path — M9-B.1's actual achievement stands.
- No `delete`, `http.post`, git mutations, scheduled triggers, or shell.
- No model-output trust increase; no raw model output persisted.
- No dependency additions (stdlib only).
- No redesign of `Planner`/`ModelRouter`/engine loop.

---

## 10. Reproduction

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q --tb=no          # → 16 failed, exit 1

# A1: dict vs ActionSpec
.venv/bin/python -c "
import tempfile
from arion.capabilities.registry import CapabilityRegistry
from arion.capabilities.search import FilesystemSearchCapability
r = CapabilityRegistry(); r.register(FilesystemSearchCapability(tempfile.mkdtemp()))
print(type(r.get('filesystem.search').actions[0]).__name__)   # dict
r.capabilities_summary()"                                     # AttributeError

# A5: single-line causality — comment out bootstrap.py:81, then:
.venv/bin/python -m pytest tests/test_model_runtime_wiring.py \
    tests/test_model_reflection_wiring.py tests/test_cli.py tests/smoke -q
#   → 41 passed, 1 skipped
```

The §3/§4 measurements came from a throwaway harness (real `ArionEngine`, real
`SQLiteStorage`, real approval flow and lock store, synthetic two-role
`filesystem.move` capability + stub planner). It was deliberately kept **outside**
the repo tree; M9-B.3's T1 should promote the useful parts into `tests/`.

---

## 11. Open decisions for you

1. **Does M9-B.2 own the repair, or is the repair a separate hotfix commit ahead of
   it?** My recommendation: one slice — the registration contract *is* the
   architectural property, and the search fix is its first consumer. Splitting them
   gives you a green suite but no invariant preventing the next one.
2. **`filesystem.move` overwrite semantics** — refuse unless explicit `overwrite`
   (write's precedent, my recommendation), or refuse unconditionally and require a
   separate future `filesystem.replace`?
3. **Fingerprint channel for secondary roles (A1)** — per-role `present_resource`
   hashes, or one digest over the canonical role set? The first keeps per-role
   diagnostics; the second keeps the record bounded as roles grow.
4. **Is `irreversible` in scope for M9-B.3 at all**, or should `move` declare
   `mutating` + `reversible=False` (write's existing precedent) and `irreversible`
   be deferred until a capability genuinely needs it? P0 should land either way.
