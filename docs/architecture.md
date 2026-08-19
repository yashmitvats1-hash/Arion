# Arion — Architecture (v0.1)

Arion is an autonomous personal computing system (JARVIS/FRIDAY-class). The
objective is **not** a chatbot with tools: it is an agentic system with
persistent state, memory, planning, perception, tool use, execution,
verification, learning, and long-running goal operation.

## Five layers (ADR-001)

```
┌─────────────────────────────────────────────────────────────┐
│  INTERFACES   cli (today) · voice · vision · gui · api      │
├─────────────────────────────────────────────────────────────┤
│  ORCHESTRATION  goal→plan→permission→execute→observe→verify │
│                 →checkpoint→complete/recover  (the loop)    │
├─────────────────────────────────────────────────────────────┤
│  INTELLIGENCE  planner · router · (future: reflection,      │
│                learning)   — never owns the loop            │
├─────────────────────────────────────────────────────────────┤
│  CAPABILITIES  filesystem.read (today) · registry · scopes  │
├─────────────────────────────────────────────────────────────┤
│  STATE         goals · tasks · checkpoints · audit events   │
│                (sqlite behind Storage)                      │
└─────────────────────────────────────────────────────────────┘
```

## The loop (ADR-004)

```
Goal → Task → Plan → Authorization → Capability → Observation
     → Verification → Checkpoint → Complete / Recover
```

- Tasks are persistent objects with status `created → planning → planned →
  running → awaiting_approval → completed | failed`.
- Every step: capability discovery (resolve ActionSpec metadata) →
  authorization (policy decision) → execute (retries per retry-safety) →
  observe → verify → checkpoint.
- Checkpoints are full task snapshots; resuming after a restart restores the
  latest checkpoint and continues where it left off.
- **The LLM never owns the loop** (ADR-005). Default planner/router are
  deterministic; the whole system runs and is tested with no model.

## Authorization model (ADR-009)

Every step is decided by a permission policy over
`Capability → Action → Resource → Parameters → Policy Decision`:

- The scope, risk and side effects come from the capability's declared
  `ActionSpec` — never from a scope the plan merely claims (spoofing cannot
  escalate).
- Outcomes: `ALLOW` / `DENY` / `REQUIRE_APPROVAL`.
- `REQUIRE_APPROVAL` routes through an `ApprovalHandler` seam; PENDING pauses
  the task (`awaiting_approval`) with a checkpoint, and a later `run_task`
  resumes the exact same step once approved. A future human approval interface
  implements this protocol.
- **Fail-closed resources:** resource-sensitive actions (ActionSpec declares
  `resource_kind`) are DENIED unless an explicit boundary is configured for
  that kind. Non-resource actions are unaffected. Boundaries are keyed by
  resource kind (extensible — future `url`, `queue:name`, ...) and enforced as
  pure string checks; the capability still enforces its own containment
  (sandbox root, symlinks). The two are independent layers.
- **Identity:** requests carry an `Actor` with a delegation chain
  (`user → agent → delegated agent`); policies can match the direct actor or
  any ancestor. Audit events record `actor` + `actor_chain`.
- Event kinds: `approval.requested`, `approval.granted`, `approval.denied`;
  `permission.checked` events include the decision (outcome, scope, resource,
  resource kind, risk, side effects) and the actor chain.

## Execution semantics (ADR-010)

- Steps are **at-least-once**: an interrupted step is re-executed on resume.
- Automatic retries are gated on `ActionSpec.retry_safe`; non-retry-safe
  actions fail immediately after one failed attempt.
- Action metadata: `required_scope`, `risk`, `side_effects`, `reversible`,
  `idempotent`, `retry_safe` — the substrate for safe side-effecting
  capabilities later.

## Vertical slice (implemented)

- `arion/state` — domain models + `SQLiteStorage` behind `Storage`.
- `arion/capabilities` — `CapabilityRegistry`, permission scopes,
  `filesystem.read` (read-only, sandboxed, symlink-safe, size-capped),
  `git.log` (read-only git history inspection via `.git` metadata, no shell),
  `http.get` (read-only HTTP GET, injectable transport, origin-contained
  redirects, bounded size/timeout).
- `arion/state/approvals.py` — `ApprovalRequest` domain model + `ApprovalStore`
  protocol (durable approval queue, ADR-018).
- `arion/intelligence` — `Planner` protocol + `DeterministicPlanner`,
  `ModelRouter` protocol + `DeterministicRouter`, `PlanSchema` (versioned,
  strict), `PlanValidator`, `RealModelPlanner`, `providers/` (OpenAI-compatible
  adapter behind ModelRouter).
- `arion/orchestration` — `authz.py` (authorization layer: requests, policy
  outcomes, `ResourcePolicy`, approval seam) + `ArionEngine` (the state
  machine: authorization gate, retries, verification policies, checkpointing,
  recovery, the long-horizon `run_goal` loop).
- `arion/cognition` — `GoalManager` (authoritative goal state machine,
  plan versioning, progress evaluation, `readopt_plan` re-adoption +
  `diff_plans` deterministic diff, `peek_evaluate` read-only evaluation
  seam), `DeterministicProgressEvaluator`
  (model-independent evaluation seam), `StrategySelector` (explainable
  strategy selection + escalation), `SQLiteCognitiveStore` (goal_plans,
  beliefs, environment facts), `WorldStateMonitor` (versioned facts +
  change detection).
- `arion/observability` — `AuditEvent` vocabulary, `EventLogger`, JSONL sink.
- `arion/interfaces` — CLI (`run`, `resume`, `status`, `tasks`, `events`,
  `capabilities`,
  `goals list|show|diff|rollback|progress|pause|resume|cancel`,
  `approvals list|show|approve|deny`).
- `arion/memory` — `MemoryStore` protocol + `SQLiteMemoryStore` (episodic
  memories + reflections tables), `MemoryRetriever` (deterministic scoring +
  relevance gate), `DeterministicReflector`, `PlanningContext` (bounded
  digest), `build_episode_from_task` (structured summaries only).
- `arion/bootstrap.py` — composition root wiring all layers (memory on by
  default).
- `docs/adr/ADR-001..018` — approved architecture decisions.
- `tests/` — deterministic, LLM-independent tests.

## Persistent cognitive memory (ADR-012)

Memory is INFORMATIONAL — it never authorizes (`Memory ≠ Authority`):

- **Episodes:** one structured row per meaningful task experience (goal, plan
  summary with param KEY names only, actions, outcome, verification, failures
  with typed categories, authorization denials, recovery, tags, importance).
  No raw prompts/responses, no secrets, no transcript archives.
- **Reflections:** structured lessons (`what_happened/worked/failed/why`,
  `lesson`, `recommendation`, `confidence`, `importance`) from a
  `DeterministicReflector` (offline; `ModelReflector` later). A reflection can
  recommend but never executes anything.
- **Retrieval:** deterministic scoring (goal-token overlap, capability tags,
  outcome salience, importance, recency tie-break) with a relevance gate;
  bounded `PlanningContext` (max episodes/reflections/chars) handed to the
  planner — relevant memory, not the whole DB.
- **Lifecycle:** the engine records an episode + reflection at task
  completion/failure/denial and injects retrieved context before planning.
  Restart persistence is inherent (same SQLite file). Memory failure never
  breaks the task loop.
- **Events:** `memory.episode.recorded`, `memory.retrieval.completed`,
  `reflection.created`, `planning.context.created` (IDs/counts/tags only).

## Learning loop (ADR-013)

`Memory informs planning; memory never authorizes execution.`

- **ModelReflector** (behind the `Reflector` seam): produces the same
  structured `Reflection` as `DeterministicReflector`, strictly validated
  against a reflection schema that forbids authority-bearing fields
  (`scope`, `permissions`, `actor`, `grant`, `approve`, `authorization`,
  `capability_registration`, `resource_boundary`, ...). Malformed output is
  rejected; the engine falls back to the deterministic reflector.
- **MemoryGuidance**: deterministic conversion of retrieved episodes +
  reflections into structured recommendations (`avoid` / `prefer` /
  `informational`) with capability/action/resource scope. `apply_guidance_to_steps`
  re-targets plans away from known-failing resources. Episodes record their
  declared resource values (safe metadata, never arbitrary params).
- **Behavioral change is proven**: the same goal that was denied on a resource
  later completes by choosing a different resource, driven by retrieved
  experience (acceptance-gate test asserts Plan A != Plan B meaningfully).
