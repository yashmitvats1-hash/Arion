# M9-B.3 Design Note — L1 (lock set) and A1 (approval identity)

**Status:** DESIGN ONLY. No source, test or schema change accompanies this note.
Nothing here is implemented.
**Ruled 2026-09-14:** **Q1–Q6 and Q8 are LOCKED** (§10 records each ruling).
**Q7 is reclassified → deferred:** §7's scalar-approval and CLI findings are
separate B.3 concerns, explicitly not to be fixed opportunistically inside L1/A1.
No question remains open. Next artifact by ruling: **ADR-063 first, then the
concrete L1/A1 data-flow**, both reviewed before any L1 implementation.
**Branch:** `arena/01a09aaa-arion` @ `2036fc3` (M9-B.2 + P0 landed).
**Scope:** the two decisions that constrain everything else in M9-B.3, because
`filesystem.move` cannot be built until they are fixed — L1 determines what a
multi-resource mutation *locks*, A1 determines what an operator *approves*.
**Evidence:** every collision and shape claim below was computed against the real
`resolve_resources` / `canonical_identities` / `present_resource` /
`canonical_resource` code, not assumed. Reproduction script is inlined in §12.
**Next artifacts before any L1 implementation:** the final ADR-063 wording, and the
concrete L1/A1 data-flow.

---

## 1. The problem in one table

For a two-role action (`source`, `dest`), the four cases that must be
distinguished:

| Case | `source` | `dest` | Meaning |
|---|---|---|---|
| `a→b` | `a.txt` | `archive/b.txt` | archive a file |
| `a→c` | `a.txt` | `secrets/c.txt` | **same source, different destination** |
| `b→a` | `archive/b.txt` | `a.txt` | **the inverse transformation** |
| `a→a` | `a.txt` | `a.txt` | degenerate: two roles, one resource |

Measured against `main`:

