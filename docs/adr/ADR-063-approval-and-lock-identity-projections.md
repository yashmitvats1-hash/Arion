# ADR-063 — Approval identity and lock identity are different projections of one declaration (M9-B.3 L1/A1)

- **Status:** **Proposed — awaiting review. Nothing in this ADR is implemented.** No source, test or schema change accompanies it. L1, A1, V1, R1, V2, C1 and T1 all remain unimplemented; P0 (`7d8f167`) is landed.
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)
- **Baseline:** `3be777e` (design note committed) on `arena/01a09aaa-arion`. Suite at `2036fc3`: **2055 collected, 2053 passed, 2 skipped, 0 failed, exit 0.**
- **Corrects:** ADR-061 D1, in two specific statements quoted verbatim below. **ADR-061 is not altered retroactively**; this ADR is the targeted clarification, so the historical record of what M8 decided and why stays intact.
- **Source of findings:** `docs/m9b.3-l1-a1-design-note.md`. Every property cited here was computed against the real `resolve_resources` / `canonical_identities` / `present_resource` / `canonical_resource` code, and the reproduction is inlined in that note's §12.
- **Related:** ADR-018 (durable approval queue), ADR-021 (locks are coordination, never permission), ADR-022/023 (FIFO waiter queue, atomic release-and-select-next), ADR-037 (resource presentation boundary), ADR-038 (approval decisions atomic with task state), ADR-044 (approval compatibility write fencing), ADR-060 D4/D5 (verification authority), ADR-061 D1/D2/D9 (resource-role declaration), ADR-062 (declaration contract at registration).

---

## Context

ADR-061 gave `ActionSpec.resources` two derived views and assigned each a purpose. No action in the shipped catalog declares more than one role, so **neither assignment has ever been exercised by production code**. `canonical_identities()` has no production consumer at all. M9-B.3's `filesystem.move` is the first action to declare two roles (`source`, `dest`), which makes both assignments load-bearing for the first time — and one of them is wrong.

The four cases a two-role action must distinguish:

| Case | `source` | `dest` | Meaning |
|---|---|---|---|
| `a→b` | `a.txt` | `archive/b.txt` | archive a file |
| `a→c` | `a.txt` | `secrets/c.txt` | same source, **different destination** |
| `b→a` | `archive/b.txt` | `a.txt` | **the inverse transformation** |
| `a→a` | `a.txt` | `a.txt` | degenerate: two roles, one resource |

Measured against `main`:

| Property | `a→b` vs `a→c` | `a→b` vs `b→a` | `a→b` vs `a→a` |
|---|---|---|---|
| Current approval fingerprint (`_authz_fingerprint`) | **IDENTICAL** | identical | differs |
| Canonical view (`canonical_identities`) | differs | **IDENTICAL** | differs (2 roles → 1 identity) |
| Role view (`resolve_resources`) | differs | differs | differs |

Two independent defects with **opposite** requirements:

1. **The approval fingerprint covers the primary role only.** `_authz_fingerprint` adds `present_resource(request.resource_kind, request.resource)` — the first-declared role — so a single approval for `move a.txt → archive/b.txt` also authorizes `move a.txt → secrets/c.txt`. Approval must be **direction- and destination-sensitive**.
2. **The canonical view is deliberately direction-blind.** `tests/test_resource_views.py::test_canonical_view_is_order_independent` asserts as intended behaviour that `a→b` and `b→a` produce the same canonical identity list. That is correct and necessary for locking — a global acquisition order is what makes a wait-for cycle impossible — and it is exactly why the canonical view **cannot** be the approval identity.

A second, compounding hazard: `present_resource`'s hash is `sha256(f"{kind}\0{exact}")` (`resource_identifiers.py:29`), which **does not include the role**. Measured: the sorted multiset of `resource_fingerprint` values for `a→b` and for `b→a` is **identical**. So any approval identity built from an *unordered set* of resource hashes re-introduces the direction collision even though it looks like it covers both resources.

### What ADR-061 D1 says, verbatim

> - **Role view** (`resolve_resources` in `resource_set.py`): ordered as declared, duplicates retained (invariant 4), values exactly as declared (invariant 20). Used for approval display, capability execution, recovery metadata.
> - **Canonical view** (`canonical_identities`): sorted set of `(kind, canonical_resource(kind, value))` pairs, deduplicated (invariant 3), identity is the pair never a bare string (invariant 2, rejected alternative R6). Used for fingerprinting, lock acquisition ordering (invariant 13).
>
> Both are derived from the same `ActionSpec.resources`; a caller can never approve one view and lock another (the "approve A, lock B" divergence class D1 exists to foreclose).

Quoted byte-exactly from ADR-061 so this correction can be diffed against it. The
three operative phrases are `Used for approval display` (role view bullet), `Used
for fingerprinting` (canonical view bullet), and `can never approve one view and
lock another` (closing sentence).

Two problems:

