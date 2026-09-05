# M7 Capability Audit — 2026-09-05

- **Status:** Working document (uncommitted). Not an ADR. No decision taken.
- **Baseline:** `6d8e477` (M6-A.1), one commit ahead of `main` @ `898127b`
- **Method:** read-only inspection plus *live execution* of the real CLI. Every
  behavioural claim below was produced by running Arion, not by reading it.
- **Framing:** behavioural reach, not LOC. A subsystem that is beautifully
  implemented but not connected to a useful end-to-end loop is scored as
  **not a capability**.

---

## 0. The headline finding

Arion's action vocabulary is **7 actions**. Its operator surface is **78 CLI
subcommands**. The ratio is roughly **11:1 governance-to-action**.

```
ActionSpec vocabulary (complete):   CLI subcommands:  78
  filesystem.read   read, list      Capability actions: 7
  filesystem.write  write
  filesystem.append append
  git.log           log, branches
  http.get          get
```

Everything Arion can *do* to the world is: read a file, list a directory,
write a file, append to a file, read git metadata, and perform an HTTP GET.
There is no delete, move, copy, mkdir, no shell, no process execution, no
structured search, no HTTP POST, no email/message send, no scheduled trigger.

The orchestration substrate beneath those 7 actions supports weighted fair
scheduling, per-goal capacity reservations, concurrency ceilings, durable
FIFO lock queues with waiter fairness, cross-process lease fencing, plan
version fencing, and atomic terminal transition lineage fences.

**This is the central asymmetry of the project.** The substrate is not
under-built; it is under-fed.

---

## 1. Current capability map

### 1.1 What Arion can genuinely execute today

Verified by running `arion run` against the real repo:

| Goal | Result | Real effect |
|---|---|---|
| `inspect the repository` | COMPLETED | list root, read key files |
| `summarize the git history` | COMPLETED | git log + branches |
| `count the lines in README.md` | COMPLETED | **read only — did not count** |
| fetch an http(s) URL | plans `http.get` | bounded GET, allowlisted |

### 1.2 What requires human intervention

- **Every high-risk mutation.** `filesystem.write` / `append` are `risk="high"`
  and route to the durable approval queue. Correct and deliberate.
- **Resolution of every approval** — CLI, or the M6-A HTTP API, or (M6-B)
  after a webhook tells a human to go look. Arion never self-authorizes.
- **Recovery acknowledgement**, **lock reclamation**, **stale scheduler work
  reclamation** — all operator verbs, none automatic.

### 1.3 Architecturally present, behaviourally dormant

This is the section the audit exists for.

| Subsystem | Status | Evidence |
|---|---|---|
| **Notifications (M6-B)** | Fully built, **disabled by default** | `load_webhook_config().enabled` false unless configured; ~1,400 LOC + 467 LOC admin API that a default operator never touches |
| **Model-backed planning** | Real, **unexercised by default** | `RealModelPlanner` + OpenAI-compatible router with retry/backoff exist and are wired in `bootstrap`, but only activate when `ARION_LLM_*` is set. The 2 skipped tests are the only live-provider coverage |
| **Scheduler policy suite** | Built, **no workload to govern** | weights, reservations, ceilings, capacity planning, dry-run `plan` verbs — ~20 CLI subcommands governing contention that a 7-action agent cannot generate |
| **Cross-goal concurrency** | Correct, **rarely reachable** | needs multiple long-running goals contending on shared resources; the action vocabulary barely produces this |
| **Memory guidance loop** | **Genuinely closes** — see §3 | not dormant; the exception that proves the rule |

---

## 2. End-to-end user journeys

Traced by execution.

### J1 — "inspect the repository" ✅ works
user → `submit_goal` → `DeterministicPlanner` keyword match (`inspect`) →
2 steps (`list`, `read`) → capability execution → `non_empty` /
`schema_keys` verification → episode + reflection persisted → COMPLETED.
**The full loop runs.** This is the journey the whole spine was built for.

### J2 — "summarize the git history" ✅ works
Keyword `history` → `git.log`/`branches` → COMPLETED. Note: "summarize"
produces *retrieval*, not a summary. No step synthesises anything.

### J3 — "count the lines in data.txt" ⚠️ completes without doing the task
Falls to the regex fallback `(\S+\.\w+)` → plans a single `read` →
verification is `schema_keys: ["content"]` → content is non-empty → **step
SUCCEEDED, task COMPLETED**. Nothing counted lines. Arion reported success
for work it did not perform.

