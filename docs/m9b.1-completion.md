# M9-B.1 Completion — Structured Filesystem Search

- Capability: `filesystem.search` (action: `search`)
- Scope: `filesystem:read` (existing)
- Resource model: `directory` (`filesystem:path`), verified via `RelativePathBoundary`
- Verification: `schema_keys` with `keys: ["results", "count"]` — validates structure; zero matches (`count==0`) is legitimate success (not `non_empty`)
- Bound: `max_results` capped at 100 (hard); `pattern` bounded at 200 chars (PlanSchema); result paths verified via `_resolve_inside` (fail-closed for symlink escape)
- Determinism: sorted by relative path; duplicate paths deduplicated implicitly by `resolve()` + sorted result
- Security: search result is DATA, never authorization; subsequent read/write/append receive independent authorization against `directory`/`path`
- No changes to `authz.py`, `engine.py`, `PlanValidator`, `ResolvedResource` required.
- Tests: `tests/test_search.py` (8 tests) — happy, empty, nested, boundary, symlink, determinism, authorization.
- Full suite: PASS (exit 0) at HEAD `38c4574`.

---

## Correction — 2026-09-13 (M9-B.2 investigation)

The original text above is preserved unchanged as history, following the
convention set when the M7 audit was left intact and superseded by
`docs/m8-completion-note-2026-09-06.md`. Two of its claims do **not** hold at
`27a144b` (merge PR #14, `main`), the commit this document landed on. Full
evidence and reproduction: **ADR-062** and
`docs/m9b.2-architecture-proposal.md` §1–§2.

**1. "Full suite: PASS (exit 0)" is false at `main`.** Measured at `27a144b`:
**1979 collected, 1961 passed, 16 FAILED, 2 skipped, exit 1.** Whether the suite
was green at `38c4574` could not be verified — that commit is not reachable from
this clone.

**2. "Introduced through the existing authority boundary" is false.** The
capability was added to the *registry*, but it never crossed the *boundary*:

- `FilesystemSearchCapability.actions` was declared as a list containing a **raw
  dict**, not an `ActionSpec`. Every other shipped capability uses `ActionSpec`.
- `CapabilityRegistry.action_spec()` (`registry.py:238`) and
  `capabilities_summary()` (`registry.py:247`) therefore raised
  `AttributeError: 'dict' object has no attribute 'name' / 'to_dict'`.
- `build_engine()` registers search unconditionally (`bootstrap.py:81`), so the
  whole catalog failed, not just search:
  - **`RealModelPlanner.plan()` could not plan any goal** — `capabilities_summary()`
    is the catalog handed to the provider (`model_planner.py:112`). M9-B.1
    disabled the M9-A model path in every default-built engine.
  - **`arion capabilities` crashed** (`cli.py:398`).
- A step targeting `filesystem.search` **could not execute at all**; it crashed in
  `_plan_steps_for_audit` (`engine.py:3897`), and on the deterministic planner
  path the exception **escaped `run_goal()` unhandled**.
- Causal proof: commenting out `bootstrap.py:81` alone turned all 16 failures
  into `41 passed, 1 skipped`.

**3. Why the 8 tests did not catch it — a test-shape gap, not a test-count gap.**
`tests/test_search.py` calls `execute()` directly and *hand-constructs* its
`AuthorizationRequest`. It never calls `registry.action_spec()` and never drives a
step through the engine, so it tested the capability rather than the boundary the
note claims to have exercised. Adding more direct-execution tests could not have
found this.

**4. What remains true.** The design decisions listed above are sound and were
not changed by M9-B.2: the `filesystem:read` scope reuse, the `directory`
resource role under `RelativePathBoundary`, `schema_keys` verification with
`count==0` as legitimate success, the `max_results ≤ 100` / `pattern ≤ 200`
bounds, `_resolve_inside` symlink fail-closed behaviour, sorted/deduplicated
determinism, and "search results are DATA, never authorization". No change to
`authz.py`, `engine.py`, `PlanValidator` or `ResolvedResource` was needed — that
claim holds.

**Resolution (M9-B.2, ADR-062).** `CapabilityRegistry.register()` now enforces
the declaration contract and refuses a capability it cannot read
(`CapabilityDeclarationError`); `filesystem.search` is redeclared as an
`ActionSpec` with its M9-B.1 semantics preserved exactly (all 8 original tests
pass **unmodified**). Two new permanent gates were added: boundary transit
(every registered action reaches a durable terminal status through the real
engine, never an exception out of `run_goal()`) and the bootstrap registry
invariant (the default catalog is complete, JSON-serializable and reaches the
model planner). Suite at M9-B.2 HEAD: **2042 collected, 2040 passed, 2 skipped,
0 failed, exit 0.**
