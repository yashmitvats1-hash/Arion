# Arion — Phase Transition Audit Report

- **Date:** 2026-08-29
- **Scope:** Read-only audit of the codebase at `6f7e493a7a9c26b31697e42a86bcf9109c8ff5a4`
  (branch `arena/01a04d3d-arion`, HEAD == `origin/main`).
- **Method:** repository/Git state verification, full authoritative test run, coverage
  measurement, source-level tracing of the execution flows, ADR-by-ADR status review,
  executable verification of demos and CLI surfaces.
- **Rule:** no code was modified. Working tree is clean at the end of the audit.

---

## 1. Executive Summary

Arion is a **single-process, SQLite-backed agentic spine (v0.1)** — not a chatbot.
The prior stabilization phase is complete: **all 56 ADRs (001–056) are marked
Approved & implemented**, the full test suite is green (**1,459 passed, 2 skipped,
0 failed, 0 errors, ~155 s, exit 0**), 22 of 23 offline demos pass, and the CLI
works end-to-end. The deterministic core (orchestration loop, authorization,
approval, recovery, mutation locks, durable scheduler with weights/reservations/
ceilings, memory, cognition) is extensively and adversarially tested with **zero
runtime dependencies** (stdlib only).

The audit confirmed **two small but real defects** (a broken demo script for ADR-026
and a strategy-selector false positive that mislabels goals mentioning bare dotted
tokens such as `README.md`), plus **documentation drift** in `docs/architecture.md`.

The defining characteristic of the system today: **everything is proven without a
model, and the model-backed path is the only materially unproven surface.** The
model components (`RealModelPlanner`, `OpenAICompatModelRouter`, `ModelReflector`)
exist behind clean seams and are unit-tested, but they are **not wired by default**,
and the complete provider→plan→execute path is exercised only by a single gated
smoke test. That is the highest-value frontier for the next phase.

---

## 2. Repository / Git State

| Item | Finding |
|---|---|
| Branch | `arena/01a04d3d-arion` (session branch; main is `6f7e493`) |
| HEAD | `6f7e493a7a9c26b31697e42a86bcf9109c8ff5a4` — "Merge pull request #9 from yashmitvats1-hash/arena/01a03c0e-arion" (2026-08-26) |
| Working tree | **clean** (`git status` → "nothing to commit") |
| Remote | `origin` → `https://github.com/yashmitvats1-hash/Arion.git` |
| Local vs remote | `git ls-remote origin` HEAD == local HEAD == `origin/main` == `6f7e493` — in sync |
| History depth | **Shallow clone** (`.git/shallow` present); exactly **1 commit** in local history |
| Tracked files | 298 (`.git` ≈ 1.2 MB) |
| Stash | empty |

**Notable:** the checkout is shallow, so ADR cross-references to prior commits
(`0c0f020`, `7e2a87c`, `8b87982`, `504d252`, `534f7d6` …) cannot be verified
locally. `origin` carries many `arena/01a*` feature branches (prior phase branches).
No Git state was altered (no reset/rebase/merge/amend).

---

## 3. Test Baseline

**Authoritative command** (per README / pyproject):
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

**Exact result (run twice):**

| Metric | Value |
|---|---|
| Collected / passed | **1,459 passed** (1,419 test functions across 157 files incl. smoke) |
| Skipped | **2** |
| Failed / errors | **0 / 0** |
| Duration | 150–155 s |
| Exit code | **0** |

**The two skips (both benign, both by design):**
1. `tests/smoke/test_live_provider.py:36` — gated smoke for the real
   OpenAI-compatible provider path; skips unless `ARION_LLM_BASE_URL` /
   `ARION_LLM_API_KEY` are set (ADR-008: no network in normal runs).
2. `tests/test_capabilities.py:53` — symlink-escape boundary test; skips on
   platforms without symlink support.