| Property | `a→b` vs `a→c` | `a→b` vs `b→a` | `a→b` vs `a→a` |
|---|---|---|---|
| **Current approval fingerprint** (`_authz_fingerprint`) | **IDENTICAL** ✗ | identical (both are the inverse's primary) ✗ | differs |
| Canonical view (`canonical_identities`) | differs ✓ | **IDENTICAL** (by design) | differs (2 roles → 1 identity) |
| Role view (`resolve_resources`) | differs ✓ | differs ✓ | differs ✓ |

Two independent defects, with opposite requirements:

- **A1's defect:** the fingerprint covers the primary role only, so one approval
  authorizes *any* in-boundary destination of the same source. Approval must be
  **direction-sensitive**.
- **L1's requirement:** the canonical view is *deliberately* order-independent —
  `tests/test_resource_views.py::test_canonical_view_is_order_independent`
  asserts `a→b` and `b→a` produce the same canonical identity list. That is
  correct and necessary for locking (a global acquisition order is what prevents
  deadlock), and it is exactly why **the canonical view must never be the
  approval identity.**

> **The core design decision: one declaration, two views, two questions.**
> A1 asks *"did the operator approve **this transformation**?"* → **ordered role
> view**. L1 asks *"which resources must be mutually excluded, and in what order
> do we take them?"* → **sorted canonical view**. Both derive from the same
> `ActionSpec.resources` via `resolve_resources`, so they can never disagree
> about *which* resources are in play — only about what they ask of them. That is
> ADR-061 D1's actual promise, and M9-B.3 is the first increment where both
> derived views become load-bearing.

**LOCKED (Q1/Q4 ruling).** The distinction becomes explicit architecture, stated
as three parts:

- **Approval / fingerprint view:** ordered, role-sensitive — because `a→b` ≠ `b→a`.
- **Lock view:** canonical, order-independent — because both operations must lock
  the same resource set.
- **Safety invariant:** the set of resources covered by **approval** must be a
  superset of / equivalent to the set that execution **locks**. Approval and
  locking may legitimately hold **different projections of the same resolved
  resources**.

This is deliberately stronger than pretending one canonical representation serves
both purposes: the invariant is about *coverage*, not about *identical shape*, so
it can be tested without forcing the two consumers to agree on ordering or
deduplication. It supersedes ADR-061 D1's line 28 as literally written — see §9
and **Q4**, to be recorded as a targeted **ADR-063** clarification, **not** as a
retroactive edit to ADR-061.

---

## 2. A second, role-blindness hazard

`present_resource()`'s hash is `sha256(f"{kind}\0{exact}")` — it does **not**
include the role. Measured: the sorted multiset of `resource_fingerprint` values
for `a→b` and `b→a` is **identical**.

So a fingerprint built from an *unordered set* of per-role hashes collides across
direction. Any A1 design must therefore carry role identity **and** order in the
compared material, not merely the set of resource hashes. This constrains both
options below.

---

## 3. A1 — Option A: per-role presentation

### Shape

The fingerprint gains one key, `resources`, whose value is **exactly the shape
`engine._resources_metadata()` already produces** (`engine.py:4578`, added by
ADR-061 C3):

```jsonc
// _authz_fingerprint(request) for `filesystem.move` a→b
{
  "capability": "filesystem.move",
  "action": "move",
  "scope": "filesystem:write",
  "risk": "high",
  "side_effects": "irreversible",
  "resource_kind": "filesystem:path",          // PRIMARY role, unchanged (inv. 16)
  "security_relevant_params": {"overwrite": false},
  "resource": "a.txt",                          // PRIMARY display, unchanged
  "resource_fingerprint": "65b55ee7bd489ed9…",  // PRIMARY hash, unchanged
  "resource_redacted": false,
  "resources": [                                // NEW — ordered, role-tagged
    {"role": "source", "resource_kind": "filesystem:path",
     "resource": "a.txt",
     "resource_fingerprint": "65b55ee7bd489ed9…", "resource_redacted": false},
    {"role": "dest",   "resource_kind": "filesystem:path",
     "resource": "archive/b.txt",
     "resource_fingerprint": "4969110509c9afb6…", "resource_redacted": false}
  ]
}
```

Hashes above are real `sha256` values from `present_resource`, truncated for
readability.

### Canonicalization rules

1. Order = **declaration order** of `ActionSpec.resources` (invariant 4/20), not
   sorted, not deduplicated.
2. Duplicate roles are **retained**: `a→a` produces two entries with equal
   hashes. That is not a bug — it is the declaration saying "this action reads one
   resource and destroys another that happens to be the same file".
3. Every value passes through `present_resource(kind, value)` — bounded display +
   hash + `redacted` (invariant 7). The exact string is never persisted.
4. Values are the **as-declared** strings, never canonicalized (invariant 20):
   `./a.txt` and `a.txt` fingerprint differently. This is intentional and matches
   the existing single-resource behaviour — canonicalization is the *lock* layer's
   job, and ADR-061 invariant 7 forbids canonicalization altering what a human is
   shown.
5. Comparison is exact dict equality on the whole fingerprint (the existing
   `_fingerprint_matches` mechanism), so list order is significant.

### Collision / ordering properties (measured)

| | `a→b` vs `a→c` | `a→b` vs `b→a` | `a→b` vs `a→a` |
|---|---|---|---|
| Option A fingerprint | **distinct** ✓ | **distinct** ✓ | **distinct** ✓ |

### ADR-037 compliance

Fully compliant, and by reuse rather than by new construction: `_resources_metadata`
is already the ADR-037-sanctioned projection (every value through
`present_resource`, exact identifiers never retained) and is **already durably
stored** in the task-level approval record at `engine.py:4627`. Option A does not
invent a channel — it wires an existing, already-persisted projection into the
comparison.

### Diagnostics

Per-role. When a fingerprint mismatch forces a fresh approval, the diff names the
role: `dest` changed, `source` did not. This matters for the resume path
(`task.approval.resumed`) and for ADR-044-style fencing audits.

### Cost

Fingerprint size grows linearly with role count (≈5 fields per role). For 2 roles
that is one extra nested list in a dict that is already persisted per approval
record and per queue row.

---

## 4. A1 — Option B: role-set digest

### Shape

```jsonc
{
  "capability": "filesystem.move", "action": "move", "scope": "filesystem:write",
  "risk": "high", "side_effects": "irreversible",
  "resource_kind": "filesystem:path",
  "security_relevant_params": {"overwrite": false},
  "resource": "a.txt", "resource_fingerprint": "65b55ee7bd489ed9…",
  "resource_redacted": false,
  "resource_set_fingerprint": "015303907a0788eb…",   // NEW — opaque digest
  "resource_set_size": 2,                            // NEW
  "resource_set_display": "a.txt -> archive/b.txt"   // NEW — human surface
}
```

Digest material must be the **ordered role view**:
`"\0".join(f"{role}\0{kind}\0{value}" for r in resolved)`.

### Canonicalization rules — and the trap

The digest is only direction-sensitive if its material is the ordered role view.
Measured both ways:

| Digest material | `a→b` vs `b→a` | `a→b` vs `a→c` |
|---|---|---|
| **ordered role view** (`role\0kind\0value`) | distinct ✓ | distinct ✓ |
| **canonical view** (`kind\0canonical`) | **COLLIDES** ✗ | distinct ✓ |

So Option B is correct *only* if it deliberately does **not** use
`canonical_identities()` — the one helper ADR-061 built and the one whose name
suggests "canonical identity". That is a live footgun: the natural implementation
of "hash the resource set" reaches for the canonical view and silently
re-introduces the direction collision. It also must hash the **exact** value
(invariant 20), not the canonical one, to match Option A's semantics.

`resource_set_size` differs between views for `a→a` (role view 2, canonical 1), so
even the size field requires stating which view it counts.

### ADR-037 compliance

Compliant for the digest (hash over exact values, exact never stored). But
`resource_set_display` is a **second** presentation channel: a bounded joined
string built outside `present_resource`. For filesystem paths it is harmless
(each component already bounded and one-lined), but for a future `url` role it
would bypass `_url_display`'s userinfo/query/fragment stripping unless each
component is individually passed through `present_resource` first — at which
point Option B has rebuilt Option A's per-role projection and then thrown the
structure away.

### Diagnostics

Opaque. A mismatch says "the resource set changed" but not **which role** changed.
Since the human surface needs a display string anyway, Option B does not actually
avoid persisting per-role display data — it avoids persisting it *structured*.

### Cost

Constant-size digest regardless of role count. Meaningful only for actions with
many roles; `move` has two.

---

## 5. A1 — comparison and recommendation

| Criterion | **A — per-role presentation** | **B — role-set digest** |
|---|---|---|
| Distinguishes `a→b` / `a→c` / `b→a` / `a→a` | ✓ (measured) | ✓ only with ordered material |
| Footgun risk | low — order is structural | **high** — canonical view silently collides |
| New code | ~none: `_resources_metadata` exists and is already persisted | new digest + new display channel |
| ADR-037 | compliant by reuse | compliant, but adds a 2nd presentation path |
| Per-role diagnostics on mismatch | ✓ | ✗ |
| Human-readable without a second field | ✓ (`resources[i].resource`) | ✗ (needs `resource_set_display`) |
| Fingerprint size | O(roles) | O(1) |
| Consistent with ADR-061 D1's "role view → approval display" | ✓ exactly as specified | ✗ uses neither view cleanly |

**LOCKED (Q1): Option A.** Three reasons, in order of weight:

1. ADR-061 D1 already *assigns* the role view to "approval display" and the
   canonical view to "fingerprinting, lock acquisition ordering". Option A honours
   that assignment for the **display/identity** half; Option B invents a third
   thing. But §1's measurement shows D1's assignment is **half wrong**, verbatim
   (`ADR-061`, line 26):

   > **Canonical view** (`canonical_identities`): sorted set of `(kind,
   > canonical_resource(kind, value))` pairs, deduplicated (invariant 3) …
   > Used for **fingerprinting**, lock acquisition ordering (invariant 13).

   The canonical view is order-independent and deduplicated, so it **cannot
   express direction** — `a→b` and `b→a` hash identically (§2 measures this even
   at the `present_resource` level, whose hash is role-blind). Approval
   fingerprinting must therefore move to the role view. See **Q4**.
2. The projection is already implemented, already ADR-037-compliant, and already
   durably persisted in the task approval record. Option A is a wiring change.
3. Per-role diagnostics are the difference between an operator seeing *"dest
   changed"* and *"something about the resources changed"*.

Option B's only real advantage is O(1) size, which does not bind at two roles.

---

## 6. A1 — compatibility with durable approvals (a separate decision)

`_fingerprint_matches` compares by **exact dict equality** against two accepted
shapes (ADR-037's presentation form and the legacy exact-resource form; ADR-044
fences writes, ADR-037 §3 fences matching). Adding a key changes the dict, so the
question is what happens to approvals already on disk.

| Approach | Effect | Cost |
|---|---|---|
| **(i) Always include `resources`** | Every durable single-resource approval written before the change fails to match → fresh approvals required | Must add a **third** accepted shape to `_fingerprint_matches` (ADR-037 precedent: reshape + fence) |
| **(ii) Include `resources` only when `len(request.resources) > 1`** | Single-resource fingerprints stay **byte-identical**; multi-resource actions have **no** pre-existing durable records (no shipped action declares two roles) | Fingerprint shape becomes conditional on role count |

**LOCKED (Q2): approach (ii).** Single-resource fingerprints stay
**byte-identical**; the ordered `resources` projection is added only when
`len(resources) > 1`. That yields: no unnecessary invalidation of existing
approvals; role-sensitive identity exactly where it is required; `a→b` and `b→a`
cannot collide; and no historical multi-resource approval needs fencing, because
none exists.

**(ii) is consistent with** the convention ADR-061 D9/invariant 16 already
established: the singular keys keep mirroring the primary role, and the plural key
is **additive**. The fingerprint already has conditional shape
(`present_resource` yields `None` display/hash for a non-resource action), so (ii)
introduces no new kind of variability. Under (ii), a declaration change from one
role to two changes the fingerprint shape and therefore forces a fresh approval —
correct, since the action's resource contract changed.

### The ruling adds an obligation this note did not carry

The "none exists" argument is a **reason the migration is cheap today**, not a
**reason the shape is safe**. It must not be load-bearing forever: the first
shipped multi-resource action creates durable multi-resource approvals, and from
then on any change to the `resources` projection is a real compatibility event.
So (ii) must be pinned as an explicit invariant with tests, not left as an
emergent property:

- **INV-C1 (compatibility invariant).** For any `AuthorizationRequest` whose
  resolved role view has length ≤ 1, `_authz_fingerprint(request)` is
  **byte-identical** to the pre-M9-B.3 fingerprint — no `resources` key, no
  reordering, no change to any existing value. Test: assert the exact expected
  dict for a single-role action (golden-shape test), so any future addition of a
  key fails loudly.
- **INV-C2.** For length ≥ 2, the fingerprint contains `resources` in
  **declaration order**, and the singular primary-role keys still mirror
  `resources[0]` (invariant 16 preserved, not replaced).
- **INV-C3.** `_fingerprint_matches` accepts exactly the shapes it accepts today
  plus the (ii) multi-role shape — and **no** multi-role shape is accepted for a
  single-role request or vice versa. This is the test that would catch a future
  refactor quietly making the key unconditional and thereby staling every stored
  approval.

INV-C1 is the one that protects the "no invalidation" claim over time: it makes
byte-identity a tested property rather than a historical accident.

---

## 7. The human surface — DEFERRED, separate B.3 concern

**Ruling: do not fix either issue opportunistically inside L1/A1.** They are real
findings and stay recorded here, but whether they are B.3 scope is decided *after*
the core approval model is locked. Nothing in §7 is authorized by the Q1–Q4/Q8
rulings, and the L1/A1 implementation must not quietly expand to cover it.

The operator's decision surface does not show the second role today:

| Surface | Multi-resource today | Deferred work |
|---|---|---|
| `ApprovalRequest` (`state/approvals.py`) | scalar `resource_kind` / `resource` only | additive `resources: list[dict]` field; `from_dict` defaults to `[]` so legacy rows rehydrate |
| `_queue_request_from_auth` summary | `"{cap}/{action} on {primary_display}"` | `"{cap}/{action} {source_display} -> {dest_display}"` rendered from the role view, bounded to 300 |
| `arion approvals show` (`cli.py:1139`) | prints `req.resource` (primary) | print each role on its own line, `role=… resource=… redacted=…` |
| `GET /approvals` (`approval_api.py:148/162`) | `ApprovalRequest.to_dict()` | gains the field automatically once added |
| `_mirror_from_request` (`engine.py:2677`) | scalar only — **already inconsistent** with `_append_approval_record`, which projects the full ordered view | carry `resources` so the PENDING mirror matches the immediate-decision record |

Two consequences of deferring, stated so they are chosen rather than inherited:

1. **A1 changes what the fingerprint binds without changing what the operator is
   shown.** Under Q1+Q2, a two-role action's approval identity becomes
   direction-sensitive while `arion approvals show` still prints only the primary
   role. Authority becomes *stricter* than display — the safe direction, since an
   approval can never authorize more than the operator saw — but the operator
   still cannot see the destination they are being asked to authorize. That is a
   genuine gap, and it is the strongest argument for pulling this into B.3 scope.
   It is simply not L1/A1's job to close it.
2. The `_mirror_from_request` / `_append_approval_record` asymmetry is a
   **pre-existing bug independent of move**: two code paths build the same
   task-level approval record at different fidelity. Deferring it does not make it
   worse, and A1 touches neither path's display projection.

**LOCKED (Q5) — param exclusivity, refused at construction.** An `ActionSpec`
must not name a resource role in `security_relevant_params`. Today nothing forbids
it, and doing so encodes the same value through two channels with different
privacy semantics — hashed and bounded as a resource, **raw** as a
security-relevant param (which is how `overwrite` is stored). That is a direct
ADR-037 violation for any non-boolean role, and the same
mutually-exclusive-spellings hazard ADR-061 D9 refuses for
`resource_kind`/`resources`. Enforcement: `ActionSpec._validate_resources`
(`registry.py:167`) raises `ResourceDeclarationError`, alongside the existing D2
construction checks. A construction-time rule, so it lands with A1 rather than
depending on the data-flow. (Renamed from "A1-C1" in the pre-ruling draft to avoid
colliding with the §6 compatibility invariants INV-C1…C3.)

---

## 8. L1 — the lock set

### Identity and ordering

| Property | Decision | Why |
|---|---|---|
| Lock identity | `canonical_identities(resolve_resources(spec, params))` — sorted `(kind, canonical_resource)` pairs | invariant 2/3/13; finally gives both a production consumer |
| Acquisition order | the sorted canonical order, always | A global total order over resources makes a wait-for cycle impossible, so `a→b` and `b→a` running concurrently cannot deadlock — whichever sorts first wins the first lock |
| `a→a` | **one** lock, not two | canonical view dedups (invariant 3, measured: 2 roles → 1 identity). Acquiring the same `(kind, resource)` twice would hit the store's UNIQUE constraint and self-deadlock |
| Unresolved roles | **refuse before acquiring anything** | `canonical_identities` silently *omits* unresolved roles while the role view retains them. Locking a partial set is the "lock B while approving A+B" divergence. `unresolved_roles()` gates this — its first production consumer |
| Cross-kind | identity is the `(kind, value)` pair | invariant 2 / rejected alternative R6: `filesystem:path "x"` and `url "x"` must not collide |

### All-or-nothing semantics

- Acquire strictly in canonical order. On failure at identity *k*, release the
  *k−1* already-held locks in **reverse** order, then raise the existing typed
  `MutationLockError` / `MutationLockTimeoutError`. A partial lock set must never
  survive the attempt.
- Release on **every** terminal path, reverse order, each release using the
  existing atomic `release_and_select_next` (ADR-023) so handoff to the next FIFO
  waiter stays transactional.
- `_inflight_locks` is already a `set[(kind, resource)]`, so it holds a set
  unchanged; `_track_inflight_lock` is called per identity.

### The seams that are single-resource today

| Seam | Today | L1 requires |
|---|---|---|
| `_lock_canonical(spec, step)` | returns one `(kind, resource)`; **8 references** — definition at `engine.py:881` plus 7 call sites (959, 1425, 3026, 3040, 3270, 3423, 3450) | a `_lock_canonical_set` returning the ordered list; keep `_lock_canonical` as the primary-role view for compatibility, or migrate all 7 callers |
| `_acquire_mutation_lock` | returns one `lock` | returns an ordered list of locks; `waited` is True if **any** identity contended |
| `_renew_mutation_lock` | renews one lease | renews **all**; a failure on any one means ownership is lost for the whole step and must fence it (ADR-039), not just that lock |
| Post-wait re-authorization (`_post_lock_revalidate`, `engine.py:1287`; "ActionSpec/policy may have changed while we waited", :1327) | rebuilds via `_build_authz_request` (:1362) from the live spec after a waited acquire | unchanged in mechanism, but with A1 it now compares the full ordered role view — so a role-declaration change while waiting correctly stales the approval. New property, worth an explicit test |
| `_set_lock_contention_blocker` / `_lock_contention_resolver` | blocker names one `(kind, resource)` | blocker must name the **contended** identity, and the recheck must clear only when that identity is free. Naming the whole set would wedge a goal on an unrelated resource |
| Dispatch gating (3039 / `_step_dispatchable` 3422) | one key per step in `chosen_resources` | **all** identities of the step must be added, and the step is gateable if **any** collides — otherwise two steps in one round can still race on the second resource |
| Waiter queue (ADR-022/023) | one waiter row per `(kind, resource, task)` | **LOCKED (Q3): gate on the lead (lowest canonical) identity only** — one waiter row per step. Deadlock freedom comes from sorted acquisition order, not from the queue, so per-identity rows would add durable state and complicate `release_and_select_next` without buying correctness |

### What L1 does *not* change

Authorization, approval, verification, recovery and the capability itself. A lock
remains coordination and never permission (ADR-021); acquiring a lock set grants
nothing.

---

## 9. How L1 and A1 fit together

One declaration → one derivation → two views → two consumers:

```
ActionSpec.resources  =  [ResourceRole("source", fs), ResourceRole("dest", fs)]
                                   │
                        resolve_resources(spec, params)
                                   │  (ordered, duplicates retained, as-declared values)
                                   ▼
              ┌────────────────────┴────────────────────┐
              │                                         │
      ORDERED ROLE VIEW                        SORTED CANONICAL VIEW
   unresolved_roles() must be []            canonical_identities()
              │                                         │
     ┌────────┴────────┐                                │
     ▼                 ▼                                ▼
  A1 approval       human surface                 L1 lock set
  identity          (summary, CLI,            (acquisition order,
  (fingerprint,      approval API)             mutual exclusion,
   direction-                                deadlock freedom,
   sensitive)                                 a→a → ONE lock)
```

Worked through the four cases:

| Case | A1 binds (role view) | L1 locks (canonical view) | Same resource set? |
|---|---|---|---|
| `a→b` | `source=a.txt`, `dest=archive/b.txt` | `{a.txt, archive/b.txt}` in sorted order | ✓ |
| `a→c` | `source=a.txt`, `dest=secrets/c.txt` — **different fingerprint** | `{a.txt, secrets/c.txt}` | ✓ |
| `b→a` | `source=archive/b.txt`, `dest=a.txt` — **different fingerprint** | `{a.txt, archive/b.txt}` — **same set as `a→b`** | ✓ |
| `a→a` | two entries, equal hashes | **one** lock | ✓ |

`b→a` is the case that proves the two views must differ: it needs the **same
locks** as `a→b` (both touch both files, and canonical ordering is what stops them
deadlocking each other) but a **different approval** (it is the inverse
transformation, and approving one must not authorize the other).

`a→a` is the case that proves the difference is not a divergence: A1 binds two
roles, L1 takes one lock, and the underlying resource set is identical. Counting a
resource once for exclusion while naming it twice for approval is correct, not
inconsistent.

**LOCKED (Q6): the capability refuses it.** A move onto itself is either a no-op
or a destruction. The refusal sits **above** the view layer and changes neither
projection: `resolve_resources` still returns two entries (ADR-061 invariant 4 is
explicitly illustrated by `move a -> a` and must keep holding) and
`canonical_identities` still dedups to one identity. So the `a→a` row above stays
as specified — it is the *execution* that is refused, not the derivation. Any
implementation that "fixes" `a→a` by changing either view has broken invariant 4.

### Resolving the apparent contradiction with ADR-061 D1, line 28

> Both are derived from the same `ActionSpec.resources`; a caller can never
> **approve one view and lock another** (the "approve A, lock B" divergence class
> D1 exists to foreclose).

Read literally, §9's table violates this: `b→a` approves the role view and locks
the canonical view. The sentence is right in intent but wrong in letter, and the
distinction matters because L1/A1 are its first real consumers.

What D1 is actually protecting against is approving a resource set that locking
does not cover — "the operator approved A+B, the mutex took only A". That property
holds here, and it holds *because* both views come from one `resolve_resources`
call. The two views can disagree about a value in exactly **two** measured ways,
and both are safe:

| Way the views differ | Example | Effect | Safe? |
|---|---|---|---|
| Unresolved role omitted from canonical view | `dest` missing → role view `[a, None]`, canonical `{a}` | would lock half of what is approved | ✓ refused outright (invariant 5/6) — see join point below |
| **Spelling normalized by the canonical view** | `./a.txt` → `a.txt`; `archive//b.txt` → `archive/b.txt` (measured via `canonical_resource`, which is `os.path.normpath` for `filesystem:path`) | approval is over **as-declared** strings (invariant 20), locking over **canonical** ones | ✓ see below |

The spelling asymmetry is the important one, and it runs in the safe direction:
**approval is finer than locking.** `move ./a.txt → b` and `move a.txt → b` produce
*different* A1 fingerprints (so a cosmetic respelling forces a fresh approval
rather than silently reusing one) while taking the *same* L1 lock (so the two
spellings can never execute concurrently). Finer-approval + coarser-locking can
only ever fail closed. The unsafe combination — coarser approval, finer locking —
would let one approval authorize operations the mutex does not serialize, and
nothing in this design produces it.

**LOCKED (Q8) — stated as an invariant, because "conservative" is only a
property if it is enforced and tested:**

> **INV-A8.** Approval identity **may distinguish** two spellings that the lock
> identity treats as equivalent; it **must never collapse** two identities that
> locking treats as distinct.

The asymmetry is acceptable *because* it is conservative: over-distinguishing in
approval costs a redundant prompt, while under-distinguishing would let one
approval authorize an operation the mutex never serialized. The second half is the
load-bearing one, and it is exactly what a careless future "optimization"
(canonicalize before hashing, to cut duplicate prompts) would break:
canonicalizing the approval material would make `./a.txt` and `a.txt` the *same*
approval, and via `canonical_identities` would make `a→b` and `b→a`
indistinguishable again (§2).

**Regression example, mandated:** `./a.txt` vs `a.txt`. A test must assert
(a) different A1 fingerprints, (b) the same L1 canonical lock identity, and
(c) that re-planning with the respelled path forces a fresh approval rather than
reusing the stored one. This belongs in the A1 test file, not L1's.

What D1's letter forbids and M9-B.3 requires is that the two consumers ask
*different questions of the same set*. They must: exclusion is symmetric (a lock
on `b` blocks both directions) while approval is not (approving `a→b` must not
authorize `b→a`). Using one view for both would either re-introduce the A1
direction collision (canonical for approval) or deadlock L1 (role order for
locking, since two tasks could then acquire in opposite orders). **LOCKED (Q4).** Line 28 is
restated as the **covering-set** property (§1's third bullet: approval must cover a
superset of / equivalent of what execution locks, and the two may legitimately hold
different projections of the same resolved resources). Recorded as a targeted
**ADR-063 correction/clarification**; ADR-061 is **not** altered retroactively.
Line 26's assignment of "fingerprinting" to the canonical view is corrected in the
same ADR: canonical view → *lock acquisition ordering only*; role view →
*approval identity and display*.

**The join point is `unresolved_roles()`.** The role view *retains* an unresolved
role (value `None`) while the canonical view *omits* it. So a step with a missing
`dest` would present to A1 as `[source=a, dest=None]` but hand L1 only `{a}` —
locking half of what the approval names. That is precisely the "approve A+B, lock
A" divergence ADR-061 D1 exists to foreclose, and it is why:

- **L1** must refuse to acquire when `unresolved_roles()` is non-empty (invariant
  5/6 — "unresolved must REFUSE, never be treated as nothing to check");
- **V1** (`PlanValidator` iterating `spec.resources`) should reject it earlier, at
  plan time, so a model gets a typed retryable error instead of a DENY;
- **A1** never sees it, because authorization already refused (ADR-061 C4's
  `_one_resource_allowed` fails closed on a missing resource, naming the role).

Ordering across the milestone therefore matters: **V1 before L1** gives the model
path a plan-time signal; **L1 before C1** means the capability can never execute
unlocked; **A1 before C1** means it can never execute under an approval that
doesn't bind its destination.

---

## 10. Decisions — all ruled

### Rulings (2026-09-14)

Q1–Q6 and Q8 are locked as recommended or as reworded below; Q7 is a **deferral**
ruling, not an approval of the fix.

| # | Question | Ruling |
|---|---|---|
| **Q1** | A1 channel: Option A (per-role presentation) or Option B (role-set digest)? | **Option A.** Reuses the existing ADR-037-compliant `_resources_metadata` (`engine.py:4578`, already persisted at `:4627`), structurally order-safe, per-role diagnostics on mismatch. Option B rejected: correct only if it avoids `canonical_identities()`, and still needs a per-role display string, so it discards structure it has to rebuild anyway. |
| **Q2** | Fingerprint compatibility: (i) always include `resources` + fence a third shape, or (ii) include only when `len(resources) > 1`? | **(ii) conditional `resources`.** Single-resource fingerprints stay **byte-identical**; the ordered projection is added only for multi-resource requests. Gives: no unnecessary invalidation of existing approvals; role-sensitive identity exactly where required; `a→b` vs `b→a` cannot collide; no historical multi-resource approval needs fencing because none exists. **Plus an obligation this note did not originally carry:** the "none exists" argument must not stay load-bearing — pin the behaviour as explicit compatibility invariants **INV-C1…C3** (§6) and test them. |
| **Q3** | L1 waiter queue: gate on the **lead** canonical identity only, or enqueue a waiter row on **every** identity? | **Lead identity only.** Correctness comes from sorted acquisition order (a global total order makes a wait-for cycle impossible), not from the queue; per-identity rows would multiply durable state and complicate the atomic `release_and_select_next` handoff. ADR-023 fairness semantics unchanged for the contended resource. |
| **Q4** | Amend ADR-061 D1? Line 26 assigns the canonical view to "fingerprinting" (cannot express direction); line 28 says a caller "can never approve one view and lock another" (but `b→a` **requires** exactly that). | **Finding accepted.** Record as a targeted **ADR-063 correction/clarification** — ADR-061 is **not** altered retroactively. The distinction becomes explicit architecture: **approval/fingerprint view** ordered and role-sensitive (`a→b` ≠ `b→a`); **lock view** canonical and order-independent (both must lock the same set); **safety invariant** — approval must cover a superset of / equivalent of what execution locks, and the two **may legitimately hold different projections of the same resolved resources**. Stronger than pretending one canonical representation serves both. |
| **Q8** | Should the A1 fingerprint hash the **as-declared** value or the **canonical** one? | **As-declared**, documented and regression-tested. **INV-A8** (§9): approval identity *may distinguish* two spellings the lock identity treats as equivalent; it *must never collapse* two identities locking treats as distinct. Acceptable **because it is conservative**. Mandated regression example: `./a.txt` vs `a.txt` — different fingerprints, same canonical lock, respelling forces re-approval. |

| **Q5** | May a resource role also be named in `security_relevant_params`? (§7) | **No — refuse at construction.** `ActionSpec._validate_resources` raises `ResourceDeclarationError`. Otherwise the same value is persisted hashed-and-bounded as a resource *and* raw as a param, violating ADR-037 for any non-boolean role; it is the same mutually-exclusive-spellings hazard ADR-061 D9 already refuses for `resource_kind`/`resources`. Lands with A1 as a construction-time check. |
| **Q6** | `a→a` (source == dest): lock one resource and approve two roles, or refuse the step outright? | **Refuse at the capability.** A move onto itself is either a no-op or a destruction, and neither is worth an approval prompt. **Crucially this does not weaken ADR-061 invariant 4:** `resolve_resources` still returns **two** entries for `a→a` and `canonical_identities` still dedups to **one** identity — the refusal is a capability-level policy *above* the view layer, not a change to either projection. The general multi-role lock/approval semantics stay fully defined (§9) for the non-degenerate case. |
| **Q7** | Does `_mirror_from_request` get the `resources` projection (fixing the pre-existing asymmetry with `_append_approval_record`)? | **Reclassified → deferred** (was "yes, independently of move"). Ruled: keep the finding in the note, **do not fix opportunistically in L1/A1**; whether it is B.3 scope is decided after the core approval model is locked. The same ruling covers the CLI / `ApprovalRequest` primary-only display (§7). |

No question remains open.

---

## 11. Explicitly not in this note

- **V2** (`move_verified` two-resource verification policy), **R1**
  (`MutationRecovery` naming every role) and **C1** (the `filesystem.move`
  capability itself) are designed next, now that Q1–Q6 and Q8 are ruled. C1 in
  particular inherits Q5 (construction refusal) and Q6 (self-move refusal).
- The §7 human-surface work (`ApprovalRequest.resources`, the queue summary,
  `arion approvals show`, `_mirror_from_request`) is **deferred by ruling**, not
  omitted by oversight. It must not be pulled into an L1/A1 change.
- No claim about `filesystem.move`'s `side_effects` declaration beyond P0 having
  made `irreversible` safe to declare.
- No performance analysis of lock-set acquisition beyond noting `_lock_canonical`
  has 8 references.
- Nothing here is implemented. **P0 is landed; L1, A1, V1, R1, V2, C1 and T1 are
  not.**
- **Before any L1 implementation**, two further artifacts are required by ruling:
  the final **ADR-063** wording (the Q4 correction/clarification, including the
  INV-A8 and INV-C1…C3 invariants), and the concrete **L1/A1 data-flow** from
  `ActionSpec.resources` through `resolve_resources` to the lock set and the
  fingerprint. Neither exists yet.

---

## 12. Reproduction

The §1–§4 measurements come from a throwaway script that imports the real
`resolve_resources`, `canonical_identities` and `present_resource` and prints the
shapes and equality results verbatim. It made no repository change. The essential
assertions, re-runnable in one line each:

```python
from arion.orchestration.resource_set import resolve_resources, canonical_identities
# canonical view is direction-blind (correct for locks, fatal for approvals):
canonical_identities(resolve_resources(move_spec, {"source":"a","dest":"b"})) == \
canonical_identities(resolve_resources(move_spec, {"source":"b","dest":"a"}))   # True
# role view is direction-sensitive:
[(r.role, r.value) for r in resolve_resources(move_spec, {"source":"a","dest":"b"})] != \
[(r.role, r.value) for r in resolve_resources(move_spec, {"source":"b","dest":"a"})]  # True
# a->a: two roles, one canonical identity
len(resolve_resources(move_spec, {"source":"a","dest":"a"}))          # 2
len(canonical_identities(resolve_resources(move_spec, {"source":"a","dest":"a"})))  # 1

from arion.resource_identifiers import _fingerprint
# present_resource's hash is ROLE-BLIND, so an unordered set of hashes collides
# across direction (this is what rules out "just hash the resource set"):
sorted(_fingerprint("filesystem:path", v) for v in ("a","b")) == \
sorted(_fingerprint("filesystem:path", v) for v in ("b","a"))         # True

from arion.state.locks import canonical_resource
# approval is finer than locking: canonical_resource normalizes spelling (§9)
canonical_resource("filesystem:path", "./a.txt")        # 'a.txt'
canonical_resource("filesystem:path", "archive//b.txt") # 'archive/b.txt'
```

The existing `tests/test_resource_views.py` already asserts the first property as
intended behaviour (`test_canonical_view_is_order_independent`), which is the
strongest evidence that the canonical view was designed for locking and not for
approval identity.
