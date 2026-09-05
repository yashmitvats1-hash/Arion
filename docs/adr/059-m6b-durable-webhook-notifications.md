# ADR-059: M6-B — Durable Webhook Notifications

- **Status:** Accepted (Revision 3), implemented in M6-B
- **Date:** 2026-09-05
- **Supersedes:** none
- **Amends:** none — **ADR-038 is explicitly NOT amended**
- **Related:** ADR-007 (audit events), ADR-028 (scheduler leases), ADR-032
  (runtime ownership), ADR-033 (required vs. best-effort sinks), ADR-034
  (error boundary), ADR-038 (event emission guarantees), ADR-058 (M6-A
  authenticated approval API)

Labels used below: **[FACT]** = verified property of this repository,
**[DECISION]** = the choice being made, **[RATIONALE]** = why,
**[IMPL]** = implementation detail that follows from a decision.

---

## 1. Context

**[FACT]** Arion emits a closed vocabulary of `AuditEvent`s
(`arion/observability/events.py`), persisted through `SQLiteStorage`
(registered as a *required* sink) and optionally mirrored to JSONL (a
*best-effort* sink). `EventLogger` isolates best-effort sink failures and
records them in `last_failures`.

**[FACT]** M6-A (ADR-058) added an authenticated HTTP surface for approvals,
with a single flat token tier: possession of any valid `ARION_API_TOKENS`
credential authorizes every action.

**[FACT]** Approval-gated work blocks until a human responds. Nothing in the
system tells a human that it is waiting. Discovery is by polling.

M6-B adds durable outbound webhook notification for a curated subset of
events.

---

## 2. Decisions

### D1 — Delivery guarantee

**[DECISION]** *Best-effort capture, then at-least-once delivery.* Capture is
performed by a sink registered with `required=False`. Once a delivery row is
committed, the system retries until success, exhaustion, or a permanent
rejection.

**[RATIONALE]** Notification is strictly downstream of orchestration. A
subscriber's storage problem must never fail an agent task. Conversely,
promising exactly-once over an unreliable network to an endpoint we do not
control would be a guarantee we cannot keep. Duplicates are expected;
receivers must be idempotent.

**[DECISION]** ADR-038 is **not** amended. No new emission guarantee is
introduced; M6-B consumes the existing one.

### D2/D3 — Capture path and coupling

**[DECISION]** No transactional coupling between event emission and delivery
enqueue. **[FACT]** `append_event` already commits outside the caller's
transaction, so a claim of transactional capture would be false. Capture is
one bounded `INSERT` on the emitting thread, with no I/O.

### D4 — Event eligibility

**[DECISION]** A **separate allowlist**, not `EVENT_KINDS`.

- Tier 1: `approval.requested`, `approval.queued`, `approval.expired`,
  `goal.approval.pending`, `goal.approval.expired`
- Tier 2 (opt-in): `approval.granted`, `approval.denied`,
  `goal.approval.granted`, `goal.approval.denied`, `goal.blocked`,
  `goal.unblocked`
- Deferred: `task.completed`, `task.failed`, `recovery.required`

**[DECISION]** No wildcard subscriptions. **[DECISION]** Structural exclusion
of any `webhook.*` kind.

**[RATIONALE]** Auditability and notification are different concerns.
Coupling them would make every future audit kind an externally subscribable
signal by accident.

### D5 — Payload

**[DECISION]** The wire body is **never** `AuditEvent.to_dict()`. External
envelope:

```json
{"schema_version","delivery_id","event_id","event_kind","occurred_at","sequence","payload"}
```

`subscription_id` is deliberately **absent**. Each eligible kind has an
explicit detail projection; unlisted keys are dropped. The body is serialized
once at enqueue, stored as `body_bytes`, and transmitted byte-identically
forever; the HMAC is computed over exactly those bytes.

### D6/D7/D8

`sequence` is an enqueue-order identifier only, from an explicit counter (not
rowid). No `webhook.*` kinds are added to `EVENT_KINDS`. **No ordering
guarantee.**

### D9 — Delivery state machine

