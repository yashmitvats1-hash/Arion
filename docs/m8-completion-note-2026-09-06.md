# M8 Completion Note — 2026-09-06

**Status:** Completion / audit note (not an ADR; not a replacement for M7 audit).  
**Baseline:** `64a7332` (merge PR #13, `main`).  
**Previous audit:** `docs/m7-capability-audit-2026-09-05.md` (updated with post-M8 note; remains historically useful; superseded for security/authorization claims by ADR-061).

## What M8 accomplished (C1 → C4)

| Commit | Description | Key artifact |
|---|---|---|
| `0221eb4` C1 | ActionSpec resource-role declaration + compatibility sugar | `capabilities/registry.py` (D1/D2/D9) |
| `c72d5b2` C2 | Derived resource views (role-preserving + canonical) | `orchestration/resource_set.py` (invariants 1,2,3,4,13,20) |
| `b00ecc7` C3 | AuthorizationRequest carries complete ordered resource set; engine projects durable view | `orchestration/authz.py`, `orchestration/engine.py` |
| `bf4ea05` C4 | Boundary-check every declared resource (first behavioural/security commit) | `authz.py` (`_resource_allowed` / `_one_resource_allowed`) |

The security gap from ADR-060 seam analysis (resource check only read `request.resource`; second resource never validated) is closed. Before C4, policy could `ALLOW` a step whose `dest` was absolute or traversing; now it denies on first failure, naming the failing role.

## Validation

- Full suite at `64a7332`: 1955 collected, 1953 passed, 2 skipped (smoke/live-provider), 0 failed, 0 errors. Exit 0. Runtime ~205 s.
- Targeted M8 tests: `test_boundary_every_resource.py` (19), `test_authz_resource_set.py` (15), `test_api_authz_unification.py`, `test_resource_declaration.py`, `test_resource_views.py` — all pass.
- Module imports verified for all 9 files changed between `898127b` and `bf4ea05`.
- Working tree clean; HEAD unchanged; no source/test modifications made during audit.

## Why this is cleaner than committing M7 as final

The M7 audit (`docs/m7-capability-audit-2026-09-05.md`) is explicitly titled a working document and notes it is not an ADR. Its headline finding — behavioural reach asymmetry (7 actions / 78 CLI subcommands) — is valuable historical context but does not claim M8 completion. Rather than altering its historical framing, a brief M8 note links the two and points to ADR-061 for the authorization/security decisions that implement the gap it identified.

## What remains for next phase (not implemented — strategic only)

- Model-backed planner integration (`RealModelPlanner`, `OpenAICompatModelRouter`) is unproven end-to-end; only a single gated smoke test exercises the full provider→plan→execute path (per M7 audit and `docs/architecture.md`).
- Capability vocabulary remains 7 actions; no delete, move, shell, POST, scheduled trigger.
- The substrate (weighted scheduling, reservations, ceilings, FIFO lock queues, cross-process lease fencing, plan version fencing) is extensively tested but under-fed by actions.
- These are strategic choices for M9, not defects to fix in M8.
