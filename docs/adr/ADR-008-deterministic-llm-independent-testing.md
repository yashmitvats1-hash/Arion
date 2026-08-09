# ADR-008 — Deterministic LLM-independent testing

- **Status:** Approved (2026-08-09)

## Context

If tests require a live LLM, the loop is untestable in CI, flaky, costly, and
— worse — the architecture quietly degrades into "prompt in, text out".

## Decision

All orchestration tests run with **no LLM**: the deterministic planner and
deterministic router are used throughout. Tests verify the state machine,
persistence round-trips, restart/resume behavior, permission denials,
verification outcomes, retries, and the audit trail — all with real SQLite in
tmp dirs and a real sandboxed capability.

A separate mocked-model integration test demonstrates that a model-backed
planner can be swapped in while the loop behaves identically.

## Consequences

- CI runs fully offline and deterministically.
- The spine is proven independent of any provider (ADR-005).
- When real models arrive, tests remain valid; only the router adapter and a
  small set of smoke tests change.

## Related

ADR-005 (ModelRouter), ADR-002 (pytest dev extra).
