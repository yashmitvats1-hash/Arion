# ADR-006 — Capability and permission model

- **Status:** Approved (2026-08-09)

## Context

Arion will eventually control shell, filesystem, browser, GitHub, and
applications. Unrestricted access is unacceptable: the orchestrator must gate
every action through an explicit permission check, and capabilities must be
self-describing for discovery and planning.

## Decision

- Every capability implements the `Capability` protocol: `name`, `description`,
  a list of `ActionSpec`s (each declaring a **required permission scope**),
  and `execute(action, params) -> structured observation`.
- Capabilities are registered in a `CapabilityRegistry` used by the planner
  (discovery) and the engine (execution).
- The engine checks a `PermissionPolicy` before every step: scope + params ->
  allow/deny. Denials are audited and fail the step (and eventually the task,
  or route to human approval).
- The first capability is **`filesystem.read`**: read-only, sandboxed to the
  repo root, size-capped, symlink-safe. No shell, no writes, no network.

## Consequences

- New capabilities plug in via registry + policy, never by editing the loop.
- The security boundary is enforced in one place (permission gate) and tested.
- Capability self-descriptions can feed model-backed planning later.

## Related

ADR-001 (capabilities layer), ADR-004 (permission gate in the loop),
ADR-007 (permission events audited).
