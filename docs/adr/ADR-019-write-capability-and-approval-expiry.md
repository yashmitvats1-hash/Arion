# ADR-019 — First Write Capability, Non-Retry-Safe Execution, Approval Expiry

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-018 delivered the durable approval queue, the canonical authorization
fingerprint, and a second (read-only) resource kind. The system still had
ZERO mutating capabilities: nothing can write a file, so the hardened
authorization/approval pipeline (`goal → planning → capability validation →
authorization → approval queue → approval → live re-authorization → mutation →
verification → completion`) had never been exercised with a real mutation.

This ADR adds the smallest real mutating capability, `filesystem.write`, and
the execution/retention semantics a mutation demands:

1. **No mutation without authorization.** Memory, cognition, reflection,
   strategy, planner output and approval summaries may inform — they never
   authorize. The capability itself never decides authorization.
2. **Non-retry-safe execution.** A failed mutation must not be blindly
   retried; it enters a durable, explainable recovery-required state; restart
   must never duplicate a mutation.
3. **Approval queue retention/expiry.** Stale PENDING requests expire into an
   explicit EXPIRED state; expired approvals cannot be resolved; audit history
   is never pruned.

## Decision

### Phase A — `filesystem.write` capability (`arion/capabilities/write.py`)

- Single action `write`: write plain-text content to a repo-relative path
  inside the sandbox. Pure `Path` I/O — **no shell, no subprocess, no
  `os.system`** (a filename containing shell metacharacters is treated as a
  plain filename, proven by test).
- **Complete ActionSpec:** `required_scope="filesystem:write"`, `risk="high"`,
  `side_effects="mutating"`, `reversible=False`, `idempotent=False`,
  `retry_safe=False`, `resource_kind="filesystem:path"`,
  `resource_param="path"`, explicit `param_schema`
  (`path`/`content` required strings, `overwrite` optional boolean),
  `default_verification={"policy": "write_verified"}`,
  `security_relevant_params=["overwrite"]`.
- **Overwrite is explicit and security-relevant:** an existing file is never
  clobbered unless `overwrite: true` is passed. Because `overwrite` is
  declared in `security_relevant_params`, flipping it after an approval forces
  a FRESH approval (fingerprint change) — proven by test.
- **Bounded input:** content is capped (default 1 MB, constructor-configurable
  for tests); oversized content is rejected with `CapabilityError` before any
  I/O.
- **Containment:** `_resolve_inside` resolves against the sandbox root and
  requires the resolved path to remain inside it — `..` traversal, absolute
  paths outside the root, and **symlink escapes** all fail closed (a symlink
  pointing outside resolves to an outside path and is rejected). The same
  pattern as the read capability.
- **Deterministic verification contract:** returns `{"written": true, "path",
  "canonical_path", "size"}` — the exact byte size. The engine's
  `write_verified` policy confirms the postcondition (reported size == utf-8
  length of the planned content) WITHOUT any second mutation.
- **Registry-discoverable** (`bootstrap` registers it) but **DENIED by the
  default policy** (`allowed_scopes` contains no `filesystem:write`): no
  mutation without an explicit operator authorization decision. Fail closed.

### Phase B — Non-retry-safe execution semantics (engine)

- `_execute_with_retries` emits a bounded **mutation audit vocabulary**:
  `mutation.attempted` (before the first attempt), `mutation.failed` (on
  capability error), `mutation.succeeded` (only after verification passes),
  `mutation.requires_recovery` (non-retry-safe failure).
- For `retry_safe=False` mutating actions, a capability failure is NEVER
  retried (`step.retrying` is never emitted for it), and the step/task fail
  durably with an explicit error:
  `mutation failed: <reason>; recovery required`.
- **Recovery requires an explicit new decision:** the failed task is terminal
  (`run_task` returns it as-is; restart never re-executes it), and the goal
  can only advance through a NEW plan version + NEW authorization request. We
  never infer "safe to repeat" from the mere fact that the previous execution
  failed.
- `write_verified` verification policy: a verification mismatch fails the task
  with `verification failed` and does NOT silently retry the write.

### Phase C — Approval expiry / queue retention

- `ApprovalStatus.EXPIRED` added to the domain model; `ApprovalRequest` gains
  `expired_at`; the `approval_requests` table gains an `expired_at` column via
  an additive migration (existing rows untouched).
- `engine.expire_stale_approvals(now=None) -> list[approval_id]` marks PENDING
  requests older than the engine's configured `approval_ttl_seconds` as
  EXPIRED (injectable clock for tests). **Idempotent**: already-EXPIRED
  requests are never touched again, so no duplicate `approval.expired` events.
- **Expired cannot be resolved:** `resolve_approval_request` rejects EXPIRED
  records with a typed `ApprovalError` ("already resolved (expired)").
- **Expiry lifecycle:** the awaiting task fails durably with
  `approval expired; recovery requires new authorization`, the goal's
  `approval_pending` blocker is cleared, and the next `run_goal` replans for
  FRESH authorization — a stale approval can never cause a mutation.
- **Audit retention:** nothing is deleted; the EXPIRED record stays queryable
  and `approval.expired` / `goal.approval.expired` are canonical event kinds.
  Cleanup/pruning must never delete audit history.
- **CLI:** `arion approvals list [--status expired]` and `show` expose the
  EXPIRED state and `expired_at`; `approve`/`deny` fail closed with a clear
  "expired" message. JSON output stays bounded and secret-free.

### Phase D — Canonical fingerprint review for write

The ADR-018 fingerprint (capability, action, resolved scope, risk, side
effects, resource kind, resource, `security_relevant_params`) already covers
everything an approval of a write must cover once `overwrite` is declared
security-relevant. Operational parameters — most importantly the **content
payload** — are deliberately NOT fingerprinted: changing the content of an
approved write does not require a fresh approval, while changing the target
resource, the scope, the risk, the side effects, or the overwrite behavior
does. Tests prove both directions explicitly.

## Consequences

- The full write path is exercised end-to-end (DoD demo, scenarios A–E):
  approved mutation with restart and exactly one write; denied mutation;
  stale-approval invalidation; non-retry-safe failure with no duplicate on
  restart; expiry.
- Authorization remains the sole authority: the capability's only role is
  containment; policy/queue decide. Poisoned memory claiming prior approval
  and model output carrying approval-like fields cannot grant write access.
- `filesystem.write` is the ONLY mutating capability; everything else stays
  read-only. Shell execution, browser automation, GUI, voice, wake-word
  behavior, concurrent goals, and arbitrary write capabilities remain
  prohibited.

## Deferred

- Advisory locks / cross-process write fencing (not needed while execution is
  strictly single-process; concurrency itself is deferred).
- Partial-write undo/rollback (the atomic temp-file replace minimizes partial
  states; a real journal is deferred).
- Expiry notification/alerting interface (the audit events are the hook).
- Configurable pruning of resolved requests WITH full audit export (retention
  policy only — audit records themselves stay).
- Other write-like capabilities (append, mkdir, delete, move) — explicitly
  out of scope until a future ADR.
