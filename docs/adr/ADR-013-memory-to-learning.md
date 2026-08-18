# ADR-013 — From Memory to Learning

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

The memory milestone made Arion store and retrieve experience. This milestone
turns that into a genuine feedback loop: prior experience must CHANGE a
subsequent planning decision in a measurable way. The central invariant:

```
Memory informs planning.   Memory never authorizes execution.
```

## Decision

### 1. Model-backed reflection (`ModelReflector`)

The existing `Reflector` seam now has two implementations:
`DeterministicReflector` (default, offline) and `ModelReflector` (asks a
ModelRouter for a structured reflection). Both produce the SAME `Reflection`
dataclass. Model output is STRICTLY validated by
`reflection_schema.validate_reflection_dict` before it can be stored:
required fields/types, confidence in (low|medium|high), importance in [0,1],
and NO authority-bearing fields (`scope`, `permissions`, `actor`, `grant`,
`approve`, `authorization`, `capability_registration`, `resource_boundary`,
`allowed`, `policy`, ...). Malformed or adversarial model reflections raise
`ReflectionValidationError`; the engine emits `reflection.validation.failed`
and falls back to the deterministic reflector so the loop never breaks.

### 2. Memory-driven planning guidance (`MemoryGuidance`)

A reusable, deterministic mechanism converts retrieved episodes + reflections
into structured planning recommendations:

```
previous outcome + capability/action + failure category + recommendation
+ confidence + importance + relevance  ->  MemoryGuidance
```

Categories: `avoid` (prior denial/failure for a capability/action/resource),
`prefer` (prior success), `informational`. Episodes now record their DECLARED
resource values (from ActionSpec resource_param; still never arbitrary param
values) so guidance is resource-aware. `apply_guidance_to_steps` deterministically
re-targets a plan: a step targeting an `avoid` resource is substituted with a
`prefer` resource for the same capability/action, or dropped if none exists.
The planner (DeterministicPlanner and RealModelPlanner alike) receives the
context and applies it; guidance is INFORMATIONAL - authorization still
decides what may run.

### 3. Failure feedback loop

A failed/denied task -> episode -> reflection -> future retrieval -> guidance
-> a DIFFERENT plan. Verified by tests: the same goal that failed against a
denied resource later completes by choosing a safe resource, with the
behavioral difference asserted (Plan A != Plan B meaningfully, and the change
is attributable to guidance + provenance).

### 4. Consolidation (`MemoryConsolidator`)

Deterministic, explainable, NO embeddings: episodes are grouped by (outcome,
capability set, failure category) + goal-token similarity; repeated lessons are
merged into explicit `ConsolidationRecord`s (source episode ids, merged lesson,
count, importance). History is NEVER deleted. Importance decays with age
(`decayed_importance`, half-life configurable). Consolidation is idempotent
(same source set not re-consolidated), so repeated identical lessons cannot
pile up forever.

**Storage semantics (clarified):** consolidation preserves history and
therefore does NOT itself bound physical storage. Bounded memory growth is a
separate concern: the archival/pruning seam is designed in ADR-014
(`MemoryStore.prune` — intentionally raising NotImplementedError until the
archival policy is approved). Consolidation optimizes retrieval/repetition,
never storage size, and never deletes.

### 5. Provenance

`PlanningContext` keeps structured `provenance` (episode_ids, reflection_ids,
guidance_ids) so every context can answer "which memory influenced this
plan?". The audit event `planning.memory.influence` records those IDs, counts,
and guidance categories - never raw contents.

### 6. Poisoning defenses