**Coverage (measured with `coverage`):** **87%** overall
(10,346 statements, 1,352 missed). Lowest modules: `engine.py` **79%**,
`capabilities/filesystem.py` 80%, `cli.py` 81%, `capabilities/http.py` 82%,
`orchestration/scheduler.py` 82%, `router.py` 83%.

**Real cross-process coverage exists:** `test_multi_process_scheduler.py` (10
tests), `test_weighted_cross_process.py` (6), `test_capacity_planning_cross_process.py`
(4), `test_lock_fairness_subprocess.py` (2), `test_lock_waiting_subprocess.py` (2)
spawn real subprocesses against a shared DB.

**Demo scripts (executable evidence):** 22 of 23 pass (details in §6).
The failing demo is a confirmed defect (§6.1).

**Validation gates in the repo:** `pytest` is the only automated gate.
**No CI workflow exists** (no `.github/workflows`), and there is **no lint/type
configuration** (no mypy/ruff config in `pyproject.toml`) and no `LICENSE` file
(`pyproject.toml` declares `license = "Proprietary"`).

---

## 4. Architecture State

### 4.1 Layers (ADR-001, implemented)
```
INTERFACES      CLI (only; voice/vision/GUI/API are future, by decision)
ORCHESTRATION   ArionEngine (state machine/loop), authz, scheduler, runtime lifecycle
INTELLIGENCE    Planner protocol + DeterministicPlanner/RealModelPlanner;
                ModelRouter protocol + DeterministicRouter/OpenAICompatModelRouter;
                PlanSchema v1.0 + PlanValidator (never owns the loop)
CAPABILITIES    CapabilityRegistry + ActionSpec; filesystem.read/write/append,
                git.log, http.get (read-only + 2 mutating, all sandboxed)
STATE           SQLite (WAL) behind Storage protocol: goals, tasks (full JSON
                snapshots + revision CAS), checkpoints, audit_events, approvals,
                recoveries, mutation locks + waiters, scheduler work/config/
                weights/state/reservations/ceilings/events, memories, reflections,
                consolidations, beliefs, preferences, environment facts,
                goal_plans, strategy_outcomes
```

### 4.2 Execution / control flow (traced in source)
`ArionEngine.run_goal` (claim per-goal run lease, ADR-045/052) →
`_run_goal_owned` long-horizon loop (evaluate → `none|paused|await_approval|
await_lock|resolve_blocker|complete|replan|continue|initial_plan`) →
`_plan_for_goal` (stored-plan fast path / `create_task` + `_plan`; immutable plan
version claimed in `BEGIN IMMEDIATE`, ADR-050/051) → `_run_task_owned`
(checkpoint restore, superseded-plan fence ADR-049, pause boundary ADR-047,
terminal-row authority) → scheduler admission (`_admit_step` → durable
`scheduler_work` claim with lease, ADR-025/026/042) → `_run_step_worker` →
`_execute_step` (capability discovery → **live policy decision** → approval
handling) → `_execute_with_retries` (mutation lock AFTER authorization with
bounded wait/FIFO, ADR-021/022/023; heartbeat + final renewal, ADR-039) →
`_execute_attempts` (retry gated on `ActionSpec.retry_safe`; per-step terminal
status persisted immediately, ADR-040/041) → `_verify` (non_empty / schema_keys /
write_verified / append_verified) → `_checkpoint` (full snapshot, bounded history
8, ADR-036) → memory recording / belief derivation / consolidation.

Key invariants verified in code and tests: the LLM never owns the loop; every
step is re-authorized against live `ActionSpec` metadata; authorization precedes
lock acquisition; locks are coordination only; terminal task rows are immutable;
goal completion/failure is plan-version-fenced (ADR-054/055/056).

