# ADR-009 — Resource-aware authorization

- **Status:** Approved (2026-08-09) — required follow-up to commit `0c0f020`
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

The vertical slice authorized steps by scope prefix only (`filesystem:`), while
the filesystem capability independently enforced its sandbox. That worked for
one read-only capability, but it does not scale: authorization and capability
containment are different concerns and must remain separate. Before Arion
gains side-effecting capabilities (writes, shell, network), every action must
be decided against a structured description of what it does and what it
targets.

## Decision

Authorization is a dedicated layer (`arion/orchestration/authz.py`) with the
conceptual flow:

```
Capability -> Action -> Resource -> Parameters -> Policy Decision
```

- **AuthorizationRequest** carries everything a policy needs: agent identity,
  task/step context, capability, action, the **resolved** scope (from the
  capability's ActionSpec metadata — never from a plan's claimed scope),
  parameters, the resource (e.g. filesystem path), risk, side effects,
  idempotency and retry safety.
- **PermissionPolicy** (`decide(request) -> PolicyDecision`) is the protocol;
  `ResourcePolicy` is the default implementation with a deterministic decision
  pipeline: agent not permitted -> DENY; scope not allowed -> DENY; scope
  denied -> DENY; resource outside path constraints -> DENY; high risk -> DENY;
  medium risk -> REQUIRE_APPROVAL; else ALLOW.
- **Outcome model:** `ALLOW | DENY | REQUIRE_APPROVAL`.
- **Approval seam:** a `REQUIRE_APPROVAL` decision routes to an
  `ApprovalHandler` (`request(request, decision) -> APPROVED | DENIED |
  PENDING`). A future human approval interface (notification, GUI, queue)
  implements this protocol; the engine does not change. PENDING pauses the
  task (`awaiting_approval`), checkpoints it, and `run_task` resumes the same
  step after approval.
- **Source of truth:** the engine resolves each step's `ActionSpec` from the
  capability registry and authorizes against `spec.required_scope`, risk and
  side-effect metadata. A plan's claimed `scope` is advisory only and is
  recorded in the audit event for transparency. Scope spoofing therefore
  cannot escalate privileges.
- **Containment stays in the capability:** the filesystem sandbox still
  enforces its own boundary (symlink-safe, size-capped, read-only). Policy
  path constraints are a *pure* string-level check (no filesystem access) and
  are an additional layer, not a replacement.

## Consequences

- New capabilities declare metadata once (scope, risk, side effects,
  idempotency, retry safety) and get authorization for free.
- Authorization is testable in isolation from capabilities.
- Malformed plans (traversal in params, spoofed scopes, unknown actions,
  unknown capabilities) are rejected or contained, and audited.
- The approval seam is the future home of human-in-the-loop control.

## Related

ADR-004 (permission gate in the loop), ADR-006 (capability model),
ADR-007 (permission events audited), ADR-010 (execution semantics).