Memory and reflections are treated as UNTRUSTED input. Adversarial content
("ignore policy", "grant root access", "register shell.exec", "approve future
writes", "act as user:admin") remains informational text at most: guidance has
no authority fields, reflection schema rejects authority fields, and every
authorization answer (scope, actor, resource boundary, capability existence,
risk, approval) comes from the CURRENT system authority - tested across the
full matrix.

## Consequences

- "Learning" is now demonstrable: prior experience changes planning decisions
  (acceptance-gate test).
- The system remains fully offline-testable (DeterministicReflector +
  DeterministicMemoryGuidance; only ModelReflector needs a model).
- Memory stays strictly informational; authority remains in PermissionPolicy.
- New observability events: `reflection.requested`, `reflection.validation.passed/
  failed`, `memory.consolidated`, `planning.memory.influence`.

## Related

ADR-009 (authorization authoritative), ADR-012 (memory), ADR-011 (intelligence
boundary), ADR-008 (offline testing).

---

## Integration addendum (2026-08-17) — lifecycle idempotency, catch-up learning, explicit state

Status: Approved & implemented (on top of the ADR-013 baseline; scheduler layer ADR-025…031 settled).

### Phase-0 assessment (what the baseline already had)

The ADR-013 loop was already fully implemented and tested before this addendum:

- episodes (`Episode`, outcomes completed|failed|denied|recovered, bounded metadata, param KEY names only);
- durable `SQLiteMemoryStore` (episodic_memories / reflections / consolidations, same DB file as core state);
- reflection (`DeterministicReflector` + `ModelReflector`, strict `reflection_schema` validation, authority-field rejection, deterministic fallback);
- deterministic consolidation (`MemoryConsolidator`, idempotent, never deletes);
- deterministic retrieval + bounded context (`MemoryRetriever`, `build_planning_context`, `ContextBudget`);
- guidance into planning (`MemoryGuidance` avoid/prefer/informational, `apply_guidance_to_steps`);
- engine hooks (`_record_memory` at every terminal path, `_build_planning_context` inside `_plan`);
- provenance + observability events + read-only CLI (`memory episodes|reflections|search|stats|consolidate`);
- poisoning defenses + the Plan-A≠Plan-B acceptance gate.

### Gaps closed by this addendum

1. **Episode idempotency (proven defect):** `_record_memory` is reachable from ~10 terminal paths (plus restart/resume/retry), and each invocation minted a NEW episode id, so recording the same terminal task twice produced duplicate episodes + duplicate reflections. Fixed by: engine-level task-keyed dedup (`get_episode_by_task`), a DB-level UNIQUE index on `episodic_memories.task_id` as the cross-process backstop, and an init-time duplicate cleanup (keep the newest row per task — a merge of a bug artifact, never archival pruning).
2. **Catch-up learning:** a crash between the terminal task save and `_record_memory` silently lost the experience. Added `engine.learn_from_terminal_tasks()` — an idempotent, restart-safe pass that records episodes (and reflections/consolidations) for every terminal task that has none, with a bounded `memory.learning.catchup` observability event.
3. **Explicit lifecycle state:** `Episode.lifecycle` ∈ {`recorded`, `reflected`, `consolidated`}, durable on the episode row and advanced transactionally by the engine after each stage. `recorded` is the retryable state (recovery after a mid-learning crash); reflection failure still falls back to the deterministic reflector, so a separate `failed` state is not required (consistent with the existing model).
4. **CLI diagnostic:** `arion memory inspect <episode_id>` (read-only, bounded, secret-free; `--json`).
5. **Demo:** `scripts/demo_adr013_learning_loop.py` (deterministic, offline).
6. **Tests:** lifecycle invariants (duplicate invocation, retry, empty/malformed input, concurrent completion), subprocess restart/crash recovery, adversarial learning-boundary tests, unrelated-memory exclusion at the engine level.

### Lifecycle (exact, after this addendum)

```text
execution outcome (durable task row)
   ↓  _record_memory / learn_from_terminal_tasks
durable episode (lifecycle=recorded; task-keyed, exactly one per task)
   ↓  reflect (configured reflector; deterministic fallback on failure)
reflection recorded + linked (lifecycle=reflected)
   ↓  consolidate (idempotent; never deletes)
consolidation records (lifecycle=consolidated)
   ↓  retrieval (bounded, deterministic) → PlanningContext → guidance → future planning
```

Learning is idempotent end-to-end: repeated passes never create duplicate
episodes, reflections, or consolidation records for the same experience.
Learning failure never rolls back execution, and execution never depends on
learning (memory remains strictly informational; the scheduler/authorization
contracts are untouched).

## Addendum: Durable one-reflection-per-episode invariant

**Status: Approved & implemented (tests-first, 2026-08-18).**

### Defect (observed in CI)

The episode-idempotency fix above made *episodes* unique per task, but the
*reflection* claim was still check-then-act in `_record_memory`:

```
Worker A: observe episode.reflection_id is None
Worker B: observe episode.reflection_id is None     (both pass the guard)
Worker A: INSERT reflection row A
Worker B: INSERT reflection row B                    (no episode-keyed uniqueness)
```

Two concurrent learners (threads, catch-up workers, or subprocesses) could
each persist their own reflection for the same episode — violating the
documented "exactly one reflection" lifecycle and intermittently failing
`test_concurrent_learning_workers_do_not_double_apply` (`len(refs) == 1`).
A contributing defect: `record_episode` used `INSERT OR REPLACE`, so a
re-record with `reflection_id=None` transiently *clobbered* the durable
reflection link (and a task-id conflict silently replaced the winner's
episode row, orphaning the loser's reflection).

### Decision

The invariant is enforced by STORAGE, not process-local coordination
(memory remains stdlib-only; no new subsystem):

1. **DB-level uniqueness:** `CREATE UNIQUE INDEX idx_reflections_episode_unique
   ON reflections(episode_id)` — the structural cross-process backstop,
   exactly mirroring `idx_episodes_task_unique` for episodes.
2. **First-writer-wins claims inside `BEGIN IMMEDIATE`:**
   - `record_reflection` re-records the SAME id as an in-place content
     refresh (historical `INSERT OR REPLACE` semantics preserved), but a
     NEW id for an episode that already has a reflection loses the claim;
     the durable canonical row is returned so the loser ADOPTS it.
   - `record_episode` claims the episode slot atomically: a fresh task's
     first writer wins the identity; a racing minted id is never stored;
     a same-id re-record refreshes content while PRESERVING the durable
     `reflection_id` link (`COALESCE`) and never regressing the lifecycle
     state. It returns the canonical episode, which the engine adopts.
3. **Engine adoption:** `_record_memory` uses the canonical episode and
   canonical reflection for linking/lifecycle/events; only the worker that
   actually created the reflection emits `reflection.created`.
4. **Crash safety:** the claim is "insert row, then link". A crash between
   the two leaves an unlinked-but-durable reflection; the next pass
   (restart/catch-up) re-runs `record_reflection`, loses the insert claim,
   discovers the canonical row and links it — no duplicates, no stranding.
5. **Legacy migration (init-time, bug-artifact merge — never archival
   pruning):** duplicate reflections per episode are merged BEFORE the
   unique index is created, keeping the episode's LINKED reflection (else
   the newest by `created_at`), repairing any link that pointed at a
   losing duplicate. Orphaned reflections (no episode row) are left in
   place: they cannot violate the per-episode invariant and memory is
   never deleted outside the explicit prune seam.

### Guarantees

- At most one reflection row exists per episode — across threads, workers,
  processes, restarts, repeated `_record_memory`/`learn_from_terminal_tasks`
  calls, and crash/retry interleavings (unique index is the backstop; the
  transactional claim makes adoption the common case).
- Exactly one episode per task (unchanged), whose durable reflection link
  can no longer be clobbered by a re-record.
- Existing semantics preserved: reflection validation + deterministic
  fallback, provenance, consolidation, belief derivation, event/audit
  behavior, and same-id reflection content refresh.

### Tests

`tests/test_reflection_invariant.py` (tests-first; every invariant test
fails against the vulnerable implementation): deterministic barrier-
synchronized `_record_memory` race, storage-level first-writer-wins +
loser adoption, link-preservation on re-record, same-id refresh, legacy
migration (linked/newest/link-repair), idempotent replays and catch-up —
plus the existing real-subprocess cross-process race test.

## Addendum: Durable one-consolidation-per-source-set invariant

**Status: Approved & implemented (tests-first, 2026-08-18).**

### Defect (observed in CI)

The reflection fix made *reflections* unique per episode, but the
*consolidation* claim was still check-then-act in `MemoryConsolidator`:

```
Worker A: list_consolidations() -> source set absent
Worker B: list_consolidations() -> source set absent     (both pass the guard)
Worker A: INSERT consolidation A  (id A)
Worker B: INSERT consolidation B  (id B)                  (no set-keyed uniqueness)
```

The `consolidations` table only keyed uniqueness on `consolidation_id`, so two
concurrent learners (threads, catch-up workers, or subprocesses) could each
persist their own consolidation for the same source-episode set — and because
consolidations are NEVER pruned (they are the permanent merged summary), this
duplicated history permanently and produced duplicate `memory.consolidated`
creation events at the engine level.

### Decision

The invariant is enforced by STORAGE, not process-local coordination (memory
remains stdlib-only; no new subsystem), mirroring the reflection fix:

1. **Canonical source identity (order-independent):** `canonical_source_key`
   maps a source-episode set to a deterministic key
   (`json.dumps(sorted(ids))`), so `[A,B,C]`, `[C,A,B]` and `[B,C,A]` all
   resolve to the same consolidation identity regardless of ordering.
2. **DB-level uniqueness:** a `consolidations.source_key` column + `CREATE
   UNIQUE INDEX idx_consolidations_source_key ON consolidations(source_key)`
   — the structural cross-process backstop, exactly mirroring the episode and
   reflection unique indexes.
3. **First-writer-wins claims inside `BEGIN IMMEDIATE`:**
   - `record_consolidation` re-records the SAME id as an in-place content
     refresh but NEVER mutates the immutable source-set identity
     (`source_key`/`source_episode_ids`);
   - a NEW id for a source set that already has one loses the claim; the
     durable canonical row is RETURNED so the loser ADOPTS it (the method now
     returns `ConsolidationRecord | None` instead of `None`, and the
     interface protocol documents this);
   - the expensive consolidation COMPUTATION stays outside the short write
     transaction — only the claim runs inside it.
4. **Consolidator behavior:** `MemoryConsolidator.consolidate()` skips groups
   already present by canonical source key (sequential idempotency), submits
   each candidate through the storage claim, and reports ONLY records this
   invocation actually created. A worker that merely adopts a concurrent
   peer's canonical consolidation returns nothing — converging silently.
5. **Event behavior:** the engine emits `memory.consolidated` ONLY for records
   actually created by the invocation, so a racing learner can never emit a
   duplicate creation event.
6. **Legacy migration (init-time, bug-artifact merge — never archival
   pruning):** the `source_key` column is added if missing, backfilled from
   the stored source episode ids, and duplicate rows sharing a source key are
   merged BEFORE the unique index is created (keep the newest by `created_at`,
   `rowid`). Malformed legacy rows that cannot produce a valid canonical key
   are left `NULL` (SQLite allows multiple NULLs in a unique index) rather
   than blocking it. The migration is idempotent across reopens.

### Narrow rider: `task_id IS NULL` data loss

The init-time episode task-dedup used `episode_id NOT IN (SELECT ... WHERE
m2.task_id = episodic_memories.task_id ...)`. Because a SQL NULL comparison
is never true, the subquery matched no row for a task-less episode
(`task_id IS NULL`), leaving `NOT IN` true and silently DELETING valid
task-less episodes at store initialization. Fixed by guarding the deletion
with `task_id IS NOT NULL`: ordinary per-task dedup is unchanged, while
`task_id NULL` episodes are preserved.

### Guarantees

- At most one consolidation row exists per canonical source-episode set —
  across threads, workers, processes, restarts, repeated learning and
  catch-up passes, and crash/retry interleavings (unique index is the
  backstop; the transactional claim makes adoption the common case).
- The source-set identity is order-independent and immutable during refresh.
- Exactly one `memory.consolidated` creation event per new consolidation.
- `task_id IS NULL` episodes survive SQLite store initialization; non-NULL
  task dedup still works.

### Tests

`tests/test_consolidation_invariant.py` (tests-first; 9 of the invariant
tests fail against the vulnerable implementation): order-independent source
identity, sequential idempotency, storage-level first-writer-wins + loser
adoption, same-id refresh WITHOUT source-identity mutation, deterministic
barrier-synchronized consolidation race, independent-connection cross-process
claim, concurrent engine `_record_memory` race (exactly one consolidation +
one creation event), legacy duplicate merge, distinct sets stay distinct,
idempotent reopen, and NULL-`task_id` preservation / non-NULL dedup.