### 4.3 Persistence & state management
Single SQLite DB file (`arion_data/arion.db` by default), WAL mode. Tasks are
full JSON snapshots with a monotonic `revision` CAS column; checkpoints are full
snapshots bounded to the newest 8 per task. Cross-process atomicity via
`BEGIN IMMEDIATE`. In-process thread safety via an RLock-guarded single
connection per store (`SQLiteStorage`, `SQLiteCognitiveStore`, `SQLiteMemoryStore`
— ADR-024/026). Recovery: crash-consistent ordering of task/recovery/approval/
scheduler commits (ADR-041), lease reclaim for locks/schedulers/goal-runs.

### 4.4 Agent/task orchestration
`GoalManager` (authoritative goal state machine: ACTIVE/PAUSED/BLOCKED/COMPLETED/
FAILED/CANCELLED, versioned CAS), `DeterministicProgressEvaluator`, `StrategySelector`
(5 rules + outcome-history preference layer), `WorldStateMonitor` (versioned
environment facts → replan signals), plan versioning with `readopt_plan`
(rollback) + `diff_plans`, `pending_task` canonical-selection (ADR-053).

### 4.5 LLM/provider integration
`OpenAICompatModelRouter` (stdlib HTTP; OpenAI-compatible chat completions with
`response_format=json_object`; injectable transport; typed errors; bounded
metadata events). `RealModelPlanner` (catalog from live registry → schema → strict
validation → memory-guidance transformation). `ModelReflector` (validated,
authority-free reflections; falls back to deterministic). **None of these are
wired in `bootstrap.py`** — defaults are the deterministic variants; provider
configuration is via `ARION_LLM_*` env vars only, and the sole live validation is
the gated smoke test.

### 4.6 API/interface boundaries
CLI only (`arion` console script, ~60 subcommands: `run, resume, status, tasks,
events, capabilities, memory, cognition, goals, approvals, recovery, locks,
scheduler` with `--json` on most). No HTTP/API server, no daemon, no async.
Protocol seams (`Storage`, `Planner`, `ModelRouter`, `Reflector`, `MemoryStore`,
`ApprovalStore`, `RecoveryStore`, `MutationLockStore`, `SchedulerRegistry`,
`PermissionPolicy`, `ApprovalHandler`, `ResourceBoundary`) are the extension
points; `bootstrap.py` is the composition root.

### 4.7 Recovery / concurrency mechanisms
- At-least-once step execution with checkpoint resume (interrupted non-retry-safe
  mutations create durable `MutationRecovery` REQUIRED records; gated replanning;
  operator acknowledgement, ADR-020/043).
- Durable cross-process advisory mutation locks with leases, exact-owner
  heartbeat/renewal, FIFO wait queues with bounded deterministic backoff
  (ADR-021/022/023/039).
- Durable cross-process scheduler registry: registrations + lease heartbeats,
  atomic claims, atomic handoff, stale reclaim, global capacity, fair share,
  DWRR weights, reservations (floors), ceilings (ADR-025…031, 042).
- Per-goal run leases with ownership fencing through every boundary (ADR-045/046/052).
- Plan-version fencing of terminal transitions, atomically inside the goal CAS
  (ADR-054/055/056).

### 4.8 Security boundaries
- Authorization is the only execution authority: `PermissionPolicy` over
  `Capability → Action → Resource → Parameters`; scope comes from live
  `ActionSpec`, never the plan; fail-closed resource boundaries; actor delegation
  chains; canonical authorization fingerprints; approval is re-validated against
  fingerprints and live metadata (ADR-009/018/019).
- No shell/subprocess/`eval` anywhere in `arion/` (verified by grep).
- Capability containment (sandbox root, symlink/traversal escapes) is a separate
  layer from policy.
- Sensitive error boundary (ADR-034), resource presentation boundary (ADR-037),
  observation retention budget (ADR-035), bounded event payloads (ADR-033),
  memory/cognition are informational and can never authorize (adversarial tests).
- Default policy: read-only scopes allowed; **writes/append/HTTP denied by
  default** (fail closed).

