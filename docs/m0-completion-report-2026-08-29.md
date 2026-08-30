# Arion — M0 Completion Report (Baseline Hygiene / Confirmed Defects)

- **Date:** 2026-08-29
- **Base commit:** `6f7e493a7a9c26b31697e42a86bcf9109c8ff5a4` (branch `arena/01a04d3d-arion`)
- **Scope:** exactly the three confirmed defects from the Phase Transition Audit.
  No model-path work, no new capabilities, no schema changes, no tests weakened
  or deleted.

---

## 1. Files changed

| File | Change |
|---|---|
| `scripts/demo_adr026_cross_process_scheduler.py` | D1 fix: two `mark_terminal` calls now pass the deterministic `now` (`now=T0`, `now=_iso_plus(T0, 3)`), matching the ADR-042 lease-expiry semantics the store enforces. |
| `arion/cognition/strategy.py` | D2 fix: rule 1 of `StrategySelector` now recognizes capability-shaped dotted tokens via a new `_looks_like_capability_token` helper (lowercase identifier parts, non-extension final part) + `_FILE_EXTENSION_SUFFIXES` set; `missing` list is `sorted()` for cross-run determinism. |
| `docs/architecture.md` | D3 fix: security-boundary section rewritten to reflect the implemented default-deny posture (no shell/subprocess/dynamic code; sandbox-contained filesystem; mutating capabilities and `http.get` registered but DENIED by default); ADR-024 demo check count corrected 30 → 31 (verified actual). |
| `tests/test_strategy_dotted_tokens.py` | **New** regression suite (19 tests): file tokens, dotfiles, version strings are never missing capabilities; unregistered capability tokens still block; registered capability mentions are never "missing"; missing list is sorted/deterministic. |
| `docs/phase-transition-audit-2026-08-29.md` | Untracked (previous deliverable) — left untouched as the historical audit record. |

## 2. Behavioral changes

- **D1 — demo:** `scripts/demo_adr026_cross_process_scheduler.py` now runs to
  completion (exit 0, "ADR-026 demo PASSED (33 checks)") instead of crashing
  with `SchedulerStateError: stale owner … (fail closed)` under the real clock.
  No store/engine semantics changed — the demo now simply respects the
  ADR-042 lease check it was violating.
- **D2 — strategy:** goals mentioning ordinary dotted file names (`README.md`,
  `package.json`, `pyproject.toml`, `notes.log`, `.env.example`), path
  fragments (`docs/design.md`) and versions (`v1.2.3`) no longer get the
  informational `blocked_missing_capability` label. Genuine unregistered
  capability mentions (`filesystem.write`, `http.get`, `web.search`,
  `browser.automation` when unregistered) still select
  `blocked_missing_capability`. Registered capability mentions are never
  "missing". The `missing_capabilities` constraint is now sorted (stable
  across interpreter runs). **No authority behavior changed**: the engine's
  durable `planner.required_capabilities()` gate — the authoritative blocking
  mechanism — is untouched.
- **D3 — docs:** architecture.md no longer claims "no writes, no network"; it
  now states the true fail-closed default (mutations + HTTP registered but
  DENIED until explicitly authorized/configured), matching `bootstrap.py`.

## 3. Tests added/changed

- Added: `tests/test_strategy_dotted_tokens.py` — 19 new tests
  (4 parametrized groups + 1 determinism test). 18 directly cover the D2 fix;
  1 covers deterministic ordering.
- Changed: **none** (no existing test was modified, weakened, or deleted).

## 4. Full-suite result

```
1478 passed, 2 skipped in 174.44s (exit 0)
```

(Previously 1,459 passed, 2 skipped. The 2 skips are unchanged and benign:
the gated live-provider smoke test and the symlink-unsupported-platform skip.)

Targeted regression run (strategy, poisoning, goal-blocked-capability,
cognition-authority, scheduler leases/reclaim/adversarial, multi-process
scheduler): **216 passed**.

## 5. Demo result

All 23 offline demos re-run: **23/23 pass, exit 0 each**.

