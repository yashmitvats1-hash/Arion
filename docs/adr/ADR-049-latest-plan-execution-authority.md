# ADR-049 — Latest-Plan Execution Authority

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Prevent approval or task resume from executing superseded plan work

## Context

ADR-048 scopes goal completion/progress to the latest immutable plan. Task
execution and approval resolution still validated task-local revision, step,
fingerprint, goal terminal/pause state, scheduler ownership, recovery, and
mutation locks—but not `task.plan_version` against the current goal plan.

The audit baseline was **1,419 passed, 2 skipped** at local and remote commit
`5d47df9`.

## Reproduction

1. Plan version 1 created a high-risk mutation task and PENDING approval.
2. Before decision, version 2 committed with a different mutation target.
3. The version-1 approval was approved through the normal engine API.
4. The version-1 task was directly resumed.

Observed:

```text
old plan: 1
latest plan: 2
old approval: approved
old task before resume: running
old task after resume: completed / succeeded
capability calls: 1
old target mutated: yes
new target mutated: no
latest-plan tasks: 0
goal: active
```

ADR-048 prevented the old success from completing version 2, but obsolete work
still produced an external effect.

## Root cause

- Latest-plan authority existed only in progress evaluation.
- Approval resolution did not check task plan version.
- Task worker freshness checked task revision and step status only.
- Per-goal leases serialize runners but do not select executable plan version.
- Mutation locks serialize resources, not plan intent.

## Invariants

1. Only a task implementing the latest immutable plan may begin/resume
   capability execution for a managed goal.
2. Pending approval for a superseded task is atomically DENIED with its task;
   it cannot become APPROVED.
3. A newer plan committed after task load, during authorization, or during lock
   wait wins before capability invocation.
4. A newer plan committed while capability code is already running cannot
   preempt it; the known result remains, but the superseded task becomes FAILED
   and never authorizes latest-plan completion.
5. Superseded failures remain historical and non-blocking under ADR-048.
6. Exact latest tasks remain executable.
7. Unversioned legacy tasks are accepted only when no exact latest task exists
   and the task is not older than the latest plan row.
8. Goal leases, task revisions, scheduler leases, pause/terminal checks,
   approvals, recovery, locks, and checkpoints remain independent authorities.

## Decision

### Current-plan task predicate

The engine uses `_task_implements_latest_plan(task)`:

- no GoalManager/latest plan → compatible standalone task;
- exact `task.plan_version == latest.plan_version` → current;
- explicit older/newer mismatch → superseded;
- unversioned task → current only when no exact latest task exists and its
  creation timestamp is not older than the latest plan row;
- read/lookup failure → fail closed.

### Superseded-task fence

`_fence_task_for_superseded_plan(task)`:

- leaves already-terminal history immutable;
- atomically DENIES a matching PENDING approval with actor
  `system:superseded_plan`;
- otherwise transitions the non-terminal task/active pending step to FAILED via
  task revision CAS;
- clears task lock-wait metadata and cancels queued waiters;
- emits bounded task failure provenance;
- performs no capability call or mutation.

### Approval boundary

`resolve_approval_request()` checks plan authority before handling pending or
legacy decided rows. A superseded pending request is denied with its task;
APPROVED legacy rows cannot reconcile a superseded awaiting task. The caller
receives typed `ApprovalError`.

### Execution boundaries

The fence runs:

- after task load in owned direct execution;
- at every single/bulk task execution round;
- at worker start before task-step freshness;
- after authorization/approval immediately before capability execution;
- after mutation lock wait before mutation;
- after capability return, preserving any already-started result while failing
  the superseded task.

## Compatibility

- No schema, plan, task, approval, checkpoint, scheduler, lock, or recovery
  format changes are required.
- Terminal historical tasks remain readable and immutable.
- Exact latest tasks execute unchanged.
- Unversioned tasks created after their plan remain a legacy fallback.
- Stored-plan reconstruction creates exact versioned tasks and is unchanged.
- Public method signatures and return types remain unchanged.

## Explicit limitations

- A planner/manager with direct database authority may still create new plan
  versions while another capability is already in flight; running code cannot
  be preempted.
- Superseded rows are retained for audit; this ADR does not delete or merge
  task/approval history.
- Timestamp ordering is used only for unversioned legacy fallback.
- No workflow engine, plan cancellation table, or event sourcing is introduced.

## Test strategy

Focused tests prove:

1. superseded PENDING approval is denied and cannot execute;
2. direct superseded task resume fails without effect;
3. a newer plan committed during mutation lock wait prevents the old mutation
   and clears coordination state;
4. exact latest and valid unversioned fallback tasks still execute;
5. superseded completion remains non-authoritative under ADR-048;
6. approval, recovery, goal/task leases, scheduler, lock fairness, checkpoint,
   stored-plan restart, and full suites remain green.

## Verification

- Read-only baseline: **1,419 passed, 2 skipped**.
- ADR-049 latest-plan execution tests: **4 passed**.
- Focused plan completion/execution, approval, recovery, goal/task lease, pause,
  stored-plan, scheduler, lock, checkpoint, concurrency, and event regressions:
  **286 passed**.
- Complete suite after implementation: **1,423 passed, 2 skipped**.
