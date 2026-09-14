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

> - **Role view** (`resolve_resources` in `resource_set.py`): ordered as declared, duplicates retained (invariant 4), values exactly as declared (invariant 20). Used for **approval display**, capability execution, recovery metadata.
> - **Canonical view** (`canonical_identities`): sorted set of `(kind, canonical_resource(kind, value))` pairs, deduplicated (invariant 3), identity is the pair never a bare string (invariant 2, rejected alternative R6). Used for **fingerprinting**, lock acquisition ordering (invariant 13).
>
> Both are derived from the same `ActionSpec.resources`; a caller can never **approve one view and lock another** (the "approve A, lock B" divergence class D1 exists to foreclose).

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

Layered refusal:

- **V1** (`PlanValidator` iterating `spec.resources`) rejects at plan time, so a model gets a typed retryable error;
- **L1** refuses to acquire when `unresolved_roles()` is non-empty (ADR-061 invariants 5, 6 — "unresolved must REFUSE, never be treated as nothing to check"). This is `unresolved_roles()`' first production consumer;
- **A1** never sees it: authorization already refused, because `_one_resource_allowed` fails closed on a missing resource and names the role (ADR-061 C4).

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
| 31 | The resource set covered by approval is a **superset of / equivalent to** the set execution locks. Different projections of the same resolved resources are legitimate; different *coverage* is not | D1 |
| 32 | The canonical view is **never** an approval identity — it is sorted and deduplicated, so it cannot express direction | D1, D2 |
| 33 | An **unordered set** of `resource_fingerprint` values is never an approval identity — `present_resource`'s hash is role-blind, so such a set collides across direction | D2 |
| 34 | For a resolved role view of length ≤ 1, `_authz_fingerprint` is **byte-identical** to the pre-M9-B.3 fingerprint: no `resources` key, no reordering, no changed value | D3 |
| 35 | For length ≥ 2, `resources` is present in **declaration order**, and the singular primary-role keys still mirror `resources[0]` (invariant 16 preserved, not replaced) | D3 |
| 36 | `_fingerprint_matches` never accepts a multi-role shape for a single-role request, nor a single-role shape for a multi-role request | D3 |
| 37 | Approval identity **may distinguish** two spellings that lock identity treats as equivalent; it **must never collapse** two identities that locking treats as distinct | D5 |
| 38 | The lock set is acquired in sorted canonical order, **all-or-nothing**, and released in reverse order on every terminal path; a partial lock set never survives a failed attempt | D4 |
| 39 | `a→a` takes exactly **one** lock while the role view retains **two** entries — invariant 4 and invariant 3 both hold, and the self-move is refused above the view layer | D4, D7 |
| 40 | An unresolved role **refuses** before any lock is acquired and before any approval is queued; it is never treated as "nothing to check" | D6 |
| 41 | A resource role may not also be named in `security_relevant_params`; the declaration is refused at construction | D8 |

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
3. **L1** — lock set per D4/D6. Must follow A1 so the capability can never execute under an approval that does not bind its destination.
4. **R1** — `MutationRecovery` names every role.
5. **V2** — `move_verified` two-resource verification policy.
6. **C1** — `filesystem.move`, inheriting D7's self-move refusal; declares `irreversible` when it can overwrite, which P0 made safe to declare.
7. **T1** — concurrency proof: `a→b` vs `b→a` contend and neither deadlocks; `a→c` is not authorized by an `a→b` approval.

The concrete L1/A1 data-flow is the next required artifact, by ruling, after this ADR is reviewed.

## Tests

None written — this ADR is design only. The tests it mandates:

- **Invariants 34–36:** golden-shape fingerprint test for a single-role action; declaration-order and primary-mirroring test for a two-role action; cross-shape non-acceptance test for `_fingerprint_matches`.
- **Invariant 37:** the `./a.txt` vs `a.txt` regression — different fingerprints, same canonical lock, respelling forces re-approval.
- **Invariants 32/33:** `a→b`, `a→c`, `b→a`, `a→a` produce four distinct approval identities; an unordered hash multiset does not (documenting *why* D2 is ordered).
- **Invariant 39:** `a→a` yields two role-view entries, one canonical identity, one lock.
- **Invariant 40:** an unresolved `dest` refuses at plan time (V1), refuses to acquire (L1), and never reaches A1.
- **Invariant 41:** `ActionSpec` construction raises `ResourceDeclarationError` when a role is also a `security_relevant_param`.
- **D4:** all-or-nothing acquisition releases held locks in reverse on mid-set failure; dispatch gating blocks on any colliding identity; renewal failure on any lock fences the step.
- **Post-wait property:** a role-declaration change while waiting stales the approval.