- **Consolidation**: deterministic, explainable merging of repeated lessons
  into explicit records (never deletes history); importance decays with age;
  idempotent so lessons don't pile up.
- **Provenance**: `PlanningContext` exposes episode/reflection/guidance IDs;
  `planning.memory.influence` audits which memories influenced each plan.
- **Poisoning defenses**: adversarial memory/reflection content stays
  informational; all authorization answers come from the current policy.
- **Lifecycle idempotency (addendum):** exactly ONE durable episode per
  task (`get_episode_by_task` + a DB-level UNIQUE index on
  `episodic_memories.task_id` as the cross-process backstop; init-time
  merge of pre-idempotency duplicates keeps the newest row — a bug-artifact
  merge, never archival pruning). Episodes carry a durable `lifecycle`
  state: `recorded` → `reflected` → `consolidated`; `recorded` is the
  retryable state, so a crash mid-learning resumes instead of duplicating.
- **One reflection per episode (addendum):** the reflection claim is
  DURABLE, not check-then-act — a DB-level UNIQUE index on
  `reflections(episode_id)` plus first-writer-wins claims inside
  `BEGIN IMMEDIATE`: `record_reflection` re-records the same id as an
  in-place refresh but a NEW id for an already-reflected episode loses and
  RETURNS the canonical row (losers adopt it); `record_episode` claims the
  episode slot atomically (a racing minted id is never stored) and a
  re-record preserves the durable `reflection_id` link (no clobber). A
  crash between insert and link self-heals on the next pass. Legacy
  duplicate reflections are merged at init (keep the linked row, else the
  newest; repair links aimed at losers).
- **One consolidation per source set (addendum):** the consolidation claim is
  DURABLE, not check-then-act — a canonical, ORDER-INDEPENDENT source-set
  identity (`canonical_source_key = json.dumps(sorted(ids))`, so
  `[A,B,C] == [C,A,B]`) is keyed by a DB-level UNIQUE index on
  `consolidations.source_key` plus first-writer-wins claims inside
  `BEGIN IMMEDIATE`: `record_consolidation` re-records the same id as an
  in-place refresh but never mutates the immutable source-set identity, and a
  NEW id for an already-consolidated source set loses and RETURNS the
  canonical row (losers adopt it). The consolidator reports only records it
  actually created, so the engine emits `memory.consolidated` only for real
  creations (a racing learner never emits a duplicate event). Legacy duplicate
  consolidations are merged at init (keep the newest by `created_at`, rowid)
  before the unique index is created; malformed keys stay NULL (SQLite allows
  multiple NULLs in a unique index) rather than blocking it.
- **NULL `task_id` preservation (rider):** the init-time episode task-dedup is
  guarded with `task_id IS NOT NULL`, so a task-less episode (`task_id IS
  NULL`) is no longer silently deleted at store initialization while ordinary
  per-task dedup is unchanged.
- **Catch-up learning:** `engine.learn_from_terminal_tasks()` is an
  idempotent, restart-safe pass that records episodes for every terminal
  task that lacks one — recovering experience lost when a process crashed
  between the durable task save and the episode write (bounded
  `memory.learning.catchup` event; never touches scheduler authority).
- **Query-aware retrieval precision (addendum):** `retrieve` /
  `build_planning_context` accept an optional `capabilities` set; the
  engine passes the planner's requirement heuristic, so capability tags
  count as a relevance signal only when they match the task's likely
  capabilities (an http.get task never receives filesystem.read memory).
  Direct callers without the hint keep the original semantics.
- **CLI diagnostics:** `arion memory episodes|reflections|search|stats|
  consolidate` plus `memory inspect <episode_id>` (read-only, bounded,
  secret-free; human + `--json`).

## Cognitive State / World Model v1 (ADR-014)

Five cognitive layers, distinct from episodic memory:

- **episodic** (what happened) — `arion/memory` episodes/reflections
- **semantic** (what Arion believes) — `arion/cognition` Beliefs
- **procedural** (how to do things) — reflections + guidance + procedural beliefs
- **preference** (user-specific) — `arion/cognition` Preferences
- **environment** (current world state) — `arion/cognition` EnvironmentFacts

Every derived belief carries provenance (episode/reflection/guidance ids),
confidence, timestamps, and source (deterministic|model);
`DeterministicBeliefDeriver` is the reference path. `CognitiveState` facade +
`SQLiteCognitiveStore` (same DB file; restart-safe). **Informational only** —
beliefs can never authorize (tested).

- **Archival policy (ADR-014 addendum, implemented):** explicit,
  operator-invoked, bounded-batched (ADR-028 SELECT-then-DELETE pattern),
  fail-closed pruning — memory/cognition is never silently deleted:
  - `SQLiteMemoryStore.prune(older_than, max_episodes, keep_importance=0.0,
    batch_size=500, dry_run=False) -> int` — age/count pruning of episodes
    (never recent, never high-importance when a floor is set); reflections
    are pruned WITH their episodes; CONSOLIDATIONS are never pruned; at
    least one criterion required; idempotent; touches only
    `episodic_memories` + `reflections`.
  - `SQLiteCognitiveStore.prune_superseded_beliefs(older_than,
    keep_versions=1, batch_size=500, dry_run=False)` — superseded-history
    pruning; ACTIVE beliefs are never pruned; the newest `keep_versions`
    rows per belief lineage (category+statement) are always retained.
  - `SQLiteCognitiveStore.prune_goal_plans(goal_id=None, keep_latest=10,
    batch_size=500, dry_run=False)` — replan-history bounding; the latest
    immutable plan version per goal is never pruned (replay safety).
  - `--dry-run` never mutates (byte-identical DB; emits no event); real
    prunes emit a bounded `memory.pruned` audit event (counts + criteria
    only, never content). Preferences and environment facts are already
    bounded per key — no prune needed (stale facts are flagged, never
    deleted). Pruning is storage hygiene with NO authority influence:
    scheduler/task/goal authority, leases, ownership, reservations,
    ceilings, weights, DWRR credit, and execution state stay
    byte-identical (adversarial-tested); forged telemetry/metadata/ids and
    oversized values fail closed.
  - **Consolidation-fed beliefs:** `refresh_from_memory(limit=20,
    include_consolidations=False)` optionally lifts merged consolidation
    lessons into procedural beliefs with complete provenance
    (`episode_ids` + `consolidation_ids`); default False preserves the
    original behavior; idempotent, deterministic supersession,
    informational only.
- **Strategy-level learning:** `apply_guidance_to_steps` is non-mutating,
  registry-aware (ActionSpec.resource_param — no hardcoded `path`), and can
  substitute actions (different decomposition) with verification adopted from
  the registry. `PlanTransformation` retains original + transformed plans with
  per-decision provenance; audited via `planning.memory.transformation`; each
  transformed step carries its provenance.
- CLI: `arion cognition beliefs|preferences|environment|snapshot|world|goals|prune-superseded|prune-plans [--json]`;
  `arion memory prune [--older-than TS] [--max-episodes N] [--keep-importance F]
  [--batch-size N] [--dry-run] [--json]` — exit 0 on success (incl. dry-run),
  1 on invalid input (fail closed); deterministic output.

## World State → Long-Horizon Goals (ADR-015)

The cognitive spine: `World State → Beliefs → Goals → Long-Horizon Planning →
Strategy Selection → Authorization → Execution → Observation → Verification →
Learning`.

- **World-state change detection:** `WorldStateMonitor` observes environment
  facts (versioned per key with `observed_at`); value changes bump the version
  and emit `world.state.changed`; `stale_facts()` flags facts that need
  re-checking so stale state cannot mislead planning.
- **Strategy selection:** `StrategySelector` (deterministic, explainable)
  maps goal + beliefs + environment + memory guidance to a `Strategy`
  (`blocked_missing_capability` / `defer_retry` / `avoid_known_failures` /
  `capability_verified` / `direct`) with provenance; emitted as
  `strategy.selected` and exposed in the planning context.
- **Long-horizon goals:** `GoalManager` records every plan version per goal
  (`goal_plans` table: version, strategy, summary) and reports per-goal task
  progress — goals span sessions with traceable history.
