# ADR-048 — Latest-Plan Completion Authority

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Prevent superseded task success from completing newer unimplemented plans

## Context

Arion persists immutable goal plan versions before or independently from their
implementing task. This is intentional: a crash after plan commit can reconstruct
the task from the stored plan. Goal completion must therefore distinguish
historical task outcomes from work implementing the latest plan.

`DeterministicProgressEvaluator` counted succeeded and skipped steps across all
tasks, then compared that total with the number of steps in the latest plan.

The audit baseline was **1,415 passed, 2 skipped** at local and remote commit
`38c8f13`.

## Reproduction

1. Goal plan version 1 had one completed task with one succeeded step.
2. A different version 2 was durably recorded.
3. No task existed for version 2, simulating a crash after plan commit.
4. Goal evaluation and `run_goal()` resumed.

Observed:

```text
plan versions: [1, 2]
latest plan tasks: 0
evaluation action: complete
reason: all_work_complete
historical handled steps credited: 1
goal: completed
latest capability calls: 0
tasks total: 1
```

The latest immutable plan became terminal without task creation or execution.

## Root cause

- Historical succeeded/skipped step counts were completion authority.
- Completion compared a raw count with latest plan length.
- Duplicate historical step indices could inflate the count.
- The latest-task resume rule prevents false completion only after a latest-plan
  task already exists.
- Plan-before-task crash recovery has no such task yet.

## Invariants

1. Only tasks implementing the latest immutable plan version can authorize goal
   completion.
2. Superseded successes/failures remain observable history, not current
   completion authority.
3. Every distinct expected latest-plan step index must be SUCCEEDED or SKIPPED.
4. Duplicate task/step rows cannot satisfy multiple expected indices.
5. A committed latest plan with no implementing task remains outstanding and is
   reconstructed by the existing stored-plan path.
6. Unversioned tasks remain a legacy fallback only when no exact latest-version
   task exists.
7. A pending exact latest task still takes priority over replan/completion.
8. Per-goal leases, task CAS, scheduler ownership, pause/terminal boundaries,
   approvals, recovery, locks, and checkpoints remain unchanged.

## Decision

### Authoritative latest task set

For a current plan version `V`, progress evaluation selects:

```text
exact tasks where task.plan_version == V
```

If at least one exact task exists, only those tasks participate in current
resume/failure/handled decisions. If no exact task exists, unversioned tasks are
accepted as the legacy compatibility set. Tasks from older explicit versions
never participate.

### Distinct step-index completion

The evaluator derives expected step indices from `latest_plan.plan_summary` and
collects unique SUCCEEDED/SKIPPED indices from the authoritative task set.
Completion requires:

```text
non-empty valid expected index set
all expected indices handled
no authoritative latest task failed
```

A raw historical count can no longer satisfy a different index or version.
Malformed/duplicate expected indices fail to complete.

### Evidence and progress

Historical task/completed/failed/succeeded/skipped totals remain in evidence for
observability. New bounded fields expose current authority:

```text
latest_plan_version
latest_plan_tasks
latest_succeeded_steps
latest_skipped_steps
latest_handled_steps
```

Progress against a latest plan uses latest succeeded indices. No-plan legacy
progress retains historical behavior.

### Resume and failure scope

Latest resumable and failed-task decisions use the same authoritative task set.
An unversioned stale row cannot override an exact latest implementation;
superseded explicit versions remain non-blocking.

## Compatibility

- No schema, plan, task, checkpoint, or event format changes are required.
- Existing evidence keys remain; new keys are additive.
- Legacy unversioned tasks remain supported when no exact task exists.
- Stored-plan task reconstruction and immutable plan history are unchanged.
- Goal/task method signatures and return types are unchanged.

## Explicit limitations

- This decision does not merge or delete historical duplicate tasks.
- Legacy unversioned rows are inherently less precise and are used only as a
  fallback.
- Plan summaries with malformed/duplicate indices remain non-completable until
  repaired/replanned; they are never guessed into completion.
- No event sourcing, workflow engine, or plan schema redesign is introduced.

## Test strategy

Focused tests prove:

1. superseded success cannot complete a newer plan with no task;
2. `run_goal` reconstructs and executes the missing latest stored plan once;
3. historical successes cannot inflate partial latest progress;
4. duplicate latest step indices cannot satisfy distinct expected steps;
5. unversioned legacy task completion remains compatible;
6. superseded failures, replanning, stored plans, goal leases, task CAS,
   scheduler, approvals, recovery, checkpoints, and full suites remain green.

## Verification

- Read-only baseline: **1,415 passed, 2 skipped**.
- ADR-048 latest-plan completion tests: **4 passed**.
- Focused progress, goal/replanning, stored-plan, lease, task, concurrency,
  approval, recovery, scheduler, lock, checkpoint, and event regressions:
  **224 passed**.
- Complete suite after implementation: **1,419 passed, 2 skipped**.