### J4 — "rename README.md to ARCHIVE.md" ❌ **false success**
```
task task_1d7546114ed7: COMPLETED
  [ok] step 0: read file (filesystem.read/read)
$ ls ARCHIVE.md → No such file or directory   (README.md untouched)
```
The regex grabbed `README.md`, planned a *read*, the read succeeded, and the
task was declared COMPLETED. **Arion cannot rename a file, does not know it
cannot, and reports success.** This is the most serious behavioural finding
in the audit.

### J5 — "send me a summary by email" ✅ fails correctly
No template matches, no filename regex → planner raises "goal not
decomposable" → task FAILED. Honest failure. Contrast with J4.

**The pattern:** goals *outside* the vocabulary that contain no filename fail
loudly and correctly. Goals outside the vocabulary that happen to *mention a
filename* silently degrade into a read and report success. The failure mode
is not "can't do it" — it's "claims it did it."

---

## 3. Cognition loop audit

| Stage | State | Notes |
|---|---|---|
| **Perception / world state** | Partial | `WorldStateMonitor` tracks registered capabilities, env facts, staleness. Perception of the *external world* is limited to what the 7 actions return |
| **Planning** | Works, thin | Deterministic keyword/regex templates; model planner available but off by default |
| **Execution** | **Strong** | Fencing, leases, locks, per-step durable status, crash-resume — genuinely production-grade |
| **Observation** | **Weak link** | Verification policies are `non_empty`, `schema_keys`, `write_verified`, `append_verified`. All check *shape*, none check *semantics*. This is precisely why J4 passes |
| **Memory** | Works | Episodes, reflections, consolidation, retention/pruning, all durable |
| **Reflection → next plan** | **Genuinely closes** | `build_guidance_for_episode` → `PlanningContext.guidance` → `apply_guidance_to_steps` → resource/action substitution or explicit `SKIPPED`, with provenance. Consumed by *both* planners |

**Where the loop closes:** failure-driven re-targeting. A failed resource
becomes `avoid` guidance; the next plan substitutes a preferred resource, or
substitutes a different action on the same capability, or keeps the step and
marks it `SKIPPED` with provenance — never silently deletes it. It fails
closed if the registry cannot resolve a resource param. This is real learning
and it is well built.

**Where the loop terminates:** the loop can only learn *which resource to
avoid*. It cannot learn *that the plan was the wrong shape*, because
verification never establishes that the goal was achieved. In J4 there is no
failure to learn from — the system believes it succeeded. **A learning loop
whose error signal is shape-only cannot detect semantic failure.**

---

## 4. Capability gap analysis

**Missing action primitives.** delete/move/copy/mkdir; recursive search
(grep); structured file edit (patch/replace rather than whole-file write);
process/shell execution; HTTP methods beyond GET.

**Missing integrations.** No outbound human channel except webhook
notification (which only announces approvals). No email/chat. No inbound
trigger — Arion acts only when a human types `arion run`.

**Missing autonomy mechanisms.** No scheduled/recurring goals, no watches, no
daemon that advances goals on its own. `arion run` is one-shot and
foreground. Goal decomposition is single-level: no sub-goals, no goal that
spawns goals.