- **Cognitive hardening:** Belief/Preference/EnvironmentFact validation;
  belief updates are append-only + versioned (revisions supersede prior rows,
  history never rewritten); the identity/confidence/version/supersession
  decision for beliefs is DURABLE, not check-then-act — a partial UNIQUE
  INDEX on `(category, statement) WHERE superseded_at IS NULL` plus
  first-writer-wins claims inside `BEGIN IMMEDIATE`
  (`SQLiteCognitiveStore.persist_belief`): a STRICTLY higher-confidence
  observation becomes a new version and atomically supersedes the prior
  active revision, an equal/lower observation adopts the canonical active
  row (no new row, no duplicate `belief.derived` event), and concurrent
  threads/processes cannot commit two active revisions; legacy duplicate
  active rows are deterministically repaired at init before the index is
  created (ADR-014 addendum); planning context carries `strategy` +
  `environment` (bounded) alongside memories.
- **Invariant (tested):** stale/poisoned beliefs, model instructions,
  preference manipulation, world-state facts, and memory-derived strategy
  changes never alter authorization - only `PermissionPolicy` decides.

## Durable Goal Management & Replanning (ADR-016)

The loop is now `Goal → Goal State → Strategy → Plan → Execute → Observe →
Learn → Replan`, owned by an authoritative, restart-safe `GoalManager`.

- **Explicit lifecycle:** ACTIVE / PAUSED / BLOCKED / COMPLETED / FAILED /
  CANCELLED with a validated transition table; invalid transitions raise
  `GoalStateError` (fail closed) and are surfaced cleanly by the CLI. Goal
  rows are authoritative versioned state: lifecycle writes use optimistic
  concurrency (`SQLiteStorage.cas_goal` — `UPDATE … WHERE id=? AND
  version=?` inside `BEGIN IMMEDIATE`). Stale writers reload and
  revalidate; distinct blocker keys merge; a progress / strategy /
  replan-reason patch cannot clobber a committed transition. A successful
  transition increments `goal.version` exactly once and emits
  `goal.state.changed` only after the CAS commits.
- **ProgressEvaluator (deterministic seam):** `DeterministicProgressEvaluator`
  evaluates completed/failed/skipped work, blockers, outstanding latest-plan
  steps, and material world-state changes → `ProgressResult` with progress,
  status, blockers, next_action, and evidence-with-provenance. Completion is
  never inferred from a single successful task.
- **Plan versioning + replanning:** every replan appends a NEW immutable plan
  version (monotonic, replay-safe; reasons like `replan_task_failed`,
  `replan_world_changed`). Version allocation is DURABLE, not check-then-act
  — `SQLiteCognitiveStore.claim_goal_plan` assigns `MAX(plan_version)+1`
  inside one `BEGIN IMMEDIATE` transaction (plain INSERT; the
  `(goal_id, plan_version)` primary key is the cross-process backstop).
  Divergent concurrent replans all survive as distinct versions; identical
  concurrent claims adopt one canonical plan when no task implements it; an
  equivalent replan after a task references the latest plan creates a new
  version. Gaps from pruning are valid (dense numbering is not required).
  `run_goal` is a per-call long-horizon cycle: it
  returns ACTIVE when a task fails (failure persisted), replanning is bounded
  by `max_replans` across calls. Superseded-plan failures never block
  completion once the newer plan is fully handled.
- **World-state → replan seam:** material environment fact changes (capability
  registrations, keys the plan depends on) trigger reevaluation → replan;
  unrelated facts (e.g. `system_uptime`) are filtered out. Deterministic and
  sequential - no daemons.
- **Strategy escalation:** `StrategySelector` now receives
  `previous_strategies` from the immutable plan history and escalates
  `direct → avoid_known_failures → defer_retry` instead of repeating a failing
  strategy; `blocked_missing_capability` when the goal names missing
  capabilities. Strategies remain informational with provenance.
- **Race-hardened learning:** successful tasks are classified `completed`
  (not `recovered`) unless a genuine mid-execution resume occurred, so
  successes yield `prefer` guidance; the plan transform refuses to substitute
  onto a resource that has since failed (explicit SKIPPED step instead).
- **Authorization invariant (extended + tested):** goal state, strategy,
  progress evaluation, world state, memory, model output, and replanning can
  never grant permissions; every step is re-authorized against CURRENT live
  `ActionSpec` metadata - old successful decisions are never reused after
  metadata changes (adversarial tests + demo).
- **Audit:** `goal.created`, `goal.state.changed`, `goal.evaluated`,
  `goal.replanned`, `progress.evaluated`, `plan.versioned` (plus existing
  `strategy.selected`, `world.state.changed`); bounded metadata only.
- **CLI:** `arion goals list|show|progress|pause|resume|cancel [--json]`.
- **Demo:** `scripts/demo_goal_replan.py` — 3-cycle race demo with a mid-goal
  restart proving state/version/progress/provenance survival, no duplicate
  plan versions, and live-metadata re-authorization. Runs offline.

## Approval-Gated Goals, BLOCKED Semantics, Second Capability (ADR-017)

Hardening around the existing approval seam (ADR-009) and the durable goal
loop (ADR-016):

- **Approval-pending stops the loop cleanly:** a task reaching
  `AWAITING_APPROVAL` makes `run_goal` return immediately (evaluator
  `next_action "await_approval"`); the goal becomes durably BLOCKED with an
  `approval_pending` blocker. No spin, no re-execution of the awaiting task,
  no re-request; distinct from an ordinary task failure. The goal is never
  completed while an approval-gated step is unresolved.
- **Resolution seam:** `engine.resolve_approval(task_id, APPROVED|DENIED,
  actor)`. APPROVED → task resumable (RUNNING), goal unblocked, next run
  resumes the EXACT pending step (no replan). DENIED → durable `approval
  denied` task failure (goal unblocked; replan later). Fail-closed on wrong
  states. Approval records persist on the task (bounded metadata, restart-safe
  via snapshots/checkpoints).
- **Live re-authorization:** resuming an approved step re-runs the policy
  against CURRENT ActionSpec/policy metadata; the approval is honored only if
  the request fingerprints identically (capability/action/scope/risk/
  side-effects/resource kind/resource). Changed `required_scope`, risk, or
  removed boundary forces fresh authorization; stale approvals never
  authorize. Approval cannot modify actor identity or ActionSpec metadata, and
  model-produced approval/grant fields are ignored.
- **`blocked_missing_capability` end-to-end:** before planning, `run_goal`
  gates on `planner.required_capabilities()` against the LIVE registry —
  missing capability → durable BLOCKED (`missing_capability` blocker), never a
  replan loop. When the capability appears (world state), the evaluator
  reports `replan` (`capability_available`), blockers are re-checked and the
  goal replans and completes. Old decisions never reused.
- **Second capability — `git.log`:** read-only git history inspection
  (`.git` metadata parsing; NO shell). Full ActionSpec metadata, same
  `filesystem:path` resource boundary + sandbox containment, discoverable via
  `arion capabilities`, flows through registry → planning → authorization →
  execution → verification.
- **GoalManager:** `goal.blocked`/`goal.unblocked` events, `clear_blocker`
  (single), `recheck_blockers` (world-state-aware; emits
  `capability.available`).
- **CLI:** `arion run` drives the durable goal loop (goal_id, status,
  blockers, task); `arion goals approve|deny <goal_id>` resolve approval.
- **Demo:** `scripts/demo_goal_approval.py` (both scenarios, offline).


## Persistent Approval Queue + URL Resource Boundary (ADR-018)

- **Durable approval queue:** `ApprovalRequest` domain model + `ApprovalStore`
  protocol (SQLite implements it, same DB file). Exactly ONE request per
  task/step/authorization-fingerprint; repeated `run_goal`/`run_task` calls are
  idempotent (no duplicate requests, no re-requests, no duplicate
  checkpoints). `resolve_approval_request(approval_id, APPROVED|DENIED, actor)`
  resolves the durable record and reuses the exact-step resume path; typed
  `ApprovalError` on unknown/already-resolved/mismatched/stale requests.
- **CLI approvals:** `arion approvals list|show|approve|deny [--json]` against
  the same persistent DB — `process A → request → exit; process B → CLI
  approve → exit; process C → resume → completion` works across real process
  boundaries (demo runs the CLI as a subprocess). `--actor` is audit-only.
