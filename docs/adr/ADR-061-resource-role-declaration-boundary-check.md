# ADR-061 — Resource-role declaration and boundary check (M8 C1–C4)

- **Status:** Approved — implemented in M8 C1 (`0221eb4`) → C2 (`c72d5b2`) → C3 (`b00ecc7`) → C4 (`bf4ea05`); merged to `main` via PR #13 (`64a7332`, 2026-09-06).
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)
- **Baseline:** ADR-060 seam analysis (2026-09-05) identified that `_resource_allowed` read exactly `request.resource` — the primary role — so a multi-resource action's second resource was never boundary-checked. The capability sandbox would still refuse execution, but that inverts ADR-009 (policy must never bless an out-of-boundary resource; capability is defence-in-depth).

---

## Context

Before M8, `ActionSpec` declared at most one resource via `resource_kind` + `resource_param`. Actions that conceptually targeted two resources (e.g., `source` and `dest`) could only declare one; the second was implicit in `params`. Authorization (`authz.py`) validated only the declared primary resource. This created a security gap: a step with `source=README.md` and `dest=../../etc/passwd` could receive an `ALLOW` from policy for the bounded source while the unvalidated destination was never checked. A human approving such a step would see a bounded source and an unvalidated destination.

The need for a structured multi-resource declaration was also required by M7-A (verified mutation outcome authority): durable projections of resource views must be ordered, role-preserving, and deterministic for fingerprinting and lock acquisition.

---

## Decision

### D1 — Ordered resource-role declaration is the single authoritative declaration

`ActionSpec.resources: list[ResourceRole]` (ordered, no deduplication) is the one source of truth for which resources an action targets. `ResourceRole(role: str, kind: str)` binds the human-facing role name (`"source"`, `"dest"`) to the parameter key holding the value and to the resource kind (`"filesystem"`, `"url"`, ...). The same string names the approval surface, the params key, and the policy category — never a second naming axis (ADR-061 D1, invariant 1).

Derived views — never independently authoritative — are produced from this declaration:

- **Role view** (`resolve_resources` in `resource_set.py`): ordered as declared, duplicates retained (invariant 4), values exactly as declared (invariant 20). Used for approval display, capability execution, recovery metadata.
- **Canonical view** (`canonical_identities`): sorted set of `(kind, canonical_resource(kind, value))` pairs, deduplicated (invariant 3), identity is the pair never a bare string (invariant 2, rejected alternative R6). Used for fingerprinting, lock acquisition ordering (invariant 13).

Both are derived from the same `ActionSpec.resources`; a caller can never approve one view and lock another (the "approve A, lock B" divergence class D1 exists to foreclose).

### D2 — Fail closed at construction

Ambiguity is never silently resolved by precedence (no "last wins", no "singular overrides plural"). `ActionSpec.__post_init__` raises `ResourceDeclarationError` immediately when any of the following holds (verified by `test_resource_declaration.py`, `test_authz_resource_set.py`):

- Both singular (`resource_kind`/`resource_param`) and plural (`resources`) declared (invariant 21 — only one representation at runtime).
- Singular is incomplete (`resource_kind` set but `resource_param` None, or vice versa).
- Duplicate roles within `resources`.
- Role names a param absent from `param_schema` (would be unresolvable at runtime).
- Entry is not a `ResourceRole` instance, or `role`/`kind` is empty.

### D3 — Boundary-check every declared resource (C4)

`PermissionPolicy._resource_allowed` no longer validates only `request.resource`. It iterates `request.resources` (the complete ordered set carried by `AuthorizationRequest` at C3) and denies on the first failure, naming the failing role so diagnostics distinguish `source is outside` from `dest is outside`. Each resource is checked against the boundary for ITS OWN kind (`_one_resource_allowed`); unconfigured kind, unresolved value, and outside boundary all fail closed.

The single-resource path (`if not request.resources`) is byte-identical to pre-C4 behaviour, preserving compatibility with existing actions.

### D4 / D9 — Compatibility sugar (additive, not a precedence rule)

Singular `resource_kind`/`resource_param` is normalized to a one-element `resources` list at construction (primary role mirrored into singular fields, invariant 16). Both spellings are mutually exclusive (D9). Existing readers of `ActionSpec.resource_param` continue to work because the primary role is preserved — this is additive (new `resources` key does not break old readers) rather than a replacement.

---

## Invariants (grounded in source)

| # | Statement | Source reference |
|---|---|---|
| 1 | Single authoritative declaration (`ActionSpec.resources`) | `registry.py` (documented at D1); `resource_set.py` docstring |
| 2 | Canonical identity is `(kind, canonical_value)` pair, never bare string | `resource_set.py` (`identity` property, invariant 2; rejected R6) |
| 3 | Canonical view deterministically ordered + deduplicated | `resource_set.py` (`canonical_identities`, invariant 3) |
| 4 | Duplicate roles retained in role view (e.g., `move a -> a`) | `resource_set.py` (`resolve_resources`, invariant 4) |
| 5 | Unresolved role must REFUSE, never treated as "nothing to check" | `authz.py` `_one_resource_allowed` (C2 hand-off note; invariant 5) |
| 6 | Incomplete resource set must refuse; never empty/safe | `authz.py` (`resources` comment; invariant 6) |
| 7 | Canonicalization never alters the value shown to a human | `resource_set.py` (`ResolvedResource` docstring, invariant 7 / invariant 20) |
| 13 | Lock acquisition uses deterministic canonical ordering | `resource_set.py` (`canonical_identities`, invariant 13) |
| 16 | Primary (first-declared) role preserved in singular fields | `registry.py` (`primary = self.resources[0]`; invariant 16) |
| 20 | As-declared string shown to human is unchanged by canonicalization | `resource_set.py` (`value` vs `canonical`; invariant 20) |
| 21 | At runtime exactly one representation exists (not dual spellings) | `registry.py` (`_normalize_resources`, D9 / invariant 21) |

---

## Consequences

- Multi-resource actions can now declare `source` + `dest` explicitly; authorization, approval display, fingerprinting, and mutation locks all use the same ordered declaration.
- Policy never blesses an out-of-boundary resource (C4 closes ADR-060 gap). Capability sandbox remains defence-in-depth per ADR-009.
- Existing single-resource capabilities (`filesystem.read`, `git.log`, `http.get`) require no change; normalization is transparent.
- The derived-view architecture prevents approval/locking divergence (approve one resource, lock another).

---

## Related

- ADR-009 (resource-aware authorization; policy vs containment separation)
- ADR-060 (M7 verified outcome execution; seam analysis that found the C4 gap)
- M8 C1 (`0221eb4`) — ActionSpec resource-role declaration + compatibility sugar
- M8 C2 (`c72d5b2`) — derived resource views (role-preserving + canonical)
- M8 C3 (`b00ecc7`) — AuthorizationRequest carries complete resource set
- M8 C4 (`bf4ea05`) — boundary-check every declared resource (first behavioural/security commit of M8)
- `tests/test_boundary_every_resource.py` (19 cases, C4 validation)
- `tests/test_authz_resource_set.py` (15 cases, C2/C3 validation)
- `tests/test_resource_declaration.py`, `tests/test_resource_views.py` (D1/D2/D9)
