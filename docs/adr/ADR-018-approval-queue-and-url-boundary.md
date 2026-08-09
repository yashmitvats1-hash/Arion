# ADR-018 — Persistent Approval Queue + Generalized Resource Boundaries

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-017 hardened the goal loop around the task-level approval seam, but the
approval decision lived on the task object (mirror records) — there was no
durable, queryable, restart-safe QUEUE that an independent interface (CLI,
future UI) could list and resolve across processes. Arion also had only
filesystem-style resource kinds: `filesystem:path` (and git.log reusing it).
Before any side-effecting capability lands, two things are required:

1. A persistent approval queue: exactly one durable request per
   task/step/authorization-fingerprint, resolvable across process boundaries,
   with fail-closed typed errors and bounded audit events.
2. A SECOND resource kind (`url`) with its own boundary implementation, to
   prove authorization is genuinely resource-kind based, plus a read-only
   `http.get` capability behind an injectable transport.

Plus two hardening items: the authorization fingerprint must cover
security-relevant parameters (Phase D), and the planner's
`required_capabilities` contract must be explicit and fail closed (Phase E).

## Decision

### Phase A — Persistent approval queue

- **Domain model** (`arion/state/approvals.py`): `ApprovalRequest` with
  approval_id, task_id, step_index, goal_id, capability, action, resolved
  scope, risk, side effects, resource kind/resource, a bounded human-readable
  summary, status (`PENDING | APPROVED | DENIED`), requester actor + actor
  chain, param KEY names, the canonical authorization fingerprint, decision
  actor/timestamps, created/updated timestamps.
- **Storage protocol** (`ApprovalStore`) + `SQLiteStorage` implementation
  (`approval_requests` table, same DB file). The CLI talks to the queue
  through `engine.approval_store` / `engine.resolve_approval_request`, never
  directly to SQLite.
- **Queueing:** when authorization returns `REQUIRE_APPROVAL` and the handler
  returns `PENDING`, the engine creates exactly ONE durable request per
  (task_id, step_index, fingerprint); repeated calls are idempotent — a
  pending request with the same fingerprint is reused, never duplicated; the
  awaiting task short-circuits in `run_task` (no re-request, no re-execution,
  no duplicate checkpoints). `approval.requested` + `approval.queued` are
  emitted once per queued request.
- **Resolution:** `resolve_approval_request(approval_id, outcome, actor)`
  resolves the durable record; `resolve_approval(task_id, ...)` remains for
  backward compatibility and delegates to the queue. APPROVED reuses the
  existing exact-step resume path (task → RUNNING, goal unblocked, live
  re-authorization at resume). DENIED produces a durable, explainable
  `approval denied` failure.
- **Fail closed** (`ApprovalError`): unknown approval id, already-resolved
  request, request whose task/step no longer awaits it, stale authorization
  fingerprint (the capability is never executed), denied remains denied.
- **Audit:** `approval.queued`, `approval.granted`, `approval.denied`
  (bounded metadata; no prompts/secrets/raw model output). No
  `approval.expired` (expiration is NOT implemented).

### Phase B — CLI approval interface

`arion approvals list [--status] [--json]`, `show <approval_id> [--json]`,
`approve <approval_id> [--actor ...] [--json]`, `deny <approval_id>
[--actor ...] [--json]` — all against the same persistent DB as the engine,
so `process A → request → exit; process B → list/approve → exit; process C →
resume → completion` works across real process boundaries (the DoD demo runs
the CLI as a subprocess). No GUI, no notification service. `--actor` is
audit-only and never changes authorization identity.

### Phase C — Generalized resource boundaries + `http.get`

- **`UrlBoundary`** (policy layer, kind `url`): explicit allowed-origin
  allowlist (normalized: lowercase host, default ports dropped);
  `allows(url)` is False for malformed URLs, embedded credentials, non-HTTP(S)
  schemes, and hosts outside the allowlist. Fail closed: a `url`-kind action
  with NO configured boundary is DENIED.
- **`HttpGetCapability`** (capability layer): read-only GET only. The policy
  validates the resource boundary; the capability performs the request through
  an INJECTABLE transport (`HttpTransport` protocol; `StdlibHttpTransport`
  default, `FakeTransport` in tests — no external network in the suite).
  Capability-level containment: http(s) only, no credentials, bounded response
  size (`max_bytes`) and bounded timeout, and REDIRECTS can never escape the
  configured origin (or the initial request's origin when no allowlist is
  configured) — an escaped redirect raises `CapabilityError` and the target is
  never fetched. Full `ActionSpec` metadata (scope `http:get`, risk low,
  read-only, `resource_kind="url"`, `resource_param="url"`, param_schema,
  default verification `schema_keys [status, body]`).
- Bootstrap registers `http.get` DISCOVERABLE but with NO `url` boundary by
  default → denied (fail closed); an operator configures `UrlBoundary` to
  enable access.

### Phase D — Canonical authorization fingerprint

`_authz_fingerprint` now covers:

```
capability, action, required_scope, risk, side_effects,
resource_kind, resource, security_relevant_params
```

`security_relevant_params` are declared per ActionSpec
(`ActionSpec.security_relevant_params: list[str]`); the resource parameter is
always covered via `resource`. Operational parameters (limits, formatting,
verification args) are NOT fingerprinted unless declared — avoiding
unnecessary incompatibility. Changing any fingerprinted field (including a
security-relevant param) after approval forces fresh authorization; the
capability is never executed under a stale approval.

### Phase E — Planner contract (fail closed)

`Planner` (protocol) now requires `required_capabilities(goal_description) ->
set[str]` (typed). `DeterministicPlanner` and `RealModelPlanner` implement it
via a shared deterministic heuristic (`http.get` / `git.log` /
`filesystem.read`). The engine's missing-capability gate:

- planner lacks `required_capabilities` (or errors on it) → the goal is
  durably BLOCKED with a `planner_contract` blocker — never planned, never
  executed (no silent `hasattr` no-op);
- planner declares a capability not in the LIVE registry → BLOCKED
  (`missing_capability`); when the capability appears the goal unblocks and
  replans through the normal path;
- a model-produced plan referencing an unregistered capability is rejected by
  `PlanValidator` (`PlanCapabilityValidationError`) before execution.

## Consequences

- Approvals are durable, queryable, resolvable across processes, idempotent,
  and fail closed.
- Authorization is proven resource-KIND based (`filesystem:path` and `url`
  boundaries), with network access strictly contained and offline-testable.
- The fingerprint is explicit and documented; mutating capabilities can be
  added safely later.
- The planner contract is explicit; a non-conforming planner cannot silently
  bypass the capability gate.
- All previous suites remain green (372 passed, 2 skipped); DoD demos pass
  offline.

## Not built yet (by decision)

Approval expiration, GUI/notification infrastructure, POST/PUT/DELETE or
arbitrary networking, write capabilities, per-origin rate limiting, TLS
certificate pinning.