States: `pending`, `delivering`, `delivered`, `failed`, `dead_letter`,
`cancelled`. Claiming is `BEGIN IMMEDIATE` + conditional `UPDATE` + rowcount
(never check-then-act). Every transition is fenced on (owner, live lease).
**No HTTP is ever performed while holding `_sql_lock`.**

**[DECISION]** The `scheduler_work` lease pattern is a **precedent only**;
its tables and machinery are never reused. Third-party endpoint latency must
not enter the agent's scheduling substrate.

### D10 — Retry

Retryable: 5xx, 429, transport errors, deadline exhaustion. Permanent: other
4xx, 3xx (redirects are never followed), destination policy rejection.
Exhaustion yields `failed` (manual retry still meaningful); permanent
rejection yields `dead_letter`.

**D10.1 [FACT] Exact arithmetic at defaults:** 8 attempts → 7 delays of
5/10/20/40/80/160/320 s = **635 s** backoff, plus 8 × 10 s attempts =
**≈715 s (11 m 55 s)** worst case. The 3600 s cap is unreached at defaults
and first binds after attempt 11.

Manual retry resets `attempts` to 0 and is permitted only while
`now <= retry_eligible_until`.

### D11 — Signing and secrets

Per-subscription HMAC-SHA256, versioned secrets, returned exactly once at
create and rotate.

**[DECISION]** Persisting signing secrets is a deliberate exception to
"Arion holds no long-lived credentials". It is **not** equivalent to LLM API
credentials: those authenticate us to a provider, whereas these authenticate
*us* to a receiver and cannot be replaced by a request-time secret without
destroying the retry guarantee.

**D11.3 — `retry_eligible_until`** is the single persisted horizon
reconciling retry, retention, deletion and secret lifetime.

> **Invariant:** a secret version's material remains available for as long as
> any retained delivery referencing it still has retry capability.

Retry capability = non-terminal **OR** (`failed`/`dead_letter` **AND**
`now <= retry_eligible_until`). Secret destruction is conditioned on a
*derived reference count of zero*, never on a timer. Maintenance order is
**prune → retire**. `MAX_SECRET_VERSIONS` = 8; a blocked rotation returns 409
naming the earliest clearing timestamp.

**D11.4 [DECISION]** SQLite-file compromise is **out of the threat model**.
An attacker with the database file already has the audit trail and approval
state; webhook secrets add no new capability.

### D12 — Network policy

HTTPS only, **no plaintext exception**. No redirect following. Mandatory
operator origin allowlist, checked at create **and every attempt**. Literal
IPs rejected; resolved addresses screened against private/loopback/
link-local/reserved ranges.

**[DECISION]** A dedicated transport, never `capabilities/http.py`: that is a
model-reachable agent capability, and any relaxation made for agent browsing
must not widen the SSRF surface of an unattended background sender.

**D12.2 — `TIMEOUT_SECONDS`** = maximum wall clock of one complete attempt
(DNS → TLS → send → response → bounded body read), enforced by a monotonic
deadline with remaining-budget socket timeouts. **[FACT]** The stdlib socket
timeout is *per blocking operation*, not per request; residual overrun is
therefore bounded by one blocking operation's return latency. This is
documented rather than overclaimed, and is why the lease adds a fixed margin.

**D12.4** DNS rebinding is narrowed, not eliminated.

### D13 — Authorization

**[DECISION]** A separate admin credential surface, `ARION_API_ADMIN_TOKENS`.
`ARION_API_TOKENS` keeps its **exact existing grammar and meaning**
(approver). A role-bearing grammar (`token:kind:name:role`) is **rejected**:
it makes escalation a one-word edit and silently changes deployed tokens.

One `AuthContext`/`Privilege` abstraction, privilege declared per route, a
single enforcement point, 401 distinct from 403, admin ⊃ approver, dual
membership is a configuration error, empty admin map fails closed.

### D14 — Configuration