- Assigning **"fingerprinting"** to the canonical view is wrong for *approval* fingerprinting. The canonical view is sorted and deduplicated, so it cannot express direction — `a→b` and `b→a` are the same value. (It remains correct for *lock* identity, which is what invariant 13 actually needs.)
- **"Can never approve one view and lock another"** over-promises, and `b→a` is the counterexample: it needs the **same locks** as `a→b` (both files are touched; canonical ordering is what stops two such tasks deadlocking each other) but a **different approval** (it is the inverse transformation, and approving one must not authorize the other). Forbidding different projections is not what makes the architecture safe.

ADR-061's *Consequences* carries the same over-promise: "The derived-view architecture prevents approval/locking divergence (approve one resource, lock another)."

---

## Decision

### D1 — Two projections of one derivation, governed by a covering invariant

`ActionSpec.resources` remains the single authoritative declaration (ADR-061 invariant 1, unchanged). One `resolve_resources(spec, params)` call feeds two projections, each answering a different question:

| | **Approval / fingerprint view** | **Lock view** |
|---|---|---|
| Projection | ordered role view | sorted canonical view |
| Order | declaration order, significant | sorted, deterministic |
| Duplicates | retained (invariant 4) | deduplicated (invariant 3) |
| Values | **as declared** (invariant 20) | canonicalized |
| Question it answers | *did the operator approve **this transformation**?* | *which resources must be mutually excluded, and in what order do we take them?* |
| Direction-sensitive | **yes** — `a→b` ≠ `b→a` | **no** — both lock the same set |

**The safety invariant is about coverage, not about identical shape:**

> The set of resources covered by **approval** must be a superset of, or equivalent to, the set that execution **locks**. Approval and locking may legitimately hold **different projections of the same resolved resources**.

This is strictly stronger than ADR-061's "never approve one view and lock another", because it names the property that actually protects the system and makes it testable: the hazard D1 was written to foreclose is *approving A+B while locking only A*, not *asking two questions of one set*. Coverage can be asserted without forcing the two consumers to agree on ordering or deduplication — which they must not, since agreement would break one of them.

**Correction to ADR-061 D1, recorded here rather than applied retroactively:**

- Role view — "Used for approval display, capability execution, recovery metadata" becomes "Used for **approval identity and display**, capability execution, recovery metadata."
- Canonical view — "Used for fingerprinting, lock acquisition ordering" becomes "Used for **lock identity and acquisition ordering only**."
- The closing sentence is replaced by the covering invariant above.

**The correction scope is wider than ADR-061.** The same two claims are duplicated
in source docstrings, and leaving them would make the code contradict this ADR the
moment A1/L1 land. Both are quoted verbatim from
`arion/orchestration/resource_set.py` and must be corrected in the same change:

- the module docstring — `canonical view ... -> fingerprinting, lock acquisition
  ordering`, and `so a caller can never approve one resource while locking another
  (the "approve A, lock B" divergence class D1 exists to foreclose)`;
- the `ResolvedResource` docstring — `` `canonical` is the lock/fingerprint
  identity derived from it ``, which assigns fingerprinting to the canonical form
  at the level of the individual resolved slot.

That is four locations, not two: ADR-061's role-view bullet, its canonical-view
bullet, its closing sentence, and the two source docstrings that restate them.

### D2 — The approval fingerprint is the ordered per-role presentation

The fingerprint gains one key, `resources`, whose value is **exactly the shape `engine._resources_metadata()` already produces** (`engine.py:4578`, added by ADR-061 C3 and already durably persisted at `engine.py:4627`):

```jsonc
// _authz_fingerprint(request) for `filesystem.move` a→b
{
  "capability": "filesystem.move", "action": "move", "scope": "filesystem:write",
  "risk": "high", "side_effects": "irreversible",
  "resource_kind": "filesystem:path",           // PRIMARY role, unchanged (inv. 16)
  "security_relevant_params": {"overwrite": false},
  "resource": "a.txt",                          // PRIMARY display, unchanged
  "resource_fingerprint": "65b55ee7bd489ed9…",  // PRIMARY hash, unchanged
  "resource_redacted": false,
  "resources": [                                // ordered, role-tagged
    {"role": "source", "resource_kind": "filesystem:path",
     "resource": "a.txt",
     "resource_fingerprint": "65b55ee7bd489ed9…", "resource_redacted": false},
    {"role": "dest",   "resource_kind": "filesystem:path",
     "resource": "archive/b.txt",
     "resource_fingerprint": "4969110509c9afb6…", "resource_redacted": false}
  ]
}
```

**Canonicalization rules.**

1. Order is **declaration order**, never sorted, never deduplicated. Comparison is exact dict equality via the existing `_fingerprint_matches`, so list order is significant.
2. Duplicate roles are **retained** — `a→a` yields two entries with equal hashes (invariant 4).
3. Every value passes through `present_resource(kind, value)`: bounded one-line display, SHA-256 fingerprint, redaction flag (ADR-037 §1). The exact identifier is never persisted.
4. Values are **as declared**, never canonicalized (invariant 20). `./a.txt` and `a.txt` fingerprint differently — see D5.

