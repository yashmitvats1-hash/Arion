# ADR-017 — Approval-Gated Goals, BLOCKED Semantics, Second Capability

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-009 introduced the approval seam at the TASK level (`ApprovalHandler`:
`REQUIRE_APPROVAL -> APPROVED | DENIED | PENDING`), and ADR-016 made
`GoalManager` the authoritative, restart-safe goal state machine with plan
versioning and the `run_goal` long-horizon loop. Two gaps remained:

1. `run_goal` did not understand `AWAITING_APPROVAL`: a paused task was
   treated as "pending work to continue", so the loop re-ran the awaiting task
   forever (re-requesting approval, re-checkpointing, spinning). There was no
   durable, distinguishable GOAL state for approval-pending, no resolution
   seam, and no proof that a fresh process could resume the exact step.
2. `blocked_missing_capability` was selected by the strategy layer but never
   consumed by planning/execution: a goal whose required capability was absent
   failed to plan and re-planned until `max_replans` (goal FAILED), instead of
   waiting for the capability to appear. Arion also had only ONE real
   capability (`filesystem.read`).

This ADR hardens the loop around the EXISTING approval seam (no redesign of
authorization) and adds a second real read-only capability.

## Decision

### 1. Approval-pending stops the goal loop cleanly

- When a task reaches `AWAITING_APPROVAL`, `run_goal` returns the goal
  immediately (`next_action == "await_approval"` from the evaluator). It never
  re-executes or re-requests the same awaiting task — no spin, no duplicate
  checkpoints.
- The engine attaches a durable, typed blocker to the goal:
  `approval_pending` (with task_id, step_index, capability, action, scope,
  resource, reason) and transitions the goal to **BLOCKED**. This is distinct
  from an ordinary task failure (a failed task keeps the goal ACTIVE and
  triggers replanning; approval-pending keeps it BLOCKED and waits).
- `DeterministicProgressEvaluator` gained rule 3: any task
  `AWAITING_APPROVAL` → `next_action "await_approval"` (before blocker and
  world-change rules), with `evidence["awaiting_approval"]` and
  `approval_pending_steps`. A goal is therefore NEVER completed while an
  approval-gated step is unresolved, even if all currently-executable work is
  done.

### 2. GoalManager approval-aware BLOCKED semantics

- `set_blocked` / `clear_blockers` now emit `goal.blocked` / `goal.unblocked`.
- New `clear_blocker(goal_id, key, reason)` removes ONE blocker (e.g. just
  `approval_pending`) without clearing a coexisting `missing_capability`
  blocker.
- New `recheck_blockers(goal_id) -> bool` re-evaluates blockers against the
  CURRENT world state: a `missing_capability` blocker whose required
  capabilities are now registered is dropped (`capability.available` emitted),
  and an `approval_pending` blocker whose task is no longer awaiting is
  dropped. When none remain the goal transitions back to ACTIVE.
- Invalid transitions remain fail-closed (`GoalStateError`); the state machine
  is unchanged (ACTIVE/PAUSED/BLOCKED/COMPLETED/FAILED/CANCELLED).

### 3. Approval resolution path (`engine.resolve_approval`)

A clean seam — no GUI, no engine redesign:

- `resolve_approval(task_id, outcome, actor)` transitions the awaiting task:
  - **APPROVED** → task becomes resumable (RUNNING), the goal's
    `approval_pending` blocker is cleared (goal ACTIVE); the next `run_goal` /
    `run_task` resumes the EXACT pending step. No re-planning (plan version
    count unchanged) and no re-request of the same approval.
  - **DENIED** → the step and task fail durably with reason `approval denied`
    (goal unblocked; a later `run_goal` replans around it, e.g. by avoiding
    the denied action).
  - Fail-closed: unknown task, not-awaiting task, or no pending record raises.
- Approval records are persisted on the task (`Task.approvals`: bounded
  metadata — capability/action/scope/risk/side-effects/resource kind/resource,
  param KEY names only, reason, actor, timestamps; never secrets or raw
  params) and survive restarts via the task snapshot/checkpoints.