### 4.9 Configuration / deployment model
Configuration is **code + env vars** (`ARION_LLM_*`), CLI flags (`--db`, `--json`,
per-command flags), and engine constructor injection. No config file, no secrets
management, no service boundary, no packaging beyond `pip install -e`. Runtime
ownership is explicit via `ResourceLifecycle` (ADR-032).

### 4.10 Extension points
Protocol seams listed in §4.6; capability registration in `bootstrap.py`;
`CapabilityRegistry.capabilities_summary()` feeds planner discovery; event sink
interface (`EventSink`, required vs best-effort mirrors); `CheckpointRetentionStore`
capability; injectable clock/sleeper/transport for determinism.

---

## 5. ADR State

- **56 ADRs (001–056), all with explicit status lines.**
  - ADR-001…011: **Approved** (foundational baseline).
  - ADR-012…056: **Approved & implemented** (dated 2026-08-09 → 2026-08-26).
- **Implementation matches decisions** for every ADR verified in source (each
  ADR's mechanisms exist with the documented authority boundaries; newest ADR
  tails record cumulative suite counts consistent with today's 1,459).
- **Doc-structure anomaly:** ADR-013 and ADR-014 are multi-addendum documents
  with several "Status:" lines each (e.g. ADR-014 line 96 "Status: design
  approved; implementation follows…" is an in-document section header predating
  the final "Approved & implemented"). Functional status is unambiguous, but the
  files have accreted into multi-document artifacts.
- **Obsolete/assumption drift:** the "Security boundary (first slice)" section in
  `docs/architecture.md` still reads "No shell, **no writes, no network**",
  which contradicts the implemented `filesystem.write` / `filesystem.append` /
  `http.get` capabilities (they exist and are default-DENIED). The same file
  claims the ADR-026 demo runs "33 checks, deterministic, offline" — it currently
  crashes (§6.1).
- **Explicitly deferred (defining the phase boundary):** voice/wake-word, GUI,
  browser automation, unrestricted shell, RAG/vector DB, multi-agent swarm,
  autonomous daemon, self-modifying code, async event transports, distributed
  locks/queues (Redis/etcd), lock jitter, per-action observation budgets,
  `audit_events.goal_id` index (ADR-016 G5), delta checkpoints/event sourcing,
  vaults/secret-reference resolution, historical row rewrites, notification
  delivery + human identity authentication (ADR-044), separation of the event
  store (ADR-032), model-backed evaluation (ADR-016).

---

## 6. Actual Capability Matrix

Legend: ✅ working (executable evidence) · 🟡 implemented but incomplete /
not wired by default · ⛔ absent by decision · ❌ confirmed defect.

| Capability / surface | Status | Evidence |
|---|---|---|
| `filesystem.read` (list/read) | ✅ | tests; live `arion run` on this repo; sandbox/symlink/size caps |
| `filesystem.write` (write) | ✅ (default-DENIED, approval-gated) | ADR-019 demo (26 checks); write tests; `mutation.*` audit events |
| `filesystem.append` (append) | ✅ (default-DENIED) | ADR-020 demo (25 checks); recovery registry tests |
| `git.log` (log/branches) | ✅ | `.git`-metadata parser, no shell; gitlog tests; CLI run |
| `http.get` (get) | ✅ (default-DENIED: no `UrlBoundary` by default) | ADR-018 queue demo; redirect containment; tests |
| DeterministicPlanner / DeterministicRouter | ✅ default path | entire suite |
| RealModelPlanner + OpenAICompatModelRouter | 🟡 implemented + unit-tested; **not wired by default**; live path = 1 gated smoke test (skipped here) | `tests/test_model_planner.py`, `test_model_router.py`, `test_model_backed_planner.py`, `tests/smoke/test_live_provider.py` |
| ModelReflector | 🟡 implemented + tested; **not wired** (bootstrap uses DeterministicReflector) | `test_reflection_validation.py`; `memory/model_reflector.py` |
| GoalManager lifecycle, replan, rollback, diff, blockers | ✅ | goal lifecycle/replan/readopt tests; CLI `goals *` |
| Approval queue + atomic decisions + expiry | ✅ | approval/atomic/compat tests; CLI `approvals *`; cross-process demo |
| Mutation recovery registry (gated replanning) | ✅ | recovery tests + CLI `recovery *`; ADR-020 demo |
| Mutation locks + leases + FIFO waiters + backoff | ✅ | lock suite incl. subprocess tests; CLI `locks *`; demos ADR-021/022/023 |
| In-process concurrency (bounded threads) | ✅ | `test_concurrency_model.py` (830 lines), ADR-024 demo |
| Cross-process scheduler: claims, leases, capacity, DWRR, reservations, ceilings, telemetry | ✅ | scheduler suites + real subprocess tests; demos ADR-026…031 |
| Memory: episodes/reflections/consolidation/retrieval/guidance/prune | ✅ | memory suites; CLI `memory *`; ADR-013 demo |
| Cognition: beliefs/preferences/environment facts/goal plans/strategy outcomes | ✅ | cognition suites; CLI `cognition *`; ADR-014/015 demos |
| Audit trail: `audit_events` + JSONL mirror + scheduler telemetry | ✅ | event-contract tests; `arion events` |
| CLI (~60 subcommands, `--json`) | ✅ | CLI test suites; live invocation |
| Health/lifecycle (`engine.health()`, shutdown) | ✅ | runtime lifecycle tests |
| Offline demos (23 scripts) | ✅ 22 pass / ❌ **1 fails** | see §6.1 |
| Interfaces: voice, vision, GUI, HTTP API | ⛔ absent by decision (ADR-012/011 "Not built") | — |
| CI workflow, lint/type-check gate, LICENSE file | ⛔ absent | repo inspection |
| README "JARVIS/FRIDAY-class autonomous personal computing" | ⚠️ aspirational framing; actual surface = sandboxed repo operations + gated HTTP | — |

### 6.1 Confirmed defects

**D1 — `scripts/demo_adr026_cross_process_scheduler.py` crashes (exit 1).**
Reproduced twice. At step "A" the demo claims a work row with the deterministic
clock `T0` (lease expires `2026-01-01T00:01:10`), then calls
`store.mark_terminal(row.work_id, COMPLETED, owner_worker_id="worker:1")`
(demo line 225) **without `now=`**, so the store defaults to the real clock
(`utcnow()`), sees the lease expired, and raises
`SchedulerStateError: stale owner … lease expired … (fail closed)`.
`mark_terminal` (store.py:2088) correctly implements ADR-042 semantics
(`lease_expires_at > now` required); the **demo predates ADR-042 and was never
updated**. The later scheduler demos (ADR-027…031) pass. `docs/architecture.md`
advertises this demo as "33 checks, deterministic, offline" — false today.

**D2 — Strategy selector false positive: bare dotted tokens treated as missing
capabilities.** `StrategySelector.select` rule 1
(`arion/cognition/strategy.py:180-190`) builds
`needed = {w for w in goal.lower().split() if "." in w and "/" not in w}` and
reports any such token not in `registered_capabilities` as missing. Reproduced:
goal `"summarize the README.md"` → strategy `blocked_missing_capability`,
`missing_capabilities: ["readme.md"]`; `"read docs/design.md"` → `direct`
(correct only because `/` excludes it). Impact today is **informational only**
(the engine gates on `planner.required_capabilities()`, which is correct), but
the goal's durable strategy + `strategy_outcomes` history are mislabeled for very
common goal phrasings ("read package.json", "check pyproject.toml"), which can
mislead strategy-escalation and outcome learning. No test pins this case.

**D3 — Documentation drift.** `docs/architecture.md` "Security boundary (first
slice): No shell, no writes, no network" contradicts the implemented
write/append/http capabilities (default-deny); the ADR-026 demo claim is stale
(D1); the interface diagram lists voice/vision/GUI/API without marking them
absent in context of the current milestone (they are "Not built yet (by
decision)").