**Measured outcome:** all four cases produce distinct fingerprints, including `a→b` vs `b→a` and `a→b` vs `a→a`.

This is a wiring change, not new machinery: the projection already exists, is already ADR-037-compliant, and is already durable. What changes is that it becomes the **authority** rather than only the display.

### D3 — Inclusion is conditional, and byte-identity is a tested invariant

`_fingerprint_matches` compares by **exact dict equality** against the shapes ADR-037 §3 and ADR-044 established. Adding a key unconditionally would change every single-resource fingerprint and stale every durable approval on upgrade.

So `resources` is included **only when `len(resolved) > 1`**. Single-resource fingerprints stay byte-identical; multi-resource actions have no pre-existing durable records because no shipped action declares two roles.

**That absence is a reason the migration is cheap today, not a reason the shape is safe.** The first shipped multi-resource action creates durable multi-resource approvals, and from then on any change to the projection is a real compatibility event. The behaviour is therefore pinned as invariants (34–36 below) with tests, so it cannot silently regress:

- a **golden-shape test** asserting the exact expected fingerprint dict for a single-role action, so any future key addition fails loudly;
- an assertion that a multi-role shape is never accepted for a single-role request, or vice versa.

Under D3, changing a declaration from one role to two changes the fingerprint shape and forces a fresh approval — correct, since the action's resource contract changed.

### D4 — The lock set: canonical identity, sorted acquisition, all-or-nothing

| Property | Decision |
|---|---|
| Identity | `canonical_identities(resolve_resources(spec, params))` — sorted `(kind, canonical_resource)` pairs. First production consumer of that helper |
| Acquisition order | sorted canonical order, always. A global total order over resources makes a wait-for cycle impossible, so concurrent `a→b` and `b→a` cannot deadlock |
| `a→a` | **one** lock, not two (invariant 3). Acquiring the same `(kind, resource)` twice would hit the store's UNIQUE constraint and self-deadlock |
| Cross-kind | identity is the `(kind, value)` pair, never a bare string (invariant 2 / rejected R6) |
| Acquisition | strictly in order; on failure at identity *k*, release the *k−1* held locks in **reverse** order and raise the existing typed `MutationLockError` / `MutationLockTimeoutError`. A partial lock set never survives the attempt |
| Release | reverse order on **every** terminal path, each via the atomic `release_and_select_next` (ADR-023) so FIFO handoff stays transactional |
| Waiter queue | gates on the **lead** (lowest canonical) identity only — one waiter row per step. Deadlock freedom comes from acquisition order, not from the queue; per-identity rows would multiply durable state and complicate the atomic handoff without buying correctness |
| Lease renewal | **all** locks renew; failure on any one means ownership of the whole step is lost and must fence it (ADR-039), not merely that lock |
| Contention blocker | names the **contended** identity, not the whole set — naming the set would wedge a goal on an unrelated resource |
| Dispatch gating | **all** identities of a step are added to the round's chosen set, and the step is gateable if **any** collides; otherwise two steps in one round can still race on a second resource |

Locks remain coordination and never permission (ADR-021): acquiring a lock set grants nothing.

#### D4.1 Where the lock set sits relative to authorization (measured)

`_execute_with_retries` documents the existing ordering at `engine.py:4739`:
*"authorization (live policy + approval) has already succeeded in `_execute_step`
BEFORE we reach this point. The advisory mutation lock is acquired NOW,
immediately before the actual mutation."* L1 must preserve that ordering, which
gives a property worth stating because it is easy to lose:

> **No lock set is ever held across an `AWAITING_APPROVAL` pause.** A step that
> needs a human decision has not yet acquired anything, so a slow operator cannot
> pin N resources.

The one path that can pause *after* acquiring is post-wait re-authorization
(`_revalidate_before_mutation`, `:1286`, called at `:4779`): if it queues a fresh
approval or denies, the lock is released (`:4782`) and the capability does not run.
For a set, that release must be the **whole set in reverse order** — it is one of
the "every terminal path" cases in invariant 38, and it is the case most likely to
be missed because it is a *pause*, not a failure.

#### D4.2 Renewal fencing (the D4 claim, grounded)

Two renewal mechanisms exist today:

1. **Heartbeat thread** — `_start_lock_heartbeat` (`:830`) starts one daemon
   thread per lock, named `arion-lock-heartbeat-{lock_id}`, renewing at
   `max(0.01, min(5.0, lease/3))`. On exception it records `state["error"]` and
   **stops renewing**.
2. **Synchronous final renewal** — immediately after `capability.execute` returns
   (`:4868`), before verification/success. Its `MutationLockError` handler
   (`:4869`) sets the step FAILED, emits `mutation.failed` **and**
   `mutation.requires_recovery`, calls `_record_recovery_required`, and returns
   without retrying. This is ADR-039 §2 and invariants 3–4, and it is the
   mechanism that actually fences.

