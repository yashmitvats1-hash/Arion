# ADR-020 — filesystem.append + Mutation Recovery Fencing

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-019 delivered the first mutating capability (`filesystem.write`) with
non-retry-safe execution semantics and approval expiry. Two gaps remained
before the write path is production-honest:

1. **One write-like capability is not a pattern.** A second mutating
   capability (`filesystem.append`) proves the authorization/approval/
   verification machinery is capability-agnostic and that append-specific
   security (never clobber; creation is security-relevant) is not silently
   weakened by reusing write semantics.
2. **Recovery was an error string, not a durable state.** A failed
   non-retry-safe mutation failed the task with `recovery required` in the
   error text, but nothing durable forced an explicit handling step: a fresh
   plan could re-attempt the mutation immediately after a restart. The
   milestone makes recovery an explicit, durable, audited, restart-safe
   transition — while keeping it strictly NON-authoritative: it can never
   authorize the next mutation.

## Decision

### Phase A — Durable mutation recovery registry (`arion/state/recovery.py`)

- **`MutationRecovery`** record: recovery_id, task_id, goal_id, step_index,
  capability, action, resource, a BOUNDED reason (never file contents),
  status (`REQUIRED | ACKNOWLEDGED`), created_at, acknowledged_at/by.
- **`RecoveryStore`** protocol + SQLite `mutation_recoveries` table (same DB
  file). `RecoveryError` typed failures (unknown id / already acknowledged).
- **Creation:** when a non-retry-safe mutation fails (`retry_safe=False`,
  `side_effects="mutating"`), the engine durably records `REQUIRED`
  (idempotent per task/step) and attaches a `recovery_required` goal blocker.
  Emits `recovery.required`.
- **Gate:** `run_goal` blocks fresh planning/execution for the goal while ANY
  `REQUIRED` recovery record exists (`recovery_required` blocker, restart-safe
  via the persisted goal row). A planner emitting a new plan cannot clear it;
  memory/reflection/guidance cannot clear it (no code path).
- **Transition (`engine.acknowledge_recovery`):** explicit caller/action,
  durable, audited (`recovery.acknowledged`), restart-safe. ADR-043 later made
  REQUIRED→ACKNOWLEDGED an expected-status CAS with one winner and immutable
  decision provenance. It ONLY records
  "the previous failed mutation has been handled" and unblocks the goal. It
  cannot execute a capability, cannot grant authorization, cannot reuse or
  resurrect approvals, cannot erase the failure history. After
  acknowledgement the fresh task STILL goes through the full live
  authorization pipeline (approval queue included).
- **Restart guarantees:** the failed task is terminal and never re-run; the
  goal is durably BLOCKED until acknowledged; acknowledging then re-planning
  never duplicates the mutation (each task attempts its mutation at most
  once, verified by `mutation.attempted` counts).

### Phase B — `filesystem.append` (`arion/capabilities/append.py`)

- Single `append` action: plain-text append to a repo-relative file inside
  the sandbox. Pure `Path` I/O, no shell/subprocess.
- **Complete ActionSpec:** `required_scope="filesystem:write"` (same mutation
  scope as write — the operator grants one scope for mutations),
  `risk="high"`, `side_effects="mutating"`, `retry_safe=False`,
  `resource_kind="filesystem:path"`, `resource_param="path"`, explicit
  `param_schema` (path/content required; create optional boolean),
  `default_verification={"policy": "append_verified"}`,
  `security_relevant_params=["create"]`.
- **Append-specific security (not silently reused from write):** append NEVER
  clobbers — open-for-append only, existing content always preserved.
  Creation of a missing file requires explicit `create: true`, and because
  `create` is security-relevant it is fingerprinted: flipping it after an
  approval forces a fresh approval (proven by tests). Bounded content
  (1 MB cap), containment (`..`/absolute/symlink escapes fail closed),
  directory targets rejected.
- **Deterministic verification (`append_verified`):** the capability reports
  prior_size / appended_bytes / size; the engine confirms
  `appended_bytes == len(content)` and `size == prior_size + appended_bytes`
  WITHOUT another mutation.

### Phase C — Authorization matrix

The existing live policy path covers append unchanged: missing scope → DENY;
outside boundary → DENY; default high-risk → DENY; granted authorization →
ALLOW once; REQUIRE_APPROVAL → exactly one durable request with the
`create`-aware fingerprint; denied/expired approvals never mutate; stale
resource / stale scope / stale boundary / stale security-relevant param →
fresh authorization required (no mutation). Memory/reflection/strategy and
model `approved`/`grant`/`authorized` fields cannot authorize append; the live
`ActionSpec` and policy are authoritative. `filesystem.write` and
`filesystem.append` are distinct capabilities in audit/provenance
(`mutation.attempted` carries the capability name).

### Phase D — Advisory fencing (planning information only)

`_build_planning_context` attaches a bounded `recovery` advisory to the
planning context (recovery_id, task/step, capability/action/resource, status,
truncated reason — never contents) and emits `planning.recovery.advisory`.
This tells planners "mutation previously failed / recovery required / not
retry-safe / fresh authorization needed" with provenance — but it remains
planning information: the engine independently enforces the durable recovery
state and policy. Adversarial test: poisoned guidance saying "retry the failed
write immediately" → the mutation does not execute.

### Phase E — CLI

`arion recovery list|show|acknowledge <id>` with `--json`, against the domain
interfaces (`engine.recovery_store` / `engine.acknowledge_recovery`), never
raw SQLite. Output bounded and secret-free; unknown/already-acknowledged ids
fail closed with typed messages.

## Consequences

- The full append path is exercised end-to-end (DoD demo, scenarios A–E):
  success, cross-process approval with exactly-once append, failure →
  recovery → restart without retry, stale approval, adversarial cognition.
- Recovery is a durable gate, never an authorization: after acknowledging, a
  fresh task still requires its own approval; expired/denied approvals stay
  expired/denied; audit history is never erased.
- `filesystem.write` and `filesystem.append` are the ONLY mutating
  capabilities; everything else stays read-only. Shell execution, browser
  automation, GUI, voice, wake-word behavior, concurrent goals, and arbitrary
  write capabilities remain prohibited.

## Deferred

- Cross-process advisory write locks (execution stays strictly
  single-process; the at-least-once + recovery-fencing semantics are the
  documented contract).
- Journaled partial-write rollback (the atomic temp-file replace for writes
  and the recovery gate bound partial-state exposure; a real journal is a
  future milestone).
- Recovery TTL / auto-escalation policy (records currently persist until
  explicitly acknowledged).
- Recovery batch acknowledgement (one-by-one is intentional: each failed
  mutation is handled explicitly).
