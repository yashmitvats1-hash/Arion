# ADR-021 — Cross-Process Advisory Mutation Locks

- **Status:** Approved & implemented (2026-08-10)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-019/020 delivered two mutating capabilities (`filesystem.write`,
`filesystem.append`) with non-retry-safe execution, durable recovery fencing,
and approval expiry — all correct within a SINGLE process. But Arion is
already operated as multiple independent processes against one SQLite DB (the
approval CLI and the runner are separate processes; demos spawn real
subprocesses). Without coordination, two processes could authorize (through
their own approval paths) the SAME security-relevant resource and mutate it
concurrently or interleaved — duplicating appends, racing writes, and
confusing recovery state.

This milestone adds the smallest cross-process coordination layer: a durable
**advisory mutation lock** keyed by the canonical security-relevant resource,
held only around the actual mutation window, with leases so a crashed owner
never permanently wedges a resource. Concurrency is still NOT introduced —
execution remains strictly sequential per process; the lock only prevents
two processes from mutating the same resource at the same time.

## Decision

### Phase A/B — `MutationLock` + `MutationLockStore` (`arion/state/locks.py`)

- **`MutationLock`** record: lock_id, resource_kind, resource (CANONICAL),
  capability, action, owner_id, acquired_at, expires_at. Bounded identifiers
  only — never file contents, prompts, or secrets.
- **Lock identity:** `(resource_kind, canonical_resource)`. For
  `filesystem:path` the canonical form is `os.path.normpath` of the relative
  path, so `./notes.txt`, `notes.txt` and `a/../notes.txt` all contend on the
  same lock — and `filesystem.write` and `filesystem.append` contend because
  they mutate the same underlying resource. Other resource kinds canonicalize
  as-is (per-kind canonicalizers can be added).
- **`MutationLockStore`** protocol + SQLite `mutation_locks` table (same DB
  file) with a UNIQUE constraint on (resource_kind, resource).
- **Atomicity:** acquisition and reclamation use `BEGIN IMMEDIATE`
  transactions — the SQLite write lock makes them atomic across processes.
  Never "check then insert" outside a transaction; never Python
  `threading.Lock`, process-local globals, or lock files. The DATABASE is the
  coordination authority. A concurrent live row fails the insert
  (IntegrityError) and is rolled back into a typed `MutationLockError`;
  `database is locked` from a busy writer also fails closed.
- **Ownership:** owner_id is explicit and unique (`proc:<pid>:<token>`).
  Release is idempotent for the owner (second release is a no-op); a
  non-owner cannot release an existing lock (typed error); an old owner
  cannot release a new owner's lock after reclamation.

### Phase C — engine integration (`ArionEngine`)

Required ordering, enforced by construction:

    plan -> authorization -> approval if required -> live re-authorization
        -> acquire mutation lock -> mutate -> verify -> release lock

- Authorization (policy + approval queue, including the live re-check on
  resume) ALWAYS runs before the lock is requested. The engine never does
  `lock -> authorize -> mutate`.
- Acquisition happens at the top of `_execute_with_retries` for mutating
  actions, immediately before the attempt loop; the lock is released in a
  `finally` on EVERY terminal path (success, mutation failure, verification
  failure, unexpected exception).