- **`http.get` + `UrlBoundary`:** second resource kind (`url`) with an
  allowlist boundary (malformed/credentials/non-http(s)/off-allowlist → DENY;
  no boundary → DENY). Read-only GET through an INJECTABLE transport (fake in
  tests, stdlib default) with bounded size/timeout and origin-contained
  redirects (escape → denied, target never fetched). Bootstrap registers
  http.get discoverable but DENIED by default (fail closed).
- **Canonical authorization fingerprint:** capability, action, required
  scope, risk, side effects, resource kind, resource, and
  `security_relevant_params` declared per ActionSpec. Changing any of these
  after approval forces fresh authorization.
- **Planner contract (fail closed):** `Planner.required_capabilities()` is
  explicit; a planner that cannot declare requirements durably BLOCKS the goal
  (`planner_contract`), and a model-proposed unregistered capability is
  rejected by PlanValidator before execution.
- **Demo:** `scripts/demo_approval_queue.py` (both DoD paths, offline).

## First Write Capability + Approval Expiry (ADR-019)

- **`filesystem.write`** (`arion/capabilities/write.py`) — the ONLY mutating
  capability. Single `write` action: bounded plain-text write to a sandboxed,
  repo-relative path; pure `Path` I/O (no shell/subprocess); refuses to
  overwrite unless `overwrite: true` (security-relevant, fingerprinted);
  `risk=high`, `side_effects=mutating`, `retry_safe=False`; explicit
  `param_schema`; `write_verified` default verification (postcondition size
  check, no second mutation). Containment via `_resolve_inside` — `..`
  traversal, absolute paths outside the root, and symlink escapes fail
  closed. Registry-discoverable via bootstrap, but DENIED by the default
  policy (no `filesystem:write` scope) — no mutation without explicit
  authorization.
- **Non-retry-safe execution:** failed mutations are never blindly retried;
  the task fails durably with `mutation failed: …; recovery required` and a
  bounded audit trail (`mutation.attempted` / `mutation.failed` /
  `mutation.succeeded` / `mutation.requires_recovery`). The failed task is
  terminal — restart never duplicates the mutation; recovery goes through a
  NEW plan version + FRESH authorization.
- **Approval expiry:** stale PENDING requests expire (`ApprovalStatus.EXPIRED`
  + `expired_at`, engine `approval_ttl_seconds`, injectable clock, idempotent).
  Expired approvals cannot be resolved (typed `ApprovalError`); the awaiting
  task fails durably with `approval expired; recovery requires new
  authorization`; nothing is pruned (EXPIRED records + `approval.expired`
  audit events remain). CLI: `arion approvals list [--status expired]`, `show`
  expose EXPIRED + `expired_at`; approve/deny fail closed.
- **Fingerprint review:** the canonical fingerprint (capability/action/scope/
  risk/side-effects/resource-kind/resource/security-relevant params) covers
  writes once `overwrite` is declared; the content payload stays operational
  (not fingerprinted) — both directions proven by tests.
- **Demo:** `scripts/demo_adr019_write_approval.py` (scenarios A–E, offline).

## filesystem.append + Mutation Recovery Fencing (ADR-020)

- **`filesystem.append`** (`arion/capabilities/append.py`) — second mutating
  capability, same `filesystem:write` scope: bounded plain-text append, pure
  `Path` I/O (no shell/subprocess), NEVER clobbers existing content; creation
  of a missing file requires explicit `create: true`
  (`security_relevant_params=["create"]`, fingerprinted). `risk=high`,
  `side_effects=mutating`, `retry_safe=False`, `append_verified`
  deterministic postcondition verification (prior_size + appended bytes ==
  size, no second mutation). Containment via `_resolve_inside` (traversal /
  absolute / symlink escapes fail closed). Registry-discoverable via
  bootstrap, DENIED by the default policy (fail closed).
- **Durable mutation recovery registry** (`arion/state/recovery.py`) —
  `MutationRecovery` records (`REQUIRED | ACKNOWLEDGED`) + `RecoveryStore`
  (SQLite `mutation_recoveries` table). A failed non-retry-safe mutation
  durably records `REQUIRED` and attaches a `recovery_required` goal blocker;
  `run_goal` blocks fresh planning until an explicit, durable, audited,
  restart-safe `acknowledge_recovery(recovery_id, actor)` transition.
  Recovery is a GATE, never an authorization: after acknowledging, a fresh
  task still needs its own approval; expired/denied approvals stay
  expired/denied; failure history is never erased; memory/reflection/
  guidance/model output can neither clear nor trigger recovery.
- **Advisory fencing** — the planning context carries bounded
  `recovery` advisory records with provenance (`planning.recovery.advisory`):
  "mutation previously failed / recovery required / not retry-safe / fresh
  authorization needed". Planning information only; the engine enforces the
  durable recovery state and policy independently (adversarial tests prove
  poisoned guidance cannot execute a mutation).
- **CLI:** `arion recovery list|show|acknowledge <id>` with `--json`
  (domain/store interfaces only; bounded, secret-free, fail-closed).
- **Demo:** `scripts/demo_adr020_append_recovery.py` (scenarios A–E, offline).

## Cross-Process Advisory Mutation Locks (ADR-021)

- **Durable advisory locks** (`arion/state/locks.py` + SQLite
  `mutation_locks`): one lock per canonical security-relevant resource
  (`resource_kind` + canonical resource — `os.path.normpath` for
  filesystem:path, so `./notes.txt` / `notes.txt` / `a/../notes.txt` and
  write-vs-append all contend). Acquisition/reclamation are ATOMIC across
  processes via `BEGIN IMMEDIATE` SQLite transactions; the DB is the
  coordination authority (no in-memory locks, no lock files, no polling
  races). Typed `MutationLockError` on contention.
- **Ordering enforced by construction:** plan → authorization → approval if
  required → live re-authorization → **acquire mutation lock** → mutate →
  verify → **release lock**. The engine never does `lock → authorize →
  mutate`. A mutation lock is coordination, NOT authorization; authorization
  is evaluated independently for every mutation attempt.
- **Contention behavior:** the capability never executes; the task fails
  durably with `mutation lock contention`; the goal is durably BLOCKED
  (`lock_contention` blocker, rechecked against the live lock store); no
  duplicate approvals, no replan loop, no recovery record; after the lock is
  released the goal replans and the new task needs its own fresh approval.
- **Release guarantees:** locks are released on every terminal path — success,
  mutation failure (recovery REQUIRED + released), verification failure
  (recovery REQUIRED + released), unexpected exception. Authorization
  failures / pending / stale approvals never reach the lock.
- **Leases:** `expires_at = acquired_at + lease_seconds` with an injectable
  clock; expired locks are reclaimable atomically (auto-reclaimed on acquire
  of the same resource, or explicitly via `reclaim_stale_locks()` /
  `reclaim_lock(id)`); a crashed owner never permanently wedges a resource.
- **Approval/recovery interaction:** approvals never imply lock ownership
  (an approved task still contends); pending/expired/denied/stale approvals
  never acquire locks; recovery never clears or transfers locks — a future
  task acquires a fresh lock after its own fresh authorization.
- **Adversarial:** memory/reflection/strategy/model output (`lock_acquired`,
  `approved`, `owner`, forged metadata) and actor-identity claims cannot
  create, release, transfer, or bypass locks — the lock store is the only
  lock authority.
- **Audit:** `mutation.lock.requested/acquired/contended/reclaimed/released`
  with bounded metadata only.
- **CLI:** `arion locks list|show|reclaim <lock_id>` with `--json`
  (fail-closed reclaim; never authorizes).
- **Demo:** `scripts/demo_adr021_lock_two_process.py` (two real subprocesses,
  scenarios A–E, offline).

## Bounded Lock-Contention Waiting/Backoff (ADR-022)

- **Durable waiting state** — a task that hits a mutation-lock contention
  enters a bounded wait instead of failing immediately: `Task.lock_wait`
  metadata (resource, deadline, attempts, next_retry) persisted on the task +
  the goal's `lock_contention` blocker (upserted each retry). Entering the
  wait is NOT a mutation failure: no recovery record, no `mutation.failed`,
  capability never executes. Waiting is coordination-only — never an
  authorization mechanism.
- **Deterministic bounded backoff** — `delay = min(base * 2^(attempt-1),
  max)` with engine-configurable deadline (`lock_wait_max_seconds`),
  injectable clock + sleeper for deterministic tests; never sleeps inside a
  SQLite transaction. `lock_wait_max_seconds=0` disables waiting (ADR-021
  immediate-contention semantics, available by configuration).
