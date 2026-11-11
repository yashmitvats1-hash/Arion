# ADR-064 — In-doubt replay-unsafe mutation fencing (issue #23 / F10)

- **Status:** Approved & implemented (2026-11-10) on `arena/8f47cc1a-arion`
  (PR #25).  The behavior delta it mandates was ordered by issue #23 itself
  and is recorded in the PR body.
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)
- **Reconciles:** ADR-010 (execution semantics), ADR-020 (append + mutation
  recovery fencing), ADR-026 (cross-process scheduler crash recovery).
  This ADR is the targeted reconciliation; the three ADRs are not altered
  retroactively beyond a one-line amendment pointer each.
- **Related:** ADR-018 (durable approval queue), ADR-043 (recovery CAS +
  decision provenance), ADR-045/052 (goal-run leases), ADR-056 (atomic
  terminal transition), ADR-060 (verification authority).  Implementation:
  `arion/state/intents.py`, `arion/state/store.py` (mutation-intents
  journal), `arion/orchestration/engine.py` (dispatch journaling, in-doubt
  fence, recovery-capability invariant).

---

## Context

ADR-010 established at-least-once step execution: a step interrupted by a
crash may have partially or fully executed, so its result is unknown and it
may run again.  ADR-010's consequence list said "crash + resume re-executes
exactly once", and ADR-020's restart guarantees said "each task attempts its
mutation at most once ... verified by `mutation.attempted` counts".  ADR-026
guaranteed that "completed mutations never replayed".  Those clauses leave
the dangerous middle case under-specified: a non-retry-safe mutation that
was DISPATCHED (side effect may exist) but whose outcome was never
persisted.  Blind replay of such a step can duplicate an append or
re-apply a non-idempotent write; refusing to ever re-run it can strand the
work forever.

Issue #23 (F10) demanded a narrow, explicit rule for exactly this case -
and independent reviews of PR #25 demanded a second, related guarantee: a
journaled replay-unsafe mutation must not execute at all unless the engine
has a usable recovery mechanism to fence it with after a crash.

## Decision

### The narrow rule

1. **In-doubt** means: a replay-unsafe (`retry_safe=False`) MUTATING action
   whose mutation intent was durably journaled before dispatch and whose
   durable outcome is unknown after interruption (the journal row is still
   open and the step has no terminal outcome).
2. **An in-doubt non-retry-safe mutation is NEVER blindly replayed.**  On
   resume the engine fences it before any dispatch:
   - with a usable recovery mechanism, a durable `REQUIRED` recovery record
     is created and the task fails closed (ADR-020 semantics);
   - without one, the task fails terminal with the mutation-intent row left
     OPEN as durable evidence - nothing is replayed and the mutation is
     never marked complete.
3. **Re-execution happens at most once after the fence, in a NEW task**, and
   only after an explicit `acknowledge_recovery` - under full live
   authorization (fresh approval included).  Each task still attempts its
   mutation at most once; the fence is what makes that claim true again.

### What remains available and unchanged (explicitly preserved)

- **Retry-safe replay** (`retry_safe=True`): re-running after a crash or
  failure is the declared contract (ADR-010 rule 3).  These actions are not
  journaled and are not gated by the recovery-capability invariant.
- **Live authorization** for every new execution attempt (ADR-009 et seq.).
- **Fresh approval** - recovery acknowledgement can never reuse or
  resurrect an approval (ADR-020, ADR-043).
- **Verification** semantics of every capability (ADR-060 authority).
- **Audit history** - failure records, intents, events and recovery rows
  persist; acknowledgement erases nothing.
- **Explicit recovery acknowledgement** semantics (REQUIRED ->
  ACKNOWLEDGED CAS, one winner, immutable decision provenance).

### Recovery-capability invariant

A journaled replay-unsafe mutation must NEVER execute unless the engine has
a usable recovery mechanism - one that can durably create the obligation
(`create_recovery` or `commit_recovery_requirement`), enumerate it
(`list_recoveries`), inspect it (`get_recovery`) and acknowledge it
(`transition_recovery`).  The dependency is enforced BEFORE dispatch:
`_persist_mutation_intent` refuses the dispatch (and writes no journal row)
when the mechanism is missing, so a crash can never leave a side effect
with no way to fence it.  A registry whose API is merely unreadable at
resume time keeps the historical fail-closed verify behaviour.

### What is NOT claimed

- **`append_verified` is a size-arithmetic postcondition only**
  (`prior_size + appended_bytes == size`).  It is NOT an independent
  duplicate-execution detector.  Duplicate prevention is the mutation-intent
  journal plus the in-doubt fence described here.

## Reconciliation

| Clause | Before | After this ADR |
| --- | --- | --- |
| ADR-010 "at-least-once execution" | Steps may run again after interruption | Unchanged for retry-safe actions.  For replay-unsafe MUTATIONS "run again" means "re-run in a new task after explicit recovery acknowledgement", never blind replay of an in-doubt step. |
| ADR-010 "crash + resume re-executes exactly once" | Read as: the step re-executes on resume | Superseded for replay-unsafe mutations: an in-doubt mutation is fenced, not re-executed, on resume.  Exactly-once re-execution is restored only through the recovery-acknowledgement path. |
| ADR-020 "each task attempts its mutation at most once" | Violated by blind resume replay of an unsettled intent | Repaired: the fence guarantees it.  The old task is terminal and never re-run; the post-ack task carries its own single attempt. |
| ADR-026 "completed mutations never replayed" | True for COMPLETED mutations only | Strengthened: IN-DOUBT (uncompleted, outcome-unknown) mutations are never blindly replayed either; only explicit recovery processing permits re-execution. |

## Operationalization

- **Journal** (`arion/state/intents.py`, `mutation_intents` table): written
  BEFORE dispatch for mutating non-retry-safe actions; settled only after a
  durable terminal outcome or explicit recovery processing.
- **Fence** (`_fence_task_on_in_doubt_mutation`, at task resume and at the
  `_execute_step` dispatch choke point): in-doubt steps are converted to
  recovery obligations (or terminal fail-closed with the intent left open)
  instead of being re-dispatched.
- **Dispatch guard** (`_persist_mutation_intent`): enforces the
  recovery-capability invariant before the journal row exists.
- **Tests:** `tests/test_mutation_crash_integrity.py` (journal ordering,
  crash-window, choke point), `tests/test_mutation_recovery_invariant.py`
  (alternate-storage configurations, no-mechanism fail-closed),
  `tests/test_scheduler_restart.py` and `tests/test_checkpoint_retention.py`
  (the operational recovery-gated resume flows; their hard-crash scenarios
  assert that nothing replays).

## Consequences

- The reclaimed replay-unsafe in-doubt mutation executes exactly once after
  explicit `acknowledge_recovery` instead of being blindly re-executed.
  This is a MANDATED behavior change (issue #23), documented in PR #25.
- Engines configured with an intent journal but no recovery registry can no
  longer dispatch replay-unsafe mutations at all (fail closed, no side
  effect).  Retry-safe work is unaffected.
- No other execution semantics change: retry-safe retries, authorization,
  approvals, verification, locks and scheduler ownership are untouched.