### 6.2 Hypotheses (not confirmed defects)

- `engine.py` (5,426 lines) and `store.py` (4,499 lines) are very large;
  maintainability risk, no functional impact observed.
- Coverage blind spots: `engine.py` 79 %, `cli.py` 81 %, live-provider path
  unexercised in normal runs; adversarial/error branches are the likely gaps.
- No CI/lint/type gates → the green suite is only as repeatable as the operator
  running it.
- Single-RLock store connections serialize in-process writes; SQLite single-file
  WAL is the throughput ceiling — acceptable at this phase, a scaling constraint
  later.
- Zero runtime dependencies (stdlib only) is a deliberate, documented choice
  (ADR-002) with no third-party risk but also no integration with richer
  ecosystems.

---

## 7. Production-Readiness Assessment

**Strengths (confirmed):** exceptionally disciplined authority model (execution,
authorization, coordination, memory all separated and adversarially tested);
crash consistency is engineered (commit ordering, leases, CAS everywhere);
default-deny security posture; deterministic testability; clean protocols/seams;
bounded observability; graceful shutdown; restart-safe learning.

**Gaps / risks (ranked):**
1. **Model-backed path unproven end-to-end** — the only surface validated by a
   single gated smoke test; everything else is deterministic/mocked. Highest
   risk for any "intelligence" claim.