- **Ordering preserved:** plan → authorize → approval if required → live
  re-authz → contention → bounded wait/backoff → retry acquisition →
  **re-validate LIVE authorization** → mutate → verify → release. After a
  waited acquisition the engine re-checks the current ActionSpec/policy and
  forces the normal fresh authorization/approval path if anything became
  stale (releasing the acquired lock if the step pauses/denies).
- **Restart safety:** the retry budget/deadline survive restarts (never
  reset); past-deadline resumes time out immediately; while the resource is
  still locked the goal is durably BLOCKED (`await_lock` — a distinct
  evaluator state) without spinning; a crashed waiter holds no lock (no
  immortal waiter).
- **Audit:** `mutation.lock.waiting` / `mutation.lock.retry` /
  `mutation.lock.timeout` (typed `MutationLockTimeoutError`), metadata only.
- **CLI:** `arion locks waiters` and `arion locks show <id>` (lock or
  waiter) with `--json` — task/goal/step, resource, attempts, deadline, next
  retry, status; bounded and secret-free.
- **Demo:** `scripts/demo_adr022_lock_wait.py` (two real subprocesses:
  wait→success, timeout, stale-authorization; offline).

## Fair, Durable Mutation-Lock Wait Queues (ADR-023)

- **Durable FIFO wait queue** (`mutation_lock_waiters` table): every waiter
  gets a durable position (seq) per canonical resource, assigned atomically
  (`1 + MAX(seq)` inside `BEGIN IMMEDIATE`); rows are append/audit-safe
  (queued → acquired | timed_out | cancelled, never deleted) and carry only
  bounded identifiers/timestamps (task/goal/step, resource, enqueue time,
  position, deadline, attempts, status).
- **Head-gated acquisition:** `acquire(waiter_id=...)` succeeds only for the
  oldest ELIGIBLE waiter (queued + deadline not passed + task not terminal,
  checked inside the same transaction as the lock insert) — a newer waiter
  can never overtake an older one; acquire + dequeue is atomic; expired
  waiters are marked timed_out durably even when the acquire fails.
- **Release handoff is atomic:** `release_and_select_next` deletes the lock
  and selects the next eligible head in ONE transaction (no check-then-act
  race); the engine uses it on every release.
- **Engine:** with bounded waiting, the task joins the queue on first
  contention and REUSES its waiter/position across restarts (deadline +
  attempt budget preserved); timeout dequeues cleanly (typed
  `MutationLockTimeoutError`, no mutation, no recovery); a terminal task
  cancels its queued waiters. `task.lock_wait` + the goal blocker carry
  waiter_id + position.
- **Fairness is coordination, never authorization:** queue position is not in
  the authorization fingerprint, policy, actor, ActionSpec, approval, or
  recovery semantics; the lock store remains the sole lock/queue authority
  and the live authorization layer remains the sole execution authority —
  live re-validation still runs after every waited acquire.
- **Audit:** `mutation.lock.queued` added to the lock vocabulary (requested →
  queued → waiting → retry → acquired → released).
- **CLI:** `arion locks waiters` shows position/waiter_id/status;
  `arion locks queue <resource>` shows the durable queue (all statuses);
  `show <id>` resolves lock/waiter/task ids; nothing grants or transfers
  ownership; unknown ids fail closed.
- **Demo:** `scripts/demo_adr023_lock_fairness.py` (real subprocesses:
  FIFO handoff, restart survival, timeout, live re-authz, adversarial).

## Bounded In-Process Concurrency (ADR-024)

- **Scope:** `ArionEngine(max_concurrency=N)` runs up to N *steps* of a
  task concurrently on bounded scheduler worker threads (default 1 = the
  historical sequential behavior). Concurrency is per engine/process; no
  distributed execution, no new capabilities, no shell/subprocess.
- **Scheduler authority:** `arion/orchestration/scheduler.py` is the ONLY
  source of worker lifecycle state (runnable/running/completed/failed/
  cancelled) with explicit `enqueue/cancel/shutdown/run_until_done`,
  injectable clock/sleeper, and joined (non-orphan) workers after
  `shutdown()`. The scheduler is NOT an authorization authority.
- **What may run concurrently:** independent read-only steps overlap; mutating
  steps serialize per canonical resource through the existing durable
  SQLite lock + FIFO queue; different-resource mutations overlap with their
  own locks. Dependency edges stay authoritative; the cursor step is always
  dispatched (approval requests, serial-path behavior unchanged); every
  concurrent step runs its OWN live authorization — no authz decision is
  reused.
- **Durable restart safety:** per-step terminal status is persisted from the
  worker immediately, so a crash mid-round never replays a completed
  mutation; an interrupted step resumes under the existing at-least-once/
  recovery contract with bounded metadata only (no thread objects, stack
  traces, capability outputs, or model output ever persisted).
- **Per-step durable waiters:** `task.lock_wait` is step-scoped, so concurrent
  same-task waiters on one resource hold distinct durable FIFO positions.
- **Cancellation/shutdown:** queued items are cancelled by the scheduler and
  never run; a running mutation is never pretended-away (its outcome stands,
  failures still create durable recovery); cancelled FIFO waiters never
  acquire; `shutdown()` joins bounded workers and the CLI calls it before
  exiting.
- **Store thread-safety:** `SQLiteStorage` guards one connection with an
  RLock (`check_same_thread=False`); cross-process authority is unchanged
  (`BEGIN IMMEDIATE` + WAL), so in-process threads and other processes
  compete on exactly the same lock/queue authority.
- **Demo:** `scripts/demo_adr024_concurrency.py` (30 checks, deterministic,
  offline): parallel reads, same-resource FIFO serialization, different-
  resource concurrency, blocked mutation not stalling reads, restart/cancel/
  shutdown with no orphan work.

## Cross-Goal Durable Concurrency (ADR-025)

- **Scope:** multiple goals/tasks share ONE bounded in-process scheduler
  (`ArionEngine.run_tasks`/`run_goals`) with a global `max_concurrency`
  (default 1 = historical behavior). Total running workers never exceed the
  bound; independent tasks execute concurrently; a blocked/approval-pending/
  recovery-gated goal consumes no worker and never stalls the others.
- **Durable scheduler registry:** `arion/state/scheduler_work.py` +
  `scheduler_work` table (typed store protocol, no raw SQLite from the
  engine/CLI). States QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED/ABANDONED
  with legal transitions enforced by typed errors (fail closed); bounded
  metadata only (ids, task/goal/step refs, scheduler + worker identity,
  timestamps, lease, truncated error) — never threads/callables/stack
  traces/capability outputs/model output/prompts/file contents/secrets.
- **Restart/crash recovery:** engine construction reclaims expired RUNNING
  leases (ABANDONED — no immortal RUNNING) and abandons dead schedulers'
  QUEUED rows; tasks re-run the full fresh authorization/recovery path;
  completed mutations are never replayed; stale mutation locks are reclaimed
  through the existing lock store; approval, FIFO waiter positions and
  mutation-lock state survive restarts (incl. a real-subprocess crash test).
- **Fairness:** rounds rotate tasks round-robin with a per-task per-round
  cap of `ceil(max_concurrency / active_tasks)` — a goal with many steps
  cannot starve a goal with one (bounded window = one round). Strictly
  scheduler coordination; never authorization.
- **Safe parking:** a mutating step whose resource is locked by another
  task registers a durable FIFO waiter and consumes NO worker; the durable
  waiter row is the deadline authority (forged leases cannot extend waits).
- **CLI:** `arion scheduler status|workers|queue|show <id>|reclaim <id>`
  (bounded, metadata-only, secret-free, restart-safe, fails closed on
  unknown ids; reclaim only abandons expired RUNNING leases).
- **Authority boundary (unchanged and regression-tested):** scheduler
  coordinates execution; the durable lock store coordinates mutation
  ownership; only live authorization permits execution. Scheduler state is
  never authorization state.
- **Demo:** `scripts/demo_adr025_cross_goal_concurrency.py` (29 checks,
  deterministic, offline): concurrent goals, same-resource FIFO writes,
  approval-pending/recovery-gated goals not stalling others, multi-goal
  restart without duplicate mutations, fairness.

## Cross-Process Shared Scheduler (ADR-026)