**The execution path discards the heartbeat's error state.** At `:4797`
`_stop_lock_heartbeat(heartbeat)` is called without assignment, unlike the
goal-run lease path at `:757`, which assigns it and emits ownership-lost when
`state["error"]` is set. So fencing on the mutation path rests entirely on the
synchronous final renewal, not on the heartbeat. For a lock set that must remain
true, and it constrains the design:

- **One heartbeat thread renews the whole set**, in sorted canonical order — not
  N threads. A single thread means one failure stops renewal for every lock, which
  is exactly the semantics invariant 42 needs ("ownership of the whole step is
  lost"). N threads would permit the pathological state where one lock keeps being
  renewed after another's ownership is gone.
- **The final synchronous renewal renews every lock in the set**, in sorted
  canonical order, and failure on **any** one fences the **whole step**. Partial
  renewal never yields partial success: a step whose second lock's ownership was
  lost is fenced even though its first lock renewed cleanly, because the side
  effect may have happened and the ownership token that authorized it is no longer
  valid (ADR-039 invariant 3).
- Locks still held after a fencing failure are released in reverse order on the way
  out, per invariant 38.

#### D4.3 Waiter adoption is the real reason the queue gates on the lead identity

`enqueue_waiter(resource_kind, resource, task_id, goal_id, step_index, deadline,
now)` (`store.py:1806`) keys a waiter row on **one** `(resource_kind, resource)`
pair, and ADR-039 §3 makes it a transactional create-or-**adopt**: *"Existing
QUEUED membership for the same resource/task/step is adopted unchanged
(position/deadline preserved across a row-before-checkpoint crash)."*

With a lead-identity row, adoption across a restart works because the lead is a
**pure function of `(spec, params)`** — sorted canonical order, first element —
so a restarted engine recomputes the identical lead and matches the existing row.
Per-identity rows would require N adoptions and would admit partial-adoption
states (some rows adopted, some re-allocated at new positions), which is a fairness
bug class ADR-023 does not have today. This strengthens R5: lead-only is not
merely cheaper, it is the only option that keeps adoption deterministic.

**Requirement:** the contention blocker (`_set_lock_contention_blocker`, `:1422`)
must name the **same** lead identity the waiter row was keyed on, or a restarted
engine will look up a blocker it cannot match to its waiter row.

#### D4.4 L1 is coupled to R1 and must not land before it

`_record_recovery_required` (`:413`) populates `MutationRecovery.resource` — a
**scalar** field (`recovery.py:57`) — from `step.params.get(spec.resource_param)`,
the primary role only. So the fencing path in D4.2 writes a recovery record naming
**one** resource.

For a fenced `move a→b` whose side effect may have partially happened, that record
would name `a.txt` and never mention that `archive/b.txt` may now exist. The
operator's recovery authority would under-describe the world — which is the same
class of defect ADR-061 C4 closed for boundary checking, one layer down.

L1 is what makes that path reachable with more than one lock. Therefore **R1
(multi-role recovery) must land no later than L1**, and invariant 43 states the
property directly so the ordering does not depend on C1 happening to ship last.
This reorders the follow-ups below: the pre-pass sequence was
`V1 → A1 → L1 → R1`, which leaves a window where a multi-resource fence is
recorded with a single-resource recovery record.

### D5 — Approval is finer than locking, and that is the safe direction

`canonical_resource` normalizes spelling (`os.path.normpath` for `filesystem:path`): `./a.txt` → `a.txt`, `archive//b.txt` → `archive/b.txt`. Approval hashes the **as-declared** string while locking uses the **canonical** one, so the two views can disagree about a value in exactly two ways, and both are safe:

| Way the views differ | Effect | Safe because |
|---|---|---|
| Unresolved role omitted from the canonical view | would lock half of what is approved | refused outright before any lock or approval — D6 |
| Spelling normalized by the canonical view | approval distinguishes spellings the lock merges | approval is **finer** than locking |

Finer-approval + coarser-locking can only fail closed: a cosmetic respelling forces a fresh approval rather than silently reusing one, while the two spellings still take the same lock and so can never execute concurrently. The unsafe combination — coarser approval, finer locking — would let one approval authorize operations the mutex never serialized, and nothing in this design produces it.

Stated as an invariant (37), because "conservative" is only a property if it is enforced and tested. The second half is the load-bearing one, and it is precisely what an obvious future optimization ("canonicalize before hashing to cut duplicate prompts") would break — canonicalizing the approval material would make `./a.txt` and `a.txt` the same approval, and via `canonical_identities` would make `a→b` and `b→a` indistinguishable again.

**Mandated regression example:** `./a.txt` vs `a.txt` — different A1 fingerprints, same L1 canonical lock identity, and re-planning with the respelled path forces a fresh approval instead of reusing the stored one.

### D6 — An unresolved role refuses before any lock is acquired and before any approval is queued

The role view **retains** an unresolved role (value `None`) while the canonical view **omits** it. A step with a missing `dest` would therefore present to approval as `[source=a, dest=None]` but hand the lock layer only `{a}` — locking half of what the approval names. That is the "approve A+B, lock A" divergence D1 exists to foreclose, and it is the one case where the covering invariant can actually be violated.

**Corrected ordering.** The pre-pass draft said "A1 never sees it: authorization
already refused", which is muddled — A1 *is* part of authorization, and per D4.1
authorization runs **before** lock acquisition. The actual layering, in execution
order:

1. **V1** (`PlanValidator` iterating `spec.resources`) rejects at plan time, so a
   model gets a typed retryable error rather than a denial.
2. **Authorization — which is where A1's fingerprint is computed — denies.**
   `_one_resource_allowed` fails closed on a missing resource and names the role
   (ADR-061 C4/D3), so the decision is DENY, not "approval required". **No
   `ApprovalRequest` is ever queued** for an unresolved role: the durable queue
   never receives it, and no operator is ever asked to approve a half-specified
   transformation.
3. **L1 is never reached**, because the lock is acquired only after authorization
   succeeds (`engine.py:4739`). The `unresolved_roles()` check there is
   defence-in-depth (ADR-061 invariants 5, 6 — "unresolved must REFUSE, never be
   treated as nothing to check"), not the primary gate. It is still
   `unresolved_roles()`' first production consumer.

**Consequence for the denied record, stated so it is a decision and not an
accident.** `_append_approval_record` computes a fingerprint for DENIED outcomes
too. Since `resolve_resources` **retains** the unresolved role with `value=None`,
and `present_resource(kind, None)` returns
`{resource: None, resource_fingerprint: None, resource_redacted: False}` (verified:
it does not raise), a denied record's `resources` list **may contain a
null-valued entry**. That is forensic only and never an authority — authorization
already refused — but no reader may mistake it for a resource that was approved.
Invariant 40 covers the refusal; this note covers the residue.

**Why the D3 predicate is stable.** `resolve_resources` appends one entry per
declared role unconditionally, so `len(resolved) == len(spec.resources)` **always**
— including when a role is unresolved. The predicate `len(resolved) > 1` is
therefore equivalent to a declaration-based one, which means the fingerprint's
*shape* depends only on the `ActionSpec` declaration and never on whether a
particular step's params happened to resolve. Shape stability is what invariant 34
requires; had `resolve_resources` filtered unresolved roles, the shape would have
varied with runtime data.

### D7 — The degenerate self-move is refused at the capability

`a→a` (`source == dest`) is refused by `filesystem.move`. A move onto itself is either a no-op or a destruction, and neither is worth an approval prompt.

**This does not weaken ADR-061 invariant 4.** The refusal sits **above** the view layer and changes neither projection: `resolve_resources` still returns **two** entries for `a→a`, and `canonical_identities` still dedups to **one** identity. Invariant 4 is explicitly illustrated in ADR-061 by `move a -> a` and must keep holding. An implementation that "fixes" `a→a` by altering either view has broken invariant 4.

### D8 — A resource role may not also be a security-relevant param

`ActionSpec` must refuse, at construction, any declaration that names a resource role in `security_relevant_params`. Today nothing forbids it. Doing so encodes the same value through two channels with different privacy semantics — hashed and bounded as a resource (ADR-037), **raw** as a security-relevant param (which is how `overwrite` is stored). For any non-boolean role that is a direct ADR-037 violation, and it is the same mutually-exclusive-spellings hazard ADR-061 D2/D9 already refuses for `resource_kind`/`resources`.

Enforced in `ActionSpec._validate_resources` (`registry.py:167`) raising `ResourceDeclarationError`, alongside the existing D2 construction checks. This is a construction-time rule, so it cannot be reached at runtime by any admitted action (ADR-062 invariant 24).

---

## Invariants

Numbering continues from ADR-062 (which ends at 29).

| # | Statement | Source (planned) |
|---|---|---|
| 30 | Approval identity is the **ordered role view**; lock identity is the **sorted canonical view**; both derive from one `resolve_resources` call and neither is independently authoritative | D1 |
| 31 | **Covering invariant, stated operationally so it is testable:** the lock set equals the canonicalization of the role view's *resolved* values — `set(canonical_identities(resolved)) == {(r.kind, r.canonical) for r in resolved if r.resolved}`. Given invariant 40 refuses the unresolved case, approval therefore covers exactly what execution locks. Different *projections* of one resolved set are legitimate; different *coverage* is not | D1 |
| 32 | The canonical view is **never** an approval identity — it is sorted and deduplicated, so it cannot express direction | D1, D2 |
| 33 | Approval-identity material **must carry role identity and declaration order**. Any fingerprint whose compared material is an unordered collection of resource hashes is non-conforming however it is spelled, because `present_resource`'s hash is `sha256(kind\0exact)` and is therefore role-blind | D2 |
| 34 | A resolved role view of length ≤ 1 produces a fingerprint containing **no `resources` key** and no change to any existing key or value — stated structurally, not historically, so it stays true long after the migration. Asserted for a given registry state: `security_relevant_params` is read live from the registry inside a `try/except` that yields `{}` on any failure (`_authz_fingerprint_base`), a pre-existing sensitivity this ADR neither introduces nor removes | D3 |
| 35 | For length ≥ 2, `resources` is present in **declaration order**, and the singular primary-role keys still mirror `resources[0]` (invariant 16 preserved, not replaced) | D3 |
| 36 | `_fingerprint_matches` never accepts a multi-role shape for a single-role request, nor a single-role shape for a multi-role request | D3 |
| 37 | Approval identity **may distinguish** two spellings that lock identity treats as equivalent; it **must never collapse** two identities that locking treats as distinct | D5 |
| 38 | The lock set is acquired in sorted canonical order, **all-or-nothing**, and released in reverse order on every terminal path — **including a post-wait re-authorization pause**, which is a terminal path for the held set even though the step merely waits. A partial lock set never survives a failed attempt or a pause | D4, D4.1 |
| 39 | `a→a` takes exactly **one** lock while the role view retains **two** entries — invariant 4 and invariant 3 both hold, and the self-move is refused above the view layer | D4, D7 |
| 40 | An unresolved role **refuses** before any lock is acquired and before any approval is queued; it is never treated as "nothing to check" | D6 |
| 41 | A resource role may not also be named in `security_relevant_params`; the declaration is refused at construction | D8 |
| 42 | **One** heartbeat renews the whole lock set in sorted canonical order, and the synchronous post-execution renewal renews **every** lock in it. Failure on any one fences the **whole step**: partial renewal never yields partial success, because the side effect may have happened and the ownership token that authorized it is no longer valid (ADR-039 invariants 3–4) | D4.2 |
| 43 | A fenced step's recovery record names **every** resource in the lock set it held, never only the primary role. Consequently L1 must not be reachable with more than one lock before R1 lands | D4.4 |
| 44 | The waiter row and the contention blocker are both keyed on the **lead** canonical identity, which is a pure function of `(spec, params)`; a restarted engine therefore recomputes the identical key and ADR-039 §3 create-or-adopt still matches, with no partial-adoption state | D4.3 |

---

## Consequences

- `move a→b` and `move a→c` require **separate approvals**. Today one approval for a source authorizes any in-boundary destination of that source; after A1 it does not.
- `move a→b` and `move b→a` require separate approvals but contend on the **same** lock set, and cannot deadlock.
- Every existing durable approval survives unchanged (invariant 34). No migration, no fencing, no re-prompting on upgrade.
- `canonical_identities()` and `unresolved_roles()` gain their first production consumers; ADR-061's derived-view architecture stops being speculative.
- Approval authority becomes **stricter than what the operator is shown**: `arion approvals show` still prints only the primary role (see *Deferred*). This is the safe direction — an approval can never authorize more than the operator saw — but it is a real gap and the strongest argument for pulling the deferred work into B.3 scope.
- Post-wait re-authorization (`_post_lock_revalidate`, `engine.py:1287`, which rebuilds via `_build_authz_request` at `:1362` from the live spec) now compares the full ordered role view, so a role-declaration change while a step waited for a lock correctly stales the approval. New property; needs an explicit test.
- ADR-061 D1's text is superseded for the two statements quoted in *Context*. ADR-061 itself is not edited; a reader following the chain finds the correction here.

## Deferred — explicitly not part of L1/A1

The human approval surface does not show the second role today, and **by ruling it stays that way through L1/A1**:

| Surface | Today | Deferred work |
|---|---|---|
| `ApprovalRequest` (`state/approvals.py`) | scalar `resource_kind` / `resource` | additive `resources` field; `from_dict` defaults to `[]` so legacy rows rehydrate |
| `_queue_request_from_auth` summary | `"{cap}/{action} on {primary_display}"` | render both roles from the role view, bounded to 300 |
| `arion approvals show` (`cli.py:1139`) | prints `req.resource` only | one line per role |
| `GET /approvals` (`approval_api.py:148/162`) | `ApprovalRequest.to_dict()` | gains the field automatically |
| `_mirror_from_request` (`engine.py:2677`) | scalar only — **already inconsistent** with `_append_approval_record`, which projects the full ordered view | carry `resources` so both paths build the record at equal fidelity |

The last row is a **pre-existing bug independent of move**: two code paths build the same task-level approval record at different fidelity. Deferring does not worsen it, and A1 touches neither path's display projection. Whether these are B.3 scope is decided after the core approval model is locked; L1/A1 must not quietly expand to cover them.

## Rejected alternatives

- **R1 — Role-set digest** (one opaque `resource_set_fingerprint` + size + joined display string). Correct *only* if its material is the ordered role view; the natural implementation reaches for `canonical_identities()` — the helper whose name means "canonical identity" — and silently re-introduces the direction collision (measured). It also needs a per-role display string for humans anyway, so it rebuilds D2's projection and then discards its structure, losing per-role mismatch diagnostics. Constant-size digest is its only advantage and does not bind at two roles.
- **R2 — One view for both consumers.** Canonical for both re-introduces the direction collision; role order for both permits two tasks to acquire in opposite orders and deadlock.
- **R3 — Unconditional `resources` key + a third accepted fingerprint shape** (ADR-037's reshape-and-fence precedent). Rejected: no durable multi-role approval exists to fence, so it would invalidate every pending write/append approval on upgrade for no compatibility benefit.
- **R4 — Canonicalize before hashing** to reduce duplicate approval prompts. Directly violates invariant 37's second half and, via `canonical_identities`, invariant 32.
- **R5 — A waiter row per lock identity.** Multiplies durable state and complicates the atomic `release_and_select_next` handoff without buying correctness, which comes from acquisition order.
- **R6 — Editing ADR-061 in place.** Rejected by ruling: the correction is recorded here so the historical record of what M8 decided, and on what evidence, stays intact.
- **R7 — Fixing the deferred human-surface issues opportunistically inside L1/A1.** Rejected by ruling; recorded above as separate B.3 concerns.

## Follow-ups (owned by implementation, not this ADR)

Sequencing matters, and follows from D1/D6/D7:

1. **V1** — `PlanValidator` iterates `spec.resources`; plan-time typed refusal (D6). Gives the model path a signal before locking.
2. **A1** — fingerprint per D2/D3, plus the D8 construction refusal (D8 is construction-time and lands here).
3. **R1** — `MutationRecovery` names every role. **Moved ahead of L1** by
   invariant 43: L1 is what makes a multi-resource fence reachable, and the fencing
   path writes a recovery record that is scalar today (`recovery.py:57`, populated
   from `spec.resource_param`). The pre-pass order was `L1 → R1`, which leaves a
   window where a fenced `move a→b` records recovery naming only `a.txt`. Closing
   that window structurally is preferable to relying on C1 shipping last — the same
   discipline the Q2 ruling applied to D3.
4. **L1** — lock set per D4/D6. Must follow A1 (so the capability can never
   execute under an approval that does not bind its destination) and R1 (invariant
   43).
5. **V2** — `move_verified` two-resource verification policy.
6. **C1** — `filesystem.move`, inheriting D7's self-move refusal; declares
   `irreversible` when it can overwrite, which P0 made safe to declare.
7. **T1** — concurrency proof: `a→b` vs `b→a` contend and neither deadlocks;
   `a→c` is not authorized by an `a→b` approval.

The concrete L1/A1 data-flow is the next required artifact, by ruling, after this ADR is reviewed.

## Contract pass (2026-09-14)

A final invariant/contract pass over the four areas flagged for review. **No
finding invalidates the direction**; all are refinements, plus one sequencing
change. Every claim below was re-checked against source, not against the draft's
own assertions.

### D4 renewal fencing — three issues found

1. **The fencing mechanism is not the heartbeat.** ADR-039 §2's fence is the
   *synchronous post-execution renewal* (`:4868`, handler at `:4869`: FAILED,
   `mutation.failed`, `mutation.requires_recovery`, `_record_recovery_required`,
   no retry). The execution path **discards** the heartbeat's error state at
   `:4797` — contrast the goal-run lease path at `:757`, which assigns it and
   emits ownership-lost. The pre-pass text said "failure on any one must fence the
   step" without naming the mechanism, the renewal order, or partial-renewal
   semantics. Added **D4.2** (one heartbeat for the whole set; renew in sorted
   canonical order; partial renewal never yields partial success) and
   **invariant 42**.
2. **The fencing path writes a scalar recovery record.** `_record_recovery_required`
   (`:413`) populates `MutationRecovery.resource` — scalar (`recovery.py:57`) —
   from `spec.resource_param`, the primary role only. So a fenced `move a→b` would
   record recovery naming `a.txt` and never mention that `archive/b.txt` may exist.
   L1 makes that reachable with >1 lock. Added **D4.4**, **invariant 43**, and
   **reordered R1 ahead of L1** — closing the window structurally rather than
   relying on C1 shipping last, the same discipline the Q2 ruling applied to D3.
3. **The lead-only waiter ruling was under-justified.** `enqueue_waiter`
   (`store.py:1806`) is a transactional create-or-**adopt** keyed on
   `(resource, task, step)` (ADR-039 §3), so the lead must be recomputable
   identically after a restart. It is — a pure function of `(spec, params)` — but
   that is the actual reason lead-only is safe, and per-identity rows would admit
   partial-adoption states. Added **D4.3**, **invariant 44**, and the requirement
   that `_set_lock_contention_blocker` (`:1422`) name the *same* lead the waiter row
   used.

### D6 ordering — one issue found

The pre-pass text said "**A1** never sees it: authorization already refused". That
is wrong: A1 *is* part of authorization, and authorization runs **before** lock
acquisition (`:4739`). Corrected to the real three-layer ordering — V1 at plan
time, authorization **denies and names the role** so **no `ApprovalRequest` is ever
queued**, L1 never reached. Two consequences added: the DENIED task record may carry
a null-valued role entry (`present_resource(kind, None)` verified to return
`{resource: None, resource_fingerprint: None, resource_redacted: False}` without
raising) and is forensic only; and `len(resolved) == len(spec.resources)` **always**,
since `resolve_resources` appends one entry per declared role unconditionally — so
D3's predicate makes the fingerprint *shape* depend on the declaration, never on
whether a step's params resolved.

### D3 byte-compatibility — verified, one scoping refinement

`_authz_fingerprint` is `_authz_fingerprint_base(...)` plus
`present_resource(kind, resource).metadata()`, compared by exact dict equality in
`_fingerprint_matches`. A conditionally-added key leaves the ≤ 1-role shape
untouched, so the claim holds. Refinement: `security_relevant_params` is read
**live from the registry** inside a `try/except` that yields `{}` on any exception,
so byte-identity holds only *for a given registry state* — a pre-existing
sensitivity this ADR neither introduces nor removes, now stated rather than implied.
Invariant 34 was also restated **structurally** ("contains no `resources` key")
instead of **historically** ("identical to pre-M9-B.3"), so it remains a live
property after the migration is forgotten.

### ADR-061 correction wording — two issues found

1. **Quote fidelity.** Three passages were presented as verbatim ADR-061 text but
   carried `**emphasis**` that the original does not contain. In a *correction* ADR
   the quotes must be diffable, so they are now byte-exact and the operative phrases
   are identified outside the quotation.
2. **Correction scope was incomplete.** The same two claims are restated in source:
   `resource_set.py`'s module docstring (`canonical view ... -> fingerprinting, lock
   acquisition ordering`; `so a caller can never approve one resource while locking
   another`) and `ResolvedResource`'s docstring (`` `canonical` is the lock/fingerprint
   identity derived from it ``). Left uncorrected, the code would contradict this ADR
   the moment A1/L1 land. Scope widened from **two statements to four locations**.

### Invariants 30–41 — durability review

Each was tested against "durable architectural property vs. accidental
implementation detail":

- **31** was a vague "superset of / equivalent to" relation → restated as a **set
  equality** that is directly assertable.
- **33** was phrased as an *observation* about a hash → restated as a **normative
  requirement** on approval-identity material, so it still binds if the hash
  construction changes.
- **34** was anchored to a historical baseline → restated **structurally**, and
  scoped to registry state.
- **38** did not name the post-wait *pause* as a terminal path for the held set →
  now does; that path is the one most likely to be missed because the step does not
  fail, it waits.
- **30, 32, 35, 36, 37, 39, 40, 41** are durable as written: each states a relation
  between the declaration, the two projections and the consumers, and none names a
  data structure, a field count or a call site as the property itself.
- **42, 43, 44** added by this pass.

---

## Tests

None written — this ADR is design only. The tests it mandates:

- **Invariants 34–36:** golden-shape fingerprint test for a single-role action; declaration-order and primary-mirroring test for a two-role action; cross-shape non-acceptance test for `_fingerprint_matches`.
- **Invariant 37:** the `./a.txt` vs `a.txt` regression — different fingerprints, same canonical lock, respelling forces re-approval.
- **Invariants 32/33:** `a→b`, `a→c`, `b→a`, `a→a` produce four distinct approval identities; an unordered hash multiset does not (documenting *why* D2 is ordered).
- **Invariant 39:** `a→a` yields two role-view entries, one canonical identity, one lock.
- **Invariant 40 (corrected by the pass):** an unresolved `dest` is rejected at
  plan time (V1); authorization **denies and names the role**, so **no
  `ApprovalRequest` is queued**; and L1 is never reached. Assert the denial and the
  absence of a queued request — not "A1 never sees it", which the measured
  ordering contradicts. Also assert the denied task record's `resources` may carry a
  null-valued entry and that nothing treats it as an approved resource.
- **Invariant 31:** the set-equality form — `set(canonical_identities(resolved))`
  equals the canonicalization of the resolved role values, for `a→b`, `b→a` and
  `a→a`.
- **Invariant 41:** `ActionSpec` construction raises `ResourceDeclarationError`
  when a role is also a `security_relevant_param`.
- **Invariant 42:** exactly **one** heartbeat thread exists for a 3-lock set;
  renewal failure on the **second** lock fences the whole step (FAILED,
  `mutation.failed`, `mutation.requires_recovery`, recovery recorded, no retry) even
  though the first lock renewed cleanly.
- **Invariant 43:** a fenced multi-resource step records recovery naming **every**
  role, not only the primary.
- **Invariant 44:** after a simulated restart, the recomputed lead identity matches
  the persisted waiter row and `enqueue_waiter` **adopts** it (position and deadline
  preserved) rather than allocating a new one; the contention blocker names the same
  lead.
- **D4:** all-or-nothing acquisition releases held locks in reverse on mid-set
  failure; dispatch gating blocks on **any** colliding identity.
- **D4.1:** no lock is held across an `AWAITING_APPROVAL` pause; and a post-wait
  re-authorization that queues a fresh approval releases the **whole set** in
  reverse order.
- **Post-wait property:** a role-declaration change while waiting stales the
  approval.