2. **No CI / no lint / no type gate** — the 1,459-test gate is manual only;
   regression risk on the largest files.
3. **Strategy-selector heuristic defect (D2)** — pollutes durable strategy
   state/outcome history.
4. **Broken demo (D1) + doc drift (D3)** — undermines the repo's own
   verification story.
5. **Observation/result storage unbounded per-task until retention** (checkpoints
   bounded at 8 since ADR-036; memory pruning available; but `audit_events` and
   `scheduler_events` grow — prune exists only for scheduler events and memory).
6. **CLI-only interface, no notification path** — approvals can sit pending with
   no push channel (deferred by ADR-044).
7. **No packaging/ops story** (no CI, no versioned release process, proprietary
   license declared but no LICENSE file).

---

## 8. Next-Phase Frontier

### 8.1 What is Arion today?
A single-process, zero-dependency, SQLite-backed agentic spine: durable
goal/task lifecycle with checkpoint recovery, fail-closed authorization,
approval queue, mutation-recovery fencing, cross-process coordination
(locks, scheduler, capacity policy), memory + cognition learning, rich audit,
and a CLI. It can inspect a sandboxed repository, perform approved sandboxed
writes/append, fetch allowlisted HTTP, and learn from experience — all with no
model required.

### 8.2 What is genuinely complete?
The spine: orchestration, authorization, approvals, recovery, mutation safety,
concurrency (in-process + cross-process), scheduler policy (weights/reservations/
ceilings), observability, memory/cognition, CLI, determinism discipline, and an
exceptionally strong deterministic test suite (1,459 green, 87 % coverage,
real-subprocess races). All 56 ADRs implemented.

### 8.3 What is still missing?
1. **A wired, validated model-backed path** (planner + router + reflector as
   configurable first-class citizens with deterministic fallback).
2. Anything beyond the CLI (API/server, notifications).
3. CI/lint/type automation.
4. A broader capability catalog (the 5 sandboxed capabilities bound what
   "autonomous computing" can do).
5. Config/secrets management and deployment packaging.
6. The two confirmed defects + doc drift fixed.

### 8.4 Biggest technical constraint
**The model-integration path is the least-proven surface in an otherwise
rock-solid system.** Every authority and coordination mechanism has been hardened
and tested deterministically; the one place where reality (an external,
non-deterministic provider) enters the loop is exercised by a single opt-in
smoke test, and the model components are not wired by default. The next level
(JARVIS/FRIDAY-class operation) cannot be claimed until the model-driven loop is
as trustworthy as the deterministic one. Secondary constraint: the tiny
capability catalog limits what any intelligence can actually accomplish.