- **Scope:** multiple Arion engine processes share ONE scheduler/work
  registry database. Every process gets a unique durable scheduler
  REGISTRATION (lease + lazy heartbeat); `abandon_foreign_queued` now keys
  on registration liveness, so a live peer's queue is never abandoned.
- **Lease-based ownership:** every dispatched work item is atomically
  CLAIMED (`BEGIN IMMEDIATE`: lazy stale reclaim + cross-process capacity +
  QUEUED→RUNNING with a bounded worker lease). Heartbeats are
  ownership-checked, monotonic (`now >= started_at`), bounded
  (`expiry <= started_at + max_lease`) and stale-rejected; `mark_terminal`
  from RUNNING requires the current owner (a stale owner can never
  complete/fail work after expiry or reassignment).
- **Atomic handoff:** `release_and_claim_next` completes one row and claims
  the next in ONE transaction (release_and_select_next-style); racing
  processes produce exactly one owner.
- **Cross-process capacity + fair share:** optional durable
  `global_max_concurrency` enforced inside every claim across ALL processes
  (lazy in-transaction reclaim means a crashed process never permanently
  consumes capacity). Fair-share admission (ceil(cap/active) per scheduler)
  prevents a hot claimer from monopolizing capacity; unset cap preserves
  ADR-025 behavior exactly.
- **Crash recovery:** dead registrations → QUEUED rows abandoned; expired
  leases → RUNNING rows reclaimed (no immortal RUNNING); stale mutation
  locks reclaimed via the existing lock store; completed mutations never
  replay; approval/recovery gates unchanged.
- **Thread safety:** `SQLiteCognitiveStore`/`SQLiteMemoryStore` gained the
  same RLock + `check_same_thread=False` guard as `SQLiteStorage`.
- **Demo:** `scripts/demo_adr026_cross_process_scheduler.py` (33 checks,
  deterministic, offline): registration/claim/heartbeat primitives,
  two-process claim race, global capacity across engines, stopped-heartbeat
  reclaim, stale-owner rejection, atomic handoff, crash recovery, multi-
  goal restart without duplicate mutations. Real subprocess coverage in
  `tests/test_multi_process_scheduler.py`.

## Durable Per-Goal Capacity Shares + Weighted Fair Scheduling (ADR-027)

- **Scope:** the ADR-026 cross-process scheduler becomes GOAL-AWARE. A
  durable per-goal weight config (`scheduler_goal_weights`: goal_id,
  positive bounded integer weight, enabled flag, updated_by/at) plus
  durable DWRR credit (`scheduler_goal_state`) extend the atomic claim
  transaction; unconfigured goals use the deterministic default weight 1
  and no global cap ⇒ behavior identical to ADR-026.
- **Weighted admission (deterministic DWRR inside `BEGIN IMMEDIATE`):**
  contending goals = goals with QUEUED/RUNNING rows; when a claim is
  attempted and NO contending goal holds credit, every contending enabled
  goal is refilled by its weight (deficit bounded at max(weight, 2×cap)
  and clamped at spend time); a claim is granted iff the goal's credit ≥ 1
  (debited 1) after the global-cap and scheduler fair-share gates. A
  weight-2 goal gets exactly 2× the claims of a weight-1 goal per round
  under sustained contention; every contending goal claims at least once
  per round (no starvation); idle goals reserve nothing; the global cap is
  never exceeded.
- **Dynamic policy:** weight changes apply to future refills only; RUNNING
  work stays owned; no retroactive cancellation, no capacity duplication;
  the durable deficit is the only fairness state (restart-safe, no
  in-memory counter).
- **Authority boundary:** weights are scheduler POLICY — planner output,
  model output, memory, guidance, task metadata, worker input can never
  establish or elevate a weight; forged deficits cannot exceed the global
  cap, bypass the scheduler fair share, or grant ownership (heartbeat/
  terminal/handoff stay owner-checked).
- **CLI:** `arion scheduler weights`, `scheduler weight set <goal> <w>
  [--disable] [--by]`, `scheduler weight remove|enable|disable <goal>`.
- **Demo:** `scripts/demo_adr027_weighted_scheduler.py` (31 checks,
  deterministic, offline): default/equal/2:1/2:1:1/low-weight progress/
  cap enforcement/cross-process/dynamic change/restart/adversarial.

## Scheduler observability / telemetry (ADR-028)

- **Scope:** the existing bounded audit abstraction (`AuditEvent` +
  `audit_events`) is extended — no second event system — with a durable,
  bounded, queryable telemetry layer for the scheduler: registration/
  liveness, queue admission (`work.queued`, atomic with the row insert),
  work claims and denials (with reason codes), heartbeats,
  lease expiry/reclaim, ownership handoff, scheduler abandonment,
  global-cap and fair-share decisions, goal-weight/DWRR refill decisions,
  terminal completion/failure, shutdown, and config changes.
- **Storage:** `scheduler_events(id, ts, scheduler_id, worker_id, goal_id,
  task_id, work_id, step_index, event_type, reason, success, detail,
  schema_version)` with five covering indexes. `detail` is sanitized at
  write time (whitelist of scalar keys, strings ≤ 200 chars, non-scalars
  dropped, `schema_version` injected). Duplicate event ids are
  `INSERT OR IGNORE` — idempotent, observational-only appends never error
  the caller. No secrets, model prompts, planner output, credentials,
  task payloads, or full memory ever enter an event.
- **Atomicity:** every event commits inside the same `BEGIN IMMEDIATE`
  transaction as the transition it describes (`_sech_insert_in_tx`), so a
  rolled-back transition leaves no phantom success event; a crash between
  transition and event is impossible. Claim success commits `work.claimed`
  (+ optional `goal_weight.refill` with weight/credit_before/credit_after)
  atomically; denials carry a specific kind (`capacity.denied`,
  `scheduler_share.denied`, `goal_weight.denied`) plus a reason code.
- **Observational only — never authority:** telemetry records are never
  consulted as permission to execute, claim, complete, extend a lease,
  bypass approval/recovery/dependencies/mutation locks, change capacity,
  or change goal weights. Forged, deleted, or duplicated events have zero
  effect on execution semantics; the durable registry rows and the
  transactional claim path remain the only authorities. Tests prove a
  forged claim creates no ownership, a forged completion/heartbeat never
  completes or extends, a forged reclaim/refill never re-queues or
  refills, delete-all leaves behavior identical, and duplicated events
  never duplicate execution.
- **Query API (read-only, bounded, fail-closed):** `scheduler_events(...)`
  (oldest-first, `limit` clamped to [1, 1000], filters by goal/scheduler/
  work/type/since), `scheduler_event_count()`, `oldest_scheduler_event()`,
  `recent_scheduler_events(...)`, and `scheduler_status()` — a point-in-
  time observation (counts, active/stale schedulers, running/queued by
  scheduler and goal, weights, DWRR credit, recent reclaim/failure
  counts) that is never a cached authority. No unbounded `SELECT *`.
- **Retention:** `prune_scheduler_events(cutoff, batch_size ≤ 5000)` runs
  bounded SELECT-then-DELETE batches, never touches scheduler authority
  tables, and never silently deletes recent events (cutoff is caller
  supplied and explicit).
- **CLI:** `arion scheduler watch [--json] [--goal G] [--scheduler S]
  [--work W] [--type T] [--since TS] [--limit N] [--follow]` — human rows
  plus stable machine-readable JSON (`[{id,ts,kind,detail}]`); `--follow`
  is a bounded read-only poller (no registration, no heartbeat) with a
  Ctrl-C clean exit.
- **Crash/restart:** events are durable across reopen; a crash after a
  durable claim leaves `work.claimed` committed and the stale lease is
  reclaimed with `work.reclaimed` committed atomically in the same
  transaction; history replays oldest-first; an active heartbeat is
  distinguishable from an abandoned scheduler.
- **Demo:** `scripts/demo_adr028_scheduler_observability.py` (28 checks,
  deterministic, offline): registration, claims, heartbeats, completion/
  failure, capacity vs fair-share denials, DWRR refill, atomic reclaim,
  handoff, abandonment, restart history, rollback-no-phantom, forged
  telemetry powerless, retention, CLI JSON.

## Per-goal weighted capacity reservation (ADR-029)