### 4. Re-authorization against CURRENT live metadata (stale approvals)

Resuming an approved step RE-RUNS the policy against the CURRENT `ActionSpec`
+ policy. A previously-approved record is honored ONLY when the current
request fingerprints identically (capability, action, scope, risk,
side_effects, resource_kind, resource). Any change — `required_scope`, `risk`,
removed resource boundary, changed resource/action — forces a FRESH approval
request or a DENY; the old approval never authorizes. Approval can never
modify actor identity (authorization still uses the engine's `Actor`), never
writes `ActionSpec` metadata, and model-produced `approved`/`grant` fields in
params are ignored (the seam is the only path).

### 5. Second capability: `git.log` (read-only)

`GitLogCapability` inspects git history by parsing `.git` metadata directly
(`.git/logs/HEAD` reflog, `.git/HEAD`, `.git/refs/heads/*`,
`.git/packed-refs`) — **no shell execution**. It:

- declares full `ActionSpec` metadata (scope `git:read`, risk low,
  read-only, idempotent, retry-safe, `resource_kind="filesystem:path"`,
  `resource_param="repo"`, `param_schema`, `default_verification`);
- reuses the SAME resource boundary as filesystem operations (kind
  `filesystem:path` + `RelativePathBoundary`) and enforces its own sandbox
  containment (repo resolved inside the sandbox root, symlink-safe);
- is discoverable via `registry.capabilities_summary()` (CLI
  `arion capabilities`);
- flows through the normal registry → planning (`DeterministicPlanner` git
  template) → authorization → execution → verification path.

The `DeterministicPlanner` now exposes `required_capabilities(description)`.

### 6. `blocked_missing_capability` end-to-end

- Before planning, `run_goal` asks the planner which capabilities the goal
  requires and, when one is absent from the LIVE registry, durably BLOCKS the
  goal (`missing_capability` blocker with the capability list) — never
  planning/failing/replanning in a loop.
- When the capability appears (registered + observed in the world state), the
  evaluator reports `replan` with reason `capability_available`; `run_goal`
  unblocks via `recheck_blockers` (emitting `capability.available` +
  `goal.unblocked`) and replans. Old authorization decisions are never reused:
  every new step is authorized against the current policy/metadata.
- Strategy selection remains informational; `blocked_missing_capability`
  strategy and the planner gate are consistent (dotted path tokens containing
  `/` are no longer misread as capability names).

### 7. CLI

- `arion run` drives the durable goal loop (`submit_goal` → `run_goal`) and
  reports `goal_id`, goal `status`, `blockers`, and the latest task —
  representing approval-pending, blocked, failed, and completed outcomes
  clearly (exit 1 only on FAILED goals).
- `arion goals approve <goal_id>` / `arion goals deny <goal_id>` resolve
  approval-pending goals through `resolve_approval` (`--json` supported;
  `--actor` is audit-only and never changes authorization identity).

### 8. Audit events (bounded, no secrets/raw output)

New kinds: `goal.blocked`, `goal.unblocked`, `goal.approval.pending`,
`goal.approval.granted`, `goal.approval.denied`, `capability.unavailable`,
`capability.available`, `task.approval.resumed`. Existing task-level
`approval.requested/granted/denied`, `permission.checked/denied` retained.

## Consequences

- Approval-pending goals are durable, restart-safe, and never spin.
- Approved work resumes the exact step; denied approvals are durable and
  explainable; stale approvals cannot authorize anything.
- Missing capabilities produce durable BLOCKED goals that recover when the
  capability appears.
- `git.log` proves the second-capability path (registry → validation →
  authorization → execution → verification) with full containment.
- The authorization architecture is untouched: `ApprovalHandler`,
  `PermissionPolicy`, `ActionSpec` semantics, and fail-closed boundaries are
  preserved (verified by the full suite).

## Not built yet (by decision)

Persistent approval queues / human approval UI (the seam is ready), goal
scheduling/prioritization, model-backed evaluation, write/shell capabilities,
git operations beyond read-only history inspection.
