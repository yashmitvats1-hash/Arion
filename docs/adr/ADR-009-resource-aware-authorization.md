# ADR-009 — Resource-aware authorization

- **Status:** Approved (2026-08-09) — required follow-up to commit `0c0f020`;
  **amended (hardening)** per review of `7e2a87c` — see Amendment below.
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

---

## Amendment — fail-closed resource authorization + identity (hardening)

Review of the initial milestone (`7e2a87c`) required two changes. The
orchestration spine, checkpointing, ActionSpec, ApprovalHandler, capability
registry and at-least-once semantics are preserved unchanged.

### 1. Fail-closed resource boundaries

`ResourcePolicy` previously allowed a resource when no constraint was
configured (`path_constraints`). That is too permissive. Semantics now
distinguish three states:

- **No resource:** an action whose `ActionSpec.resource_kind` is `None` has
  nothing to constrain and is never denied on resource grounds.
- **Explicitly constrained:** the policy holds `boundaries: dict[resource_kind,
  ResourceBoundary]`. A configured boundary (e.g. `RelativePathBoundary`,
  `PathPrefixBoundary` for kind `"filesystem:path"`) is enforced against the
  action's resource.
- **Requiring a boundary but lacking one:** a resource-sensitive action whose
  `resource_kind` has no configured boundary is **DENIED** — fail closed.
  Absence of a boundary never means unrestricted access.

Boundaries are keyed by resource **kind**, not capability name, so a new
capability reuses the boundary of its kind and a new kind must be explicitly
configured before any of its actions can run. The policy performs pure
string-level checks; the capability still enforces its own containment
(sandbox root, symlinks). The resource is extracted generically by the engine
from `ActionSpec.resource_param` — no filesystem-specific logic lives in the
policy, and a plan cannot redirect which parameter is read.

### 2. Identity abstraction

`agent="system"` is no longer the final authorization model. Authorization
requests carry an `Actor` with a delegation chain:

```
user -> agent -> delegated agent -> capability
```

- `Actor.user("alice").delegated("arion").delegated("delegate-7")` yields
  `id = "agent:delegate-7"` and `chain = ("user:alice", "agent:arion",
  "agent:delegate-7")`.
- Policies and approval flows can match the direct actor or any ancestor in
  the chain (e.g. `allowed_agents={"user:alice"}` permits alice and anything
  she delegates).
- Audit events (`permission.checked`) record `actor` and `actor_chain`.
- No identity *system* is built yet — this is the abstraction future
  authentication, per-user policies and per-user approvals attach to.

### 3. Test matrix added

Adversarial tests prove: missing boundary -> DENY; valid boundary -> normal
evaluation; outside boundary -> DENY; non-resource actions unaffected; scope
spoofing and resource-param smuggling cannot bypass; traversal denied;
identity/delegation matching. See `tests/test_authz.py`.