Environment-only, disabled by default. Defaults: timeout 10 s, max response
8192 B, max attempts 8, backoff 5 → 3600 s, lease 60 s, poll 5 s, worker
concurrency 1, page size 50/200, retention 7 d delivered / 30 d failed, max
secret versions 8.

**D14.1** Relationships are **rejected at load, never clamped** — notably
**`LEASE_SECONDS >= TIMEOUT_SECONDS + 5`** (50 s margin at defaults),
`BACKOFF_CAP >= BACKOFF_BASE`, `PAGE_DEFAULT <= PAGE_MAX`,
`RETENTION_FAILED >= RETENTION_DELIVERED`, `MAX_SECRET_VERSIONS >= 2`, and a
non-empty allowlist whenever enabled.

**Security bounds are configuration-only and can never be widened through the
API.**

### D15 — HTTP surface

All webhook endpoints require `ADMIN`. Category A hardening applies to this
new surface: exact routing, PATCH/DELETE, pagination, sanitized errors,
centralized authorization, validated query parameters, explicit projections.
`tests/test_approval_api.py` is unmodified. Category B (retrofitting M6-A
itself) is deferred to **M6-A.1**.

**D15.4** Delivery history is **liveness-independent**:
`GET /api/v1/webhooks/{id}/deliveries` returns `200` with
`subscription_exists: false` when history remains, `404` when none does
(never-existed and fully-pruned are deliberately indistinguishable);
`GET /api/v1/deliveries/{id}` and `POST /api/v1/deliveries/{id}/retry` work
after deletion while the key is retained.

### D16/D17 — Deletion and retention

Deletion marks the subscription deleted, cancels `pending` deliveries, and
moves live secret versions to `retiring` — **destroying nothing**. Retention
is asymmetric, and a row is never pruned while `now <= retry_eligible_until`.

---

## 3. Rejected alternatives

- **R1 — Transactional outbox coupled to the caller's transaction.** Rejected:
  `append_event` already commits independently; the coupling would be
  illusory and would put notification on the critical path.
- **R2 — Reuse `scheduler_work`.** Rejected: it schedules agent work with a
  Task and Actor and feeds capacity accounting. Webhook egress is
  infrastructure.
- **R3 — Reuse `capabilities/http.py`.** Rejected: it is an agent capability
  governed by the permission seam; sharing it couples agent browsing policy
  to unattended egress policy.
- **R4 — Role-bearing token grammar.** Rejected: see D13.

---

## 4. Invariants (26)

1. Notification never fails orchestration.
2. Capture is best-effort; committed deliveries are at-least-once.
3. The capture path performs no network I/O.
4. Delivery is never on the orchestration critical path.
5. Only allowlisted kinds are deliverable.
6. No wildcard subscriptions.
7. The external payload is versioned.
8. `body_bytes` is frozen at enqueue and never rebuilt.
9. Claiming is atomic.
10. Every transition is fenced on owner + live lease.
11. No HTTP under `_sql_lock`.
12. `sequence` is enqueue-order only and implies no delivery order.
13. Secret material is returned exactly once and never re-readable.
14. Security bounds are configuration-only.
15. HTTPS only.
16. Redirects are never followed.
17. Destination policy is enforced at create and every attempt.
18. Signature covers exactly the transmitted bytes.
19. Disabled by default.
20. Invalid configuration is rejected, never clamped.
21. `LEASE_SECONDS >= TIMEOUT_SECONDS + 5`.
22. One attempt is bounded by a monotonic deadline.
23. A secret remains available while any referencing delivery is retryable.
24. Deletion destroys no secret material.
25. No row is pruned while still retryable.
26. Delivery history remains queryable after subscription deletion.

---

## 5. Consequences

**Positive.** Humans learn about pending approvals without polling. Failures
are durable, inspectable and manually retryable. The default runtime is
byte-for-byte unchanged.

**Negative.** Arion now persists long-lived signing secrets (D11) and
performs unattended outbound network I/O (D12), both accepted with explicit
mitigations. Duplicate and out-of-order deliveries are possible by design.

**Deferred.** M6-A.1 Category B hardening; Tier-2 task/recovery events;
multi-worker concurrency.