**Missing reliability mechanisms.** *Semantic* verification — the gap that
makes J4 possible. Post-conditions ("the file no longer exists at the old
path") rather than shape checks. Also: no self-check that the plan actually
addresses the goal.

---

## 5. Candidate M7 directions

### C1 — Outcome verification (semantic post-conditions)
- **User value: very high.** Fixes false success. Today an operator cannot
  trust COMPLETED, which undermines *everything* else the system reports.
- **Fit: excellent.** `VerificationPolicy` is already a pluggable seam with
  4 implementations and an "unknown policy" fail-closed branch. Adding
  policies is the seam working as designed.
- **Scope: small–medium.** New policies + planner emitting them + a
  "plan does not address goal" refusal.
- **Exercises:** verification seam, progress evaluator, reflection (real
  failures start flowing into guidance).
- **Risk:** low. Mostly additive; may convert some silent passes into
  loud failures — which is the point.

### C2 — Capability vocabulary expansion (delete/move/copy/mkdir/search/edit)
- **User value: very high.** Turns 7 actions into a usable toolkit; makes
  "rename" *possible* at all.
- **Fit: excellent.** `Capability` is a Protocol; registration, risk tiers,
  approval routing, locks, recovery all already generalise. Each new
  mutating action inherits approval + lock + recovery for free.
- **Scope: medium**, and highly parallel.
- **Exercises:** approval queue, mutation locks, recovery registry — and
  finally generates enough contention to justify the scheduler suite.
- **Risk: medium.** Every new mutation is new blast radius. Delete/move need
  recovery semantics as carefully specified as ADR-020.
- **Dependency:** significantly safer *after* C1.

### C3 — Autonomous execution daemon (scheduled/recurring goals)
- **User value: high** — the difference between a tool you invoke and an
  agent that works.
- **Fit: good.** Scheduler, leases, capacity, lifecycle all exist and are
  built for exactly this.
- **Scope: medium.**
- **Risk: high right now.** Unattended execution of a system that reports
  false success (J4) with a 7-verb vocabulary automates the wrong thing.
  **Strictly gated behind C1.**

### C4 — Model-backed planning by default
- **User value: high** — dissolves the keyword/regex brittleness.
- **Fit: excellent**, already built and validated.
- **Risk: high.** With shape-only verification, an LLM's plausible-looking
  wrong plan passes verification. C4 *multiplies* the J4 failure mode.
  **Also gated behind C1.**

---

## 6. Explicit non-recommendations

**NOT M7: multi-agent coordination.** Nothing is bottlenecked on a second
agent. One agent with 7 actions doesn't saturate the existing scheduler.
Adding agents multiplies a coordination substrate that is already ahead of
demand. Architecturally seductive, behaviourally worthless today.

**NOT M7: more orchestration machinery** (multi-worker webhook delivery,
richer capacity policy, Tier-2 notification events). These are M6.x. Building
more governance for 7 actions widens the wrong ratio.

**NOT M7: a richer observability/audit surface.** Audit is already strong
(closed event vocabulary, required/best-effort sinks, JSONL mirror, 78 CLI
verbs of introspection). More dashboards over an agent that can't act is
polish, not reach.

**NOT M7 on its own: model-backed planning (C4).** Genuinely valuable, and
*wrong to do first* — see §5.

**Caution: capability expansion (C2) before verification (C1).** The instinct
is "Arion can't rename a file, add rename." But J4's real defect isn't the
missing verb — it's that Arion *claimed to rename* and didn't. Add mutating
verbs to a system that can't tell success from failure and you get confident,
approved, destructive actions.

---

## 7. Recommendation

**Primary: C1 — Outcome verification.** Then **C2 — capability vocabulary**,
in that order, ideally as M7 (C1) and M7.1/M8 (C2).

The reasoning: Arion's sophistication currently fails to translate into
usefulness for two compounding reasons — it can barely act (7 actions), and
it cannot tell whether its actions achieved anything (shape-only
verification). Of those, the verification gap is *upstream*. It caps the
value of every other candidate: capability expansion becomes dangerous
without it, autonomy becomes reckless without it, and model planning becomes
unfalsifiable without it. It is also the cheapest of the four and it turns
the already-working guidance loop from "avoid broken resources" into "learn
from real outcomes."

**Alternatives if you disagree:** C2 first, if your priority is Arion doing
*more* rather than Arion being *trustworthy* — defensible, since C1 with 7
actions is verification of a very small world. Or C1+C2 as one narrow
milestone: add `filesystem.move`/`delete` *together with* the post-condition
policies that prove they worked, as a vertical slice.

**The one thing I'd argue against regardless:** shipping any new autonomy
(C3) or planner intelligence (C4) while J4 reproduces.

---

## 8. Known issues / follow-up candidates

- `tests/test_concurrency_model.py::test_crash_mid_flight_restart_resumes_without_duplicate`
  exhibits timing sensitivity under full-suite contention; reproduced against
  pristine baseline (10/10 isolated passes on both baseline and patched
  trees); **not an M6-A.1 regression.** M6.x backlog. No fix proposed here.
- `ARION_SANDBOX` is not read: `cli.main` hardcodes `sandbox_root` to the
  Arion repo root. Arion can currently only operate on its own checkout,
  which is a real limit on using it for outside work. Small, separate fix.
- M6-A.1 left `_MAX_BODY_BYTES` duplicated across `approval_api` and
  `webhook_api`. Cosmetic; deliberately out of scope.