| Demo | Result |
|---|---|
| demo_adr013_learning_loop | PASSED (28 checks) |
| demo_adr014_cognitive_archival | PASSED (32 checks) |
| demo_adr015_strategy_learning | PASSED (33 checks) |
| demo_adr016_goal_replan | PASSED (40 checks) |
| demo_adr016_plan_management | PASSED (35 checks) |
| demo_adr019_write_approval | PASSED (26 checks) |
| demo_adr020_append_recovery | PASSED (25 checks) |
| demo_adr021_lock_two_process | PASSED (24 checks) |
| demo_adr022_lock_wait | PASSED (15 checks) |
| demo_adr023_lock_fairness | PASSED (26 checks) |
| demo_adr024_concurrency | PASSED (31 checks) |
| demo_adr025_cross_goal_concurrency | PASSED (29 checks) |
| **demo_adr026_cross_process_scheduler** | **PASSED (33 checks)** — was exit 1 |
| demo_adr027_weighted_scheduler | PASSED (31 checks) |
| demo_adr028_scheduler_observability | PASSED (28 checks) |
| demo_adr029_reserved_capacity | PASSED (32 checks) |
| demo_adr030_capacity_planning | PASSED (35 checks) |
| demo_adr031_goal_ceilings | PASSED (32 checks) |
| demo_goal_approval / demo_approval_queue / demo_goal_replan | PASSED (all checks) |

Note: the ADR-026 demo's advertised "33 checks" count in architecture.md was
accurate all along (the demo itself was broken); no count edit was needed for
that line. The ADR-024 count was genuinely stale (30 vs actual 31) and is fixed.

## 6. Coverage result

- Overall: **87%** (10,354 statements, 1,353 missed) — unchanged from the
  audit baseline (statement count grew by 8 from the new strategy helper).
- `arion/cognition/strategy.py`: **94%** (124 stmts, 7 miss). The single new
  uncovered line is the defensive `len(parts) < 2` branch in
  `_looks_like_capability_token`, unreachable by construction because the
  caller only invokes it for tokens containing `"."` (so a split always yields
  ≥ 2 parts). All other misses are pre-existing branches.

## 7. Working tree / diff verification

```
 M arion/cognition/strategy.py
 M docs/architecture.md
 M scripts/demo_adr026_cross_process_scheduler.py
?? docs/phase-transition-audit-2026-08-29.md   (previous deliverable, untouched)
?? tests/test_strategy_dotted_tokens.py        (new regression tests)
```

- Exactly 3 tracked files modified — all three confirmed-defect targets.
- No unrelated files modified; no schema/DDL changes; no authority-model
  changes; no model-path changes.
- Runtime artifacts from verification (`arion_data/`, `.pytest_cache`,
  `.coverage`, egg-info, `__pycache__`, temp DBs) removed; no stray files.

## 8. Remaining known issues

1. **Corner case (accepted, documented in code):** a bare dotted token whose
   final part is both a capability action and a common file extension — e.g.
   `git.log` mentioned literally while `git.log` is *unregistered* — is now
   treated as file-like by the informational strategy rule (`.log` is a common
   extension). The authoritative behavior is unaffected: the engine's
   `planner.required_capabilities()` gate still durably BLOCKS such goals with
   the correct `missing_capability` blocker. No existing test depends on the
   old label for this case.
2. **Unchanged from the audit (not in M0 scope):** model-backed path not wired
   by default (next phase, ADR-057); no CI/lint/type gate; large-file
   maintainability (`engine.py`, `store.py`); audit/scheduler event growth
   (retention exists for scheduler events and memory, not audit_events).
3. **Audit report** (`docs/phase-transition-audit-2026-08-29.md`) remains as
   the historical record of the pre-M0 state; this report supersedes its
   defect claims.

---

## M0 gate summary

| Gate | Result |
|---|---|
| Targeted regression tests | 216 passed |
| Full test suite | 1,478 passed, 2 skipped, exit 0 |
| All 23 demos | 23/23 pass, exit 0 (incl. previously-broken ADR-026) |
| Coverage | 87% overall; strategy.py 94% |
| Working tree / diff | 3 tracked files changed (all defect targets), 1 new test file |
| Unrelated modifications | none |
| End-to-end defect re-check | `arion run "summarize the README.md"` → goal completed, strategy `direct` (was `blocked_missing_capability ['readme.md']`) |

**M0 is complete and green.** Stopping here as instructed — no ADR-057 or
model-path implementation started.