- **Concept — weight ≠ reservation:** a weight is relative scheduling
  opportunity among contending goals (DWRR); a reservation is a minimum
  guarantee — the goal may reserve a bounded floor of concurrent RUNNING
  slots **while it has runnable work** (idle goals reserve nothing).
  Unit: `reservation = minimum number of concurrent runnable execution
  slots for that goal`. Weights keep their exact ADR-027 semantics.
- **Data:** `scheduler_goal_reservations(goal_id, reservation, enabled,
  updated_at, updated_by)` — integer `[0, 10000]`, default 0 for
  unconfigured goals, fail-closed validation, mirroring the weight-config
  conventions. Config API: `set/get/list/remove_goal_reservation`,
  `set_goal_reservation_enabled` (all transactional, event-emitting).
- **Oversubscription policy — REJECT, never normalize:** with a global
  cap configured, the sum of ENABLED reservations may never exceed the
  cap; `set_goal_reservation` / `set_goal_reservation_enabled(True)` /
  `set_scheduler_global_max` fail closed with a typed error on any change
  that would make the policy impossible. With no cap configured,
  reservations are accepted (unbounded capacity) and the admission gate
  is a no-op, exactly like the DWRR gate.
- **Admission order (inside `BEGIN IMMEDIATE`):** 1) reclaim stale;
  2) global capacity; 3) scheduler fair share; 4) **reservation gate**
  (floor path: the claiming goal is below its floor → grant, with DWRR
  accounting kept honest — the claim flows through the DWRR gate and the
  floor overrides only a credit denial, never the weight-disabled hard
  gate, never gates 1–3; protection path: the claim would consume a free
  slot needed by another runnable reserved goal → `reservation.denied`,
  row stays QUEUED); 5) DWRR weighted admission; 6) ownership.
- **Guarantee (exact, durable):** for an enabled reserved goal G with
  runnable work, global capacity ≥ R and the claiming scheduler eligible
  under fair share, no other goal can consume a free slot while G is
  below R and the remaining free slots cannot cover G's deficit
  (`free − 1 < outstanding` denies). RUNNING work is never cancelled to
  satisfy a floor. After all floors are satisfied, remaining capacity
  follows ADR-027 weighted fairness exactly (proven 5:1 with a reserved
  floor in place).
- **Dynamic semantics:** config changes apply to future claims only;
  RUNNING work is never retroactively cancelled/re-owned; oversubscribing
  changes fail closed; reservations + DWRR deficit survive restart.
- **Cross-process:** all gates read only durable rows inside
  `BEGIN IMMEDIATE`; real-subprocess tests prove the floor holds across
  processes, racing processes cannot bypass the protection, stale
  scheduler reclaim does not permanently consume reserved capacity, and
  reservations never exceed the global cap.
- **Observability (ADR-028 extension):** new kinds
  `goal_reservation_changed`, `reservation.denied`, `reservation.satisfied`
  (atomic with their transitions; observational only). `scheduler_status()`
  adds `goal_reservations`, `reserved_capacity`, `reservation_satisfied`,
  `reservation_pressure` (deterministic from durable rows). CLI:
  `arion scheduler reservations`, `reservation set|remove|enable|disable`
  (human + `--json`, bounded validation, persistence).
- **Security boundary:** reservations are scheduler POLICY only — no
  planner/model/task metadata path exists; forged reservation events,
  forged satisfied/denied telemetry, forged capacity counts / DWRR
  deficits / queue positions / stale ownership have zero effect (the
  gates read authority tables only); one goal can never use another
  goal's reservation; reservations never establish execution authority.
- **Demo:** `scripts/demo_adr029_reserved_capacity.py` (32 checks,
  deterministic, offline): default 0, single/multiple floors, protection,
  high-weight vs reserved, DWRR interaction, idle/runnable-again,
  cross-process, dynamic changes, restart/reclaim, telemetry, forged
  attempts, CLI/status/watch.

## Reservation-aware capacity planning & scheduler status (ADR-030)

- **Nature:** a READ-ONLY projection over durable scheduler state —
  observability/planning only. It never claims, heartbeats, registers,
  writes reservations/weights/capacity/DWRR credit, establishes
  ownership, completes work, reclaims leases, or bypasses approvals. A
  forged planning result has zero execution effect; the authoritative
  claim transaction always wins; the ADR-029 claim path is unchanged.
- **Capacity arithmetic:** `available = max(cap − running, 0)`; with NO
  global cap, `available`/`unreserved` are explicit `None` (unbounded)
  sentinels — never an invented finite capacity.
- **Configured vs active reservations:** `reserved_capacity` = sum of
  ENABLED reservations over all configured goals (configuration view);
  `active_reserved_capacity` = sum over enabled reserved goals WITH
  queued work (idle floors consume nothing — identical to the claim
  gate's idle-goal rule); `reservation_pressure` = Σ max(0, R − running)
  over runnable reserved goals; `unreserved = max(cap − running −
  active_reserved, 0)`.
- **Feasibility (read-only):** `reservation_feasibility(proposed=None)`
  evaluates the current or a full proposed configuration:
  feasible iff no cap OR `proposed_total ≤ cap`; returns totals, exact
  `overflow`, `affected_goals`, `reason ∈ {ok, no_global_cap,
  oversubscribed}`; never mutates config.
- **Simulation (dry-run):** `simulate_reservation_change(goal_id, new)`
  and `simulate_reservation_config(proposed)` compute current/proposed
  totals, remaining capacity, feasibility, overflow, and a deterministic
  `pressure_delta` (increase/decrease/unchanged vs the ADR-029 pressure
  formula). Repeated runs leave reservations, weights, DWRR credit,
  events, and ownership byte-identical.
- **Admission explanations (projection, not a gate):** per-goal
  `state ∈ {idle, weight_disabled, reserved_floor, reservation_waiting,
  global_capacity_exhausted, scheduler_share_limited, eligible,
  goal_weight_limited, unknown}` — derived from the same durable tables
  and constants as the claim path (incl. the DWRR refill-round rule and
  the ceil(cap/active) fair share), computed WITHOUT running the gates
  (which mutate credit). Every explanation carries: "Eligible based on
  current snapshot; admission is still authoritative at claim time."
- **Store API:** `capacity_snapshot()` (typed snapshot: scalars,
  below/at/above lists, per-goal projections, config views),
  `explain_goal_eligibility()`, `reservation_feasibility()`,
  `simulate_reservation_change()`, `simulate_reservation_config()`,
  `reservation_check()`.
- **CLI:** `arion scheduler status` upgraded to the planning layout
  (human) + additive JSON (old keys preserved); `scheduler reservations
  --check [--json]` (exit 0 feasible / 1 infeasible, read-only);
  `scheduler reservation plan <goal> <n> [--json]` (dry-run, never
  persists, invalid input exits 1).
- **JSON schema:** semantic field names, no SQLite internals; `null`
  for unbounded (no cap), `0`/`[]` for zero/empty, deterministic
  ordering (goals sorted by id); bounded (ids/counts/enums only).
- **Cross-process:** snapshots/plans are plain reads over the shared
  registry — they may be stale the instant they return, and they can
  never mutate authority even while subprocess workers claim, heartbeat,
  complete, or reclaim.
- **Security boundary:** planning reads authority tables only — forged
  telemetry (reservation/satisfied/denied/refill/queue/capacity events),
  fake goal ids, and planner/model/task metadata cannot alter planning
  inputs, create reservations, change DWRR, establish ownership, or make
  an infeasible config executable; malformed/oversized inputs fail
  closed.
- **Demo:** `scripts/demo_adr030_capacity_planning.py` (35 checks,
  deterministic, offline): empty/cap/running snapshots, configured vs
  active reservations, idle/below/satisfied goals, feasibility,
  increase/decrease simulation, status JSON, check JSON, no-mutation
  proof, forged telemetry, cross-process observation, restart.

## Per-goal concurrency ceilings (ADR-031)

- **Concept — three independent policy dimensions:** weight = relative
  opportunity (DWRR); floor = minimum protected capacity while runnable
  (ADR-029); **ceiling = maximum concurrent capacity for one goal**.
  Ceilings never create ownership, never reserve capacity, never bypass
  existing gates, never establish execution authority.
- **Data:** `scheduler_goal_ceilings(goal_id, ceiling, enabled,
  updated_at, updated_by)` — integer `[1, 10000]` when configured;
  **default `None` = unbounded** (never an invented huge integer; `0` is
  rejected — use remove/disable for unbounded). API mirrors the
  reservation registry; config writes emit `goal_ceiling_changed`
  atomically.
- **Floor + ceiling compatibility:** when both are enabled, `R <= C`
  (enforced atomically in ALL four write directions — set ceiling, set
  floor, enable ceiling, enable floor; failed writes leave no partial
  state or event). `sum(ceilings)` does NOT need to fit the global cap
  (maximums, not reservations); ADR-029's floor oversubscription rule is
  unchanged. Under valid config, a below-floor goal is never at its
  ceiling (`running < R <= C`) — the impossible state is unconstructible.
- **Admission gate (final order in `BEGIN IMMEDIATE`):** 1) stale
  reclaim; 2) global capacity; 3) scheduler fair share; 4) reservation
  floor/protection; 5) **goal ceiling** (`ceiling.denied`, row stays
  QUEUED, NO DWRR credit consumed, NO refill); 6) DWRR weighted
  admission; 7) ownership. Enforced in both the specific-row and
  `claim_next` paths, so the floor path can never bypass it. Core
  invariant: `running_G <= C` at every committed state — never bypassed
  by floors, races, multiple processes, stale reclaim, restart, or
  handoff.