- On contention: the capability NEVER executes; the step/task fail durably
  with `mutation lock contention: ...`; the goal is durably BLOCKED with a
  `lock_contention` blocker (recheck clears it when the lock is gone, via the
  engine's live lock store); no duplicate approval requests; no replan loop;
  NO recovery record (contention is operational coordination, not a mutation
  failure). After the lock is released, the goal replans and the new task
  needs its own FRESH authorization.
- A mutating action with no lock store available fails closed.

### Phase D — release guarantees

- Success: `acquire -> mutate -> verify -> release` (released before the
  task completes).
- Mutation failure: `acquire -> mutation fails -> recovery REQUIRED ->
  release` (lock released even though the mutation may have partially
  applied; recovery recorded).
- Verification failure: `acquire -> mutation succeeds -> verification fails ->
  recovery REQUIRED -> release` (the mutation happened, the postcondition is
  unconfirmed: this is a recovery case; ADR-020's recovery gate now also
  covers verification failure of a non-retry-safe mutation).
- Authorization failure / approval pending / stale approval: the lock is
  never requested.

### Phase E — leases / stale-owner reclamation

- Every lock has `expires_at = acquired_at + lease_seconds` (engine
  `mutation_lock_lease_seconds`, default 300 s). The clock is injectable
  (`lock_clock`) for deterministic tests.
- `reclaim_expired(now, ...)` atomically deletes expired locks (scoped to a
  resource or global); `acquire` also auto-reclaims expired rows for the same
  resource inside its own transaction — a crashed owner can never wedge a
  resource. Active locks are never touched.
- `engine.reclaim_stale_locks()` sweeps all expired locks and audits each
  (`mutation.lock.reclaimed`). Administrative `reclaim_lock(id)` fails closed
  on unknown/active locks and never grants authorization.

### Phase F/G/H — write + append matrix, approval and recovery interaction

- Same resource: write-vs-append (and any two processes) contend. Different
  resources lock independently. After release, the new owner proceeds only
  through its own authorization path.
- An approval NEVER implies lock ownership: an approved task whose resource is
  locked by another process still contends at resume time. A pending approval
  never acquires or reserves a lock. Expired/denied/stale approvals never
  reach the lock.
- Recovery never clears or transfers a lock: after a mutation failure the
  lock is released, recovery stays REQUIRED across restarts, and a future
  task acquires a FRESH lock after its own fresh authorization.

### Phase I — adversarial guarantees

Memory, reflection, strategy, model output (`lock_acquired`, `approved`,
`owner`, forged lock metadata), poisoned recovery guidance, and actor-identity
claims CANNOT create, release, transfer, or bypass a lock: the engine reads
lock state only from the lock store. Proven by tests (a plan step carrying
`lock_acquired/owner/approved` still contends against a real lock; poisoned
memory creates no lock rows; guidance cannot reclaim).

### Phase J — audit events

Bounded events: `mutation.lock.requested`, `mutation.lock.acquired`,
`mutation.lock.contended`, `mutation.lock.reclaimed`,
`mutation.lock.released` — lock id, resource kind, canonical resource,
capability/action, owner id, bounded reason, timestamps. Never file contents
or full arbitrary parameters. Ordering makes the mutation lifecycle
explainable: requested → acquired → attempted → (succeeded|failed) →
released, with contended where applicable.

### Phase K — CLI

`arion locks list|show <lock_id>|reclaim <lock_id>` with `--json`, via the
domain store/engine interfaces (never raw SQLite). Reclaim is fail-closed
(active locks and unknown ids are rejected) and audited; it removes a stale
coordination record only — it never authorizes a mutation.

### Phase L — DoD demo

`scripts/demo_adr021_lock_two_process.py` runs TWO REAL subprocesses sharing
one SQLite DB (`scripts/_lock_demo_worker.py`): contention (A holds the lock,
B contends and never mutates, then proceeds after release via fresh
authorization); stale owner (A crashes holding the lock, B reclaims after the
lease, authorizes, mutates, verifies); approval+lock (A queues approval and
exits, B approves and restarts, live re-authz, one mutation); mutation
failure (acquire → fail → recovery REQUIRED → release → restart → no
duplicate, no wedged lock); adversarial (poisoned memory/model claims cannot
bypass the real lock store).

## Consequences

- Two independent processes cannot simultaneously mutate the same canonical
  resource; acquisition is atomic; contention never executes the capability;
  stale locks are reclaimable; locks cannot grant authorization; approvals
  and recovery cannot grant lock ownership; memory/cognition/strategy/model
  output cannot forge lock state.
- `filesystem.write` and `filesystem.append` remain fully green; no
  shell/subprocess was introduced in any capability; no raw content/secrets
  enter lock or audit records.
- Explicit statements:
  **A mutation lock is coordination, not authorization.**
  **Authorization is evaluated independently for every mutation attempt.**
- ADR-045 later reuses the same proven lease table under the disjoint internal
  `arion:goal-run` resource kind to serialize long-horizon runners for one goal.
  That internal lease is also coordination only and never substitutes for the
  capability mutation-resource lock or authorization.

## Deferred

- True concurrency (parallel goal execution within a process) — still
  explicitly deferred; the lock makes cross-process coordination safe, not
  concurrent execution.
- Distributed locks (Redis/etcd) — SQLite transactions are sufficient while
  the DB is the single coordination authority.
- Heartbeats: leases with the injectable clock are the bounded staleness
  mechanism; active-owner heartbeat renewal is not needed yet (execution
  windows are short; leases are generous and reclaimable).
- Lock acquisition backoff/wait-queue semantics: contention currently fails
  the task durably (blocker); a future milestone could add bounded waiting.