### 8.5 What should the next phase accomplish?
**Prove the model-backed loop as a first-class, configurable path with a
deterministic fallback**, without weakening any existing authority boundary —
then fix the confirmed defects and close the validation-automation gap.
Concretely: wire `RealModelPlanner` + `OpenAICompatModelRouter` (+ `ModelReflector`)
through bootstrap/CLI/config; add a repeatable live-provider validation harness
(not a single gated test) covering plan quality, schema rejection, error paths,
and latency/token metadata; keep the LLM-owned-the-loop invariant and the
authorization invariants untouched; fix D1/D2/D3; add CI so the gate is
repeatable.

### 8.6 What should explicitly NOT be worked on yet
Voice/wake-word, GUI, browser automation, unrestricted shell, RAG/vector DB,
multi-agent swarm, autonomous daemon, self-modifying code, async event
transports, distributed coordinators, Postgres/vector stores, schema rewrites,
new mutating capabilities, or any change to the authority model.

### 8.7 Proposed milestones
- **M0 (hygiene):** fix D1 (demo), D2 (strategy selector), D3 (docs). ~small.
- **M1:** ADR-057 — "Model-backed intelligence as a first-class optional path"
  with acceptance criteria and explicit non-goals.
- **M2:** provider configuration (env/CLI) + planner/router selection with typed
  failure → deterministic fallback; model wiring behind existing seams.
- **M3:** repeatable live-provider validation harness (offline-skippable, scripted
  scenarios) replacing the single smoke test; wire `ModelReflector` with
  validation + fallback.
- **M4:** end-to-end proof: model-driven goal → approval → write/append → memory
  → learning across sessions; adversarial suite proving model output still cannot
  authorize, escalate, or forge state.
- **M5:** CI (pytest + demo smoke on push) and coverage gate.

---

## 9. Evidence / References

- Git: `git status/branch/remote/ls-remote`, `.git/shallow` (shallow clone).
- Tests: full `pytest` run (1,459/2/0, exit 0, ~155 s); `coverage report` 87 %.
- Demos: all 23 run; 22 pass; `demo_adr026_cross_process_scheduler.py` exit 1
  (`SchedulerStateError: stale owner: work sw_… lease expired 2026-01-01 …`).
- Strategy bug reproduction: `StrategySelector.select("summarize the README.md", …)`
  → `blocked_missing_capability {'missing_capabilities': ['readme.md']}`; live
  `arion run "summarize the README.md"` recorded the same strategy on a completed
  goal (`strategy.selected` event detail).
- Flow tracing: `engine.py` (`run_goal`, `_run_goal_owned`, `_plan_for_goal`,
  `_run_task_owned`, `_admit_step`, `_run_step_worker`, `_execute_step`,
  `_execute_with_retries`, `_execute_attempts`, `_verify`, `_checkpoint`);
  `store.py` (DDL, `mark_terminal`, `cas_goal_terminal_fenced`, claim gates);
  `goals.py` (`GoalManager`), `strategy.py` (`StrategySelector`), `planner.py`
  (`DeterministicPlanner`, `planner_requirements`), `authz.py` (`ResourcePolicy`,
  `ApprovalHandler`), `scheduler.py`, `locks.py`, `approvals.py`, `recovery.py`,
  `scheduler_work.py`, `bootstrap.py`, `cli.py`, `lifecycle.py`,
  `openai_compat.py`, `model_planner.py`, `model_reflector.py`.
- ADR statuses: grep over `docs/adr/ADR-*.md` (56 files, all with Status lines);
  deferred lists in ADR-011/012/016/019/020/021/022/023/032/035/036/037/044.