- **DWRR interaction (no stranded credit):** a goal AT its enabled
  ceiling cannot spend credit, so the refill-round check skips it (like
  weight-disabled goals) — peers keep getting refill rounds and
  progress; the ceiling-limited goal's durable credit is never destroyed
  and becomes spendable again below the ceiling.
- **Dynamic changes:** increase permits new claims immediately; decrease
  never cancels RUNNING work (binds future claims until running falls
  below C); disable/remove = unbounded for future claims; values persist
  across restart; pair changes validate atomically.
- **Cross-process:** real-subprocess tests prove 2+ processes cannot
  collectively exceed a ceiling, the final-slot race has exactly one
  owner, stale reclaim frees a ceiling slot, restart preserves it, and
  cap/floors/share/DWRR stay authoritative.
- **Telemetry:** `goal_ceiling_changed`, `ceiling.denied` (goal, work,
  running, ceiling, reason `goal_ceiling`); `scheduler watch` renders
  them; forged ceiling events have zero effect.
- **Status/planning (ADR-030 extension):** per-goal `ceiling`
  (`None` = unbounded), `ceiling_enabled`, `ceiling_headroom =
  max(C − running, 0)` (None when unbounded); aggregates
  `ceiling_limited_goal_count`, `goals_at_ceiling`,
  `recent_ceiling_denials`; explanation state `goal_ceiling_limited`
  (outranks credit states, carries the claim-time disclaimer).
  `reservation_feasibility` validates `floor <= ceiling` (reason
  `floor_exceeds_ceiling`); `simulate_reservation_change` reports
  `floor_ceiling_valid`; new `simulate_ceiling_change` and
  `simulate_goal_policy(goal_id, reservation=, ceiling=, weight=)`
  dry-runs — all provably mutation-free.
- **CLI:** `arion scheduler ceilings`; `ceiling set|remove|enable|
  disable <goal>` (human + `--json`, `--by`, fail-closed validation);
  `ceiling plan <goal> <n>` (dry-run, never persists, invalid → exit 1);
  `scheduler status` rows and `reservations --check` expose ceilings.
- **Security boundary:** forged ceiling config/changed/denied events,
  task metadata, planner/model output, fake goal ids, queue positions,
  running counts, DWRR credit, heartbeat/reclaim events cannot change a
  ceiling, exceed it, transfer it, or bypass it; global capacity remains
  authoritative. Policy influences admission; policy never establishes
  execution authority.
- **Demo:** `scripts/demo_adr031_goal_ceilings.py` (32 checks,
  deterministic, offline): unbounded default, set/get/remove,
  enforcement + exact boundary, multiple goals, cross-process race,
  high/low weight, increase/decrease/disable, restart, stale reclaim,
  floor+ceiling valid/invalid, telemetry, status, planning, forged
  events.

## Structured intelligence boundary (ADR-011)

```
Goal → ModelRouter → Structured Plan → Schema Validation
     → Capability/Authorization Validation → Orchestrator
```

- **Plan schema (`v1.0`):** versioned, strict, serializable. Contains intent,
  ordered steps (capability, action, params, verification, `depends_on`).
  Authorization fields (`scope`, `resource_kind`, `resource_param`, `risk`,
  `permissions`, `actor`, `approve`, ...) are **forbidden in the schema** —
  the model cannot set them.
- **PlanValidator:** validates capability/action existence, `param_schema`
  conformance (required keys, types, no injected arguments), and resource
  parameters against the live registry. Never grants permissions.
- **ModelRouter:** provider-neutral (`generate`, `plan_structured`).
  OpenAI-compatible adapter (stdlib HTTP; OpenAI/Azure/Ollama/LiteLLM/vLLM)
  requests structured JSON, then parses + strictly validates into PlanSchema —
  invalid responses are rejected. Credentials via `ARION_LLM_*` env vars.
  `DeterministicRouter.plan_structured` runs the same structured path offline.
- **Capability discovery:** the model sees a catalog built live from
  `registry.capabilities_summary()` (actions, scopes, risk, side effects,
  resource kind/param, param_schema, verification expectations) — never a
  hardcoded tool list.
- **Planners:** `DeterministicPlanner`, `RealModelPlanner`, and future
  planners share one `Planner` protocol.
- **Invariant:** the model proposes; the system authorizes. Model `scope`
  values never override the registry; the model cannot change `resource_kind`,
  bypass a boundary, approve itself, change actor, grant permissions, or
  create capabilities.
- **Observability:** `planning.requested`, `model.response.received`,
  `plan.validation.passed/failed` — provider/model/latency/token metadata
  only; raw prompts/responses are never persisted.

## Security boundary (first slice)

No shell, no writes, no network. Filesystem access is read-only and confined
to the repository root; every action passes the permission gate (ADR-006).

## Not built yet (by decision)

wake-word, voice pipeline, GUI, vector DB, RAG, browser automation,
unrestricted shell, large tool catalogs, multi-agent infrastructure,
provider-specific code. The spine must be proven first.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/arion run "summarize this repository"
.venv/bin/arion tasks
.venv/bin/arion resume <task_id>     # survives process restarts
.venv/bin/arion events --task <task_id>
.venv/bin/arion goals list           # durable goals (ADR-016)
.venv/bin/arion goals approve <goal_id>   # approve a pending approval (ADR-017)
.venv/bin/arion goals deny <goal_id>      # deny a pending approval (ADR-017)
.venv/bin/arion approvals list            # durable approval queue (ADR-018)
.venv/bin/arion approvals list --status expired   # expiry state (ADR-019)
.venv/bin/arion approvals approve <approval_id>
.venv/bin/arion goals show <goal_id> --json
.venv/bin/python scripts/demo_goal_replan.py   # 3-cycle DoD demo (offline)
.venv/bin/python scripts/demo_goal_approval.py  # approval + blocked-capability demo (offline)
.venv/bin/python scripts/demo_approval_queue.py  # durable approval queue + http.get demo (offline)
.venv/bin/python scripts/demo_adr016_goal_replan.py     # 3-cycle + restart + live-authz demo (offline)
.venv/bin/python scripts/demo_adr019_write_approval.py  # write approval A-E demo (offline)
.venv/bin/python scripts/demo_adr020_append_recovery.py # append + recovery A-E demo (offline)
.venv/bin/arion recovery list        # mutation recovery registry (ADR-020)
.venv/bin/arion recovery acknowledge <recovery_id>
.venv/bin/arion locks list              # advisory mutation locks (ADR-021)
.venv/bin/arion locks show <lock_id>
.venv/bin/arion locks reclaim <lock_id> # expired locks only; never authorizes
.venv/bin/python scripts/demo_adr021_lock_two_process.py  # two-process lock demo (offline)
.venv/bin/python scripts/demo_adr022_lock_wait.py         # bounded lock-wait demo A-C (offline)
.venv/bin/python scripts/demo_adr023_lock_fairness.py     # fair lock-wait queue demo A-E (offline)
.venv/bin/arion locks waiters        # tasks waiting (bounded) on mutation locks (ADR-022/023)
.venv/bin/arion locks queue <resource>  # durable FIFO wait queue for a resource (ADR-023)
.venv/bin/python -m pytest
```

State lives in `arion_data/` (gitignored).
