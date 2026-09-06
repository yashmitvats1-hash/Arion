# ADR-060 — M7: Verified Outcome Execution

- **Status:** M7-A implemented (uncommitted). **M7-B deliberately not
  implemented — blocked by D8; see §10.**
- **Date:** 2026-09-05
- **Baseline:** `6d8e477` (M6-A.1)
- **Supersedes:** none
- **Amends:** none. ADR-010 (execution semantics), ADR-019, ADR-020 and
  ADR-021 are **exercised**, not modified.
- **Related:** ADR-006 (capability/permission model), ADR-009 (resource-aware
  authorization), ADR-010 (execution semantics), ADR-011 (structured
  intelligence boundary), ADR-013 (memory→learning), ADR-018 (approval queue
  and fingerprinting), ADR-019 (write capability), ADR-020 (append and
  mutation recovery), ADR-021 (advisory mutation locks), ADR-035 (capability
  observation contract)

Labels: **[FACT]** = verified property of this repository (by inspection or
by execution), **[DECISION]** = the choice being made, **[RATIONALE]** = why,
**[IMPL]** = implementation detail following from a decision.

---

## 1. Context

### 1.1 The observed defect

**[FACT]** Executed against this repository at baseline:

```
$ arion run "rename README.md to ARCHIVE.md"
task task_1d7546114ed7: COMPLETED
  [ok] step 0: read file (filesystem.read/read)

$ ls ARCHIVE.md → No such file or directory      (README.md untouched)
```

`DeterministicPlanner`'s fallback regex `(\S+\.\w+)` extracted `README.md`,
planned a **read**, the read succeeded, and `schema_keys: ["content"]` passed
because content was non-empty. Arion has no `move` action; it did not report
that. It reported success.

### 1.2 Why this is worse than a missing capability

**[FACT]** The false success propagates into the learning substrate:

```
episode  ep_d494…  outcome=completed  failures=0  tags=['filesystem.read','outcome:completed']
reflect  refl_6936…  conf=HIGH
         lesson="Goal 'rename README.md to ARCHIVE.md' is achievable
                 with the current capability"
```

Memory now holds a **high-confidence false belief** that Arion can rename
files, and will carry it into future planning through
`PlanningContext.guidance`.

**[FACT]** The existing failure→learning path is well built: verification
failure → `verification.failed` → `step.error` → episode `failures[]` →
`DeterministicReflector` → `build_guidance_for_episode` → `category="avoid"`
→ `apply_guidance_to_steps`. The path is not broken. **It is never entered,**
because nothing failed.

**[RATIONALE]** Capability absence is a limitation that can be expanded
systematically. False success corrupts every layer above execution. A
learning loop whose error signal is shape-only cannot detect semantic
failure.

### 1.3 Root cause

**[FACT]** All four verification policies check **shape, not outcome**:
`non_empty` (result truthy), `schema_keys` (keys present), `write_verified`
and `append_verified` (byte arithmetic against the capability's own report).

**[FACT]** `engine.py:4865` is the **sole** authority promoting a step to
SUCCEEDED:

```python
if self._verify(task, step):
    step.status = StepStatus.SUCCEEDED
```

Every other `StepStatus.FAILED` assignment (~35 sites) is a failure path.
Goal-level completion (`DeterministicProgressEvaluator`: "all plan steps
succeeded → complete") **inherits** step truth without re-deriving it.

**[RATIONALE]** There is exactly one chokepoint to strengthen. It is asking
too weak a question, not asking it in too many places.

---

## 2. Decisions

### D1 — `_verify()` remains the sole step-success authority

**[DECISION]** M7 adds no second path to SUCCEEDED. The engine's single
promotion site is unchanged in location and structure.

### D2 — No generic postcondition DSL

**[DECISION]** M7 introduces **one new named policy**, `move_verified`,
implemented as a hardcoded deterministic check beside the existing four.
`VerificationPolicy(policy: str, args: dict)` is **unchanged**.

**[RATIONALE]** `write_verified`/`append_verified` already establish the
pattern: a policy name is an opaque identifier for Python logic. A condition
language would need `AND`/`NOT`/parameter-reference syntax to express what 10
lines of Python express directly, and would arrive with its own validation,
serialization, security and failure semantics before we know what real
postconditions look like. **[FACT]** Nothing new is serialized: the policy
name already round-trips through `to_dict`/`from_dict`, SQLite and
checkpoints. Generalization must be earned by a second and third mutation.

### D3 — Evidence-based verification, never independent observation

**[DECISION]** Verification reads **only** `step.verification.policy`,
`step.verification.args`, `step.params` and `step.result`. It performs no
I/O, calls no capability, and re-reads no world state.

**[RATIONALE]** **[FACT]** This is an existing load-bearing property —
`write_verified`'s comment states it: *"confirm the intended postcondition
WITHOUT another mutation."* The tempting implementation of "did the rename
happen?" is to re-stat the filesystem. That would place unauthorized,
unfingerprinted, unapproved I/O inside the verification path, bypassing the
permission seam, approval queue and lock model — all of which govern
*capability* execution, not verification. It would also introduce TOCTOU and
make verification itself a failure source.

**[IMPL]** Instead the capability returns enough evidence, gathered inside
its own authorized execution, to prove its postcondition. **[FACT]**
Observations are already frozen and detached by `normalize_observation`
(ADR-035).

### D4 — Mutating actions take their verification policy from the registry

**[DECISION]** For an action whose `side_effects` is mutating, the executable
step's verification policy comes from `ActionSpec.default_verification`, not
from the plan proposer. A model-proposed weaker policy is **deterministically
upgraded**, not rejected.

**[DECISION] The engine is the authoritative normalization point.**
Normalization happens in `_execute_step`, immediately after the ActionSpec is
resolved and **before** authorization, execution and `_verify`.
`PlanValidator` performs the same normalization as a **secondary**, earlier
pass for model plans only.

**[RATIONALE]** **[FACT]** `PlanValidator` is instantiated at exactly one
site — `model_planner.py:129`. `engine.py` imports only `topo_sort_steps`
from that module. Therefore **`DeterministicPlanner` never traverses
`PlanValidator`**, and neither does the stored-plan rehydration path
(`engine.py:2742`). Enforcing normalization solely in the validator would
protect model-generated plans while leaving unprotected both the default
planner — which produced the observed §1.1 defect — and every replayed
stored plan. **[FACT]** `_execute_step` (`engine.py:4318`) resolves the spec
on the single path every plan traverses regardless of origin (deterministic,
model, or stored), and precedes verification at `engine.py:4865`. The
verifier therefore never has to infer which policy was intended.

**[RATIONALE]** **[FACT]** `PlanValidator.validate` currently passes
`verification=s.verification` straight through from the model while already
taking `scope` from the registry with the comment *"registry authority, not
the model."* Verification of a mutation is the same class of invariant: the
model does not control it and should not be responsible for it. Upgrade
rather than rejection follows the existing `scope` precedent and avoids
failing plans that are otherwise valid.

**[DECISION]** The normalization is recorded in **existing** provenance:
`PlanStep.guidance` (a `list[dict]`, already persisted via `to_dict`, already
used by `apply_guidance_to_steps` for exactly this "why does the executed
plan differ from the proposed plan?" purpose). **No new provenance subsystem
is created.**

**[IMPL]** Entry shape mirrors existing guidance provenance, e.g.
`{"kind": "verification_normalized", "requested": "non_empty",
"applied": "move_verified", "authority": "registry"}`.

### D5 — Fail closed on missing or unknown mutation verification

**[FACT]** Compatibility investigation (§3) establishes that **no
Arion-written plan omits `verification`**.

**[DECISION]** Four cases are distinguished explicitly. They arise on two
different code paths and must not be collapsed: **missing** verification is a
*rehydration* condition (`PlanStep.from_dict`'s
`v.get("policy", "non_empty")`), whereas an **unknown policy** is an
*execution* condition (`_verify`'s `else` branch).

| Case | Mutating action | Read-only action |
|---|---|---|
| **Explicit known policy** | honoured as-is | honoured as-is |
| **Registry default available** | applied (D4 normalization) | applied |
| **Missing verification** | **fail closed** | historical `non_empty` fallback |
| **Unknown policy, no registry default** | **fail closed** | fails closed in `_verify` (existing) |

**[DECISION]** For a mutating action: an explicit *known* policy is
acceptable; otherwise the registry default must supply one. If **neither**
exists the step is refused with a **precise diagnostic** naming the
capability, the action and the missing declaration — never executed as though
shape verification sufficed.

**[RATIONALE]** For a mutating step the `"non_empty"` rehydration default is a
silent fail-**open** route into precisely the §1.1 defect. Conversely, a
blanket "mutations must carry a registry default" rule would render a custom
mutating capability that declares no `default_verification` permanently
unexecutable even when its plan carries a perfectly valid explicit policy.
Honouring an explicit known policy avoids that while still refusing to guess.
**[FACT]** Both existing mutating actions already declare defaults
(`filesystem.write` → `write_verified`, `filesystem.append` →
`append_verified`), so no current capability is affected. **[FACT]**
`_execute_step` already fails closed when `spec is None`.

**[DECISION]** Read-only steps retain the historical `non_empty` default;
M7 fixes the mutation truth boundary and does not invalidate the existing
verification model.

### D6 — Evidence must bind to the declared parameters

**[DECISION]** A postcondition passes only if the evidence refers to the
resources the plan actually declared. A capability cannot satisfy
`move(A → B)` by reporting a successful move of `C → D`.

### D7 — `filesystem.move` is the first proof-producing mutation

**[DECISION]** M7-B adds `filesystem.move` with `risk="high"`,
`side_effects="mutating"`, `retry_safe=False`, `reversible=True`.

**[IMPL]** Observation (following the `write`/`append` precedent
`{"written": True, "size": N}` / `{"appended": True, "prior_size": P, ...}`):

```python
{"moved": True, "source": <rel>, "dest": <rel>,
 "canonical_source": ..., "canonical_dest": ...,
 "source_exists": False, "dest_exists": True,
 "size": N, "prior_size": N}
```

`move_verified` passes only if **all** hold:

1. `moved is True`
2. `source_exists is False` **and** `dest_exists is True`
3. `size == prior_size` (nothing lost in transit)
4. `source` and `dest` match `step.params` (D6)

`{"moved": True}` alone **fails** — missing keys yield `ok = False`.

### D8 — Two-resource authorization and locking

**[FACT]** `move` is Arion's first action targeting **two** resources. Every
existing mutating action has a single `resource_param`.

**[DECISION]** `resource_param="source"`, and `dest` is declared in
`security_relevant_params` so it enters the ADR-018 canonical authorization
fingerprint.

**[RATIONALE]** Without this, an approval to move `A → B` could be replayed
as `A → C`. **[FACT]** ADR-018 fingerprints the resource param plus declared
security-relevant params only; operational params are excluded.

**[DECISION]** Both resources are locked (ADR-021), acquired in **canonical
sorted order**, so two concurrent opposing moves cannot deadlock.

**[DECISION]** If the existing single-resource mechanisms cannot express two
resources without modification, that is a **finding to report, not to work
around silently**.

### D9 — Verified-outcome provenance reaches memory

**[DECISION]** Verification outcome is distinguished internally as
`VERIFIED_SUCCESS` / `VERIFIED_FAILURE` / `UNVERIFIABLE` (evidence absent or
no applicable policy) and recorded in the episode's existing `verification`
dict (**[FACT]** today `{"passed": [...], "failed": [...]}` — a free field to
extend).

**[DECISION]** These are **internal provenance, not new public
`StepStatus` values.**

**[RATIONALE]** **[FACT]** `StepStatus` is fenced by ~35 engine sites plus
the lineage and terminal-transition ADRs (ADR-040, ADR-054, ADR-056). New
enum members carry a large blast radius for no user-visible gain. **[FACT]**
`_verify` already emits `verification.passed|failed` with a `detail` dict;
extending that detail costs nothing.

**[DECISION]** `UNVERIFIABLE` must be representable so that
*"capability returned successfully + no applicable verification = success"*
is detectable rather than silent.

### D10 — False-success regression is explicitly prevented

**[DECISION]** A regression test asserts the §1.1 case is dead: the goal
`rename README.md to ARCHIVE.md` must never again yield COMPLETED without the
rename having occurred.

**[DECISION]** A **lying-capability test** is a centrepiece: a stub reporting
`moved: True` with contradictory evidence (`source_exists: True`,
`dest_exists: False`) must FAIL verification, and memory must **not** record
`outcome="completed"` nor learn "goal achievable". This validates the
epistemic boundary rather than the happy path.

### D11 — Narrow, deterministic plan refusal

**[DECISION]** `PlanValidator` gains a **narrow, deterministic** refusal: when
a goal requests a mutation for which the plan contains no mutating action, the
plan is rejected — yielding *"I cannot satisfy this goal with my available
capabilities"* instead of *"I read something, therefore your goal succeeded."*

**[DECISION]** No semantic goal understanding is attempted. Matching is
verb→action-availability only.

---

## 3. Persistence compatibility investigation (D5)

The question: can a legitimately-stored plan omit `verification`, such that
tightening the default would break historical data?

**[FACT] `PlanStep.to_dict` unconditionally emits `verification`.** It is in
the base dict, not behind a conditional (unlike `depends_on`, `guidance`,
`skipped_reason`, which *are* conditional).

**[FACT] Every persistence path funnels through `to_dict`.** The sole writer
of goal plans is `engine.py:4000`:
`record_plan_version(..., [step.to_dict() for step in steps], ...)`.

**[FACT] Empirically verified** by executing real goals and reading SQLite
directly. All three persistence sites carry explicit verification on every
step:

```
goal_plans.plan_summary   filesystem.read/list  {'policy': 'non_empty', 'args': {}}
                          filesystem.read/read  {'policy': 'schema_keys', 'args': {'keys': ['content']}}
                          git.log/log           {'policy': 'schema_keys', 'args': {'keys': ['commits']}}
                          git.log/branches      {'policy': 'schema_keys', 'args': {'keys': ['branches']}}
tasks.snapshot            filesystem.read       {'policy': ...}   (all steps)
checkpoints.snapshot      filesystem.read       {'policy': ...}   (all steps)
```

**[FACT]** The four `PlanStep.from_dict` call sites are: `models.py:210`
(Task rehydration), `engine.py:2742` (stored-plan execution),
`engine.py:2068` (approval candidate definition), and `models.py:137` itself.
None constructs steps from a source outside Arion's own `to_dict` output.

**Conclusion.** The `"non_empty"` default is **defensive, not load-bearing**.
No historical plan relies on it. Tightening it *conditionally for mutating
actions* closes a fail-open route with **no migration and no compatibility
break**; read-only plans keep the historical default untouched.

**Residual risk (accepted, documented).** A hand-edited or externally
generated plan row could omit `verification`. Under D5 such a row fails
closed if mutating — the correct outcome.

---

## 4. Invariants

1. A step is SUCCEEDED only if `_verify` returned true; `engine.py:4865`
   remains the sole authority.
2. Successful completion requires **positive evidence**, never merely the
   absence of failure.
3. Verification performs no I/O and calls no capability.
4. Evidence is produced by the capability inside its authorized execution.
5. A mutating action's verification policy comes from the registry, never
   from a model-proposed plan.
6. A mutating step with absent or unknown verification fails closed.
7. Evidence binds to the plan's declared parameters.
8. Unknown policy names fail closed in every engine version.
9. Verification outcome is recorded with provenance distinguishing
   verified-success / verified-failure / unverifiable.
10. **A task cannot be recorded as successfully completed if any mutating
    step lacks a verified-success outcome.** (Read-only steps may legitimately
    use `schema_keys`/`non_empty`; M7 fixes the *mutation* truth boundary and
    does not invalidate the existing verification model.)
11. Multi-resource mutations fingerprint every resource for authorization.
12. Multi-resource locks are acquired in canonical order.

---

## 5. Non-goals

- **No generic postcondition DSL** (D2).
- **No `delete`.** Irreversible, and its postcondition ("the file is gone") is
  indistinguishable from "it was never there" without a pre-condition
  snapshot — a materially harder design deserving its own ADR.
- **No multi-agent work.**
- **No generalized semantic goal evaluator** (D11 is deliberately narrow).
- **No redesign of `api_authz`.**
- **No broad verification rewrite** — the four existing policies are
  untouched.
- **No new public `StepStatus` values** (D9).
- **Planning-stage failures entering memory** — a real learning gap, but a
  different epistemic problem (*could not formulate a plan* vs *attempted
  action failed*). Deferred to M7.x unless essentially free.

---

## 6. Behavioural transition

> Some tasks that previously reached `COMPLETED` because an action returned
> successfully may now correctly become `FAILED` or be recorded as
> `UNVERIFIABLE`.

**This is not a regression. It is Arion becoming more truthful.**

Consequences to expect:

- Tasks that "worked" begin failing — correctly.
- **[FACT]** For mutating, non-retry-safe steps, verification failure already
  triggers `mutation.requires_recovery` and a durable recovery record
  (`engine.py:4913`, ADR-020 / ADR-021 Phase D). `move` inherits this
  automatically, so expect new `recovery.required` records.
- Real failures begin flowing into the guidance loop, which will start
  producing `avoid` guidance for genuinely unachievable goals.

**Honest limitation.** Verification is only as strong as the evidence
contract. A capability that fabricates internally consistent evidence defeats
it. D6/D7/D10 raise the cost of fabrication (multi-key, cross-checked,
parameter-bound evidence); they do not make it impossible. This is why the
evidence contract belongs to the capability author and is reviewed as
security-relevant code.

---

## 7. Minimum change set

**Zero schema migrations. No new tables, columns or serialized shapes.**

| File | Change |
|---|---|
| `arion/capabilities/move.py` | **new** (M7-B) — `FilesystemMoveCapability` (~120 LOC) |
| `arion/orchestration/engine.py` | **authoritative** verification normalization in `_execute_step` (D4); verified-outcome provenance; `move_verified` branch in `_verify` (M7-B) |
| `arion/intelligence/plan_validator.py` | **secondary** registry-authoritative normalization for model plans + D11 refusal |
| `arion/capabilities/registry.py` | helper resolving the authoritative policy for an action (D4/D5) |
| `arion/state/models.py` | mutating + absent verification → fail closed (D5) |
| `arion/intelligence/planner.py` | rename/move goal template |
| `arion/bootstrap.py` | register the capability |
| `arion/memory/lifecycle.py` | record verified-outcome provenance in the episode `verification` dict |
| `tests/test_move_capability.py` | **new** — success, lying capability, false-success regression |

**Sequencing.** M7-A (D1–D6, D9, D11) then M7-B (D7, D8, D10). M7-A is
independently testable against the existing four policies before any new
mutation exists.

---

## 8. Rejected alternatives

- **R1 — Generic `PostCondition` abstraction.** Rejected (D2): a mini-language
  before we have experience with two real postconditions.
- **R2 — Verification re-reads the world.** Rejected (D3): unauthorized I/O
  inside the verification path, TOCTOU, bypasses permission/approval/lock
  seams.
- **R3 — Reject model plans with weak mutation verification.** Rejected (D4)
  in favour of deterministic upgrade, matching the `scope` precedent.
- **R4 — New `StepStatus` members.** Rejected (D9): large fenced blast radius,
  no user-visible gain.
- **R5 — `copy` as the first mutation.** Rejected: avoids the two-resource
  authorization problem, which is part of M7's value, and would not kill the
  observed defect.
- **R6 — Change the `from_dict` default globally.** Rejected (D5/§3) in favour
  of a conditional tightening scoped to mutating actions.

---

## 9. Known issues / follow-up candidates (not M7)

- `tests/test_concurrency_model.py::test_crash_mid_flight_restart_resumes_without_duplicate`
  — timing sensitivity under full-suite contention; reproduced against
  pristine baseline; not an M6-A.1 regression.
- **`ARION_SANDBOX` is ignored**; `cli.main` hardcodes `sandbox_root` to the
  Arion repo root, so Arion can only operate on its own checkout. Directly
  limits real operator use. Small, separate fix.
- Planning-stage failures produce no episode (§5).
- `_MAX_BODY_BYTES` duplicated across `approval_api` and `webhook_api`.

---

## 10. M7 outcome — M7-B blocked by D8

**M7 outcome:** Verification authority and the mutation truth boundary are
established (M7-A). A multi-resource mutation was **deliberately not
implemented** because the existing security, approval and locking
architecture is structurally single-resource.

This is the D8 escape hatch working as designed, not a failed milestone.

### 10.1 What shipped (M7-A)

D1–D6, D9 and the registry/engine normalization. Verified by execution:
1,890 tests, 0 failures, 2 skips. The default runtime is behaviourally
unchanged — both shipped mutations already declare correct defaults, so
`verification.normalized` never fires on the default CLI path.

### 10.2 What was withheld (M7-B) and why

**[FACT]** Single-resource-ness is a **cross-cutting invariant**, encoded in
dataclass *fields*, not in a single check:

```
ActionSpec.resource_param: str | None
  -> AuthorizationRequest.resource: str | None
    -> ResourcePolicy._resource_allowed  (reads exactly one field)
      -> ApprovalRequest.resource: str | None
        -> _lock_canonical / _acquire_mutation_lock  (one lock)
          -> guidance resolver + episode resources[]
```

**[FACT] Boundary gap**, executed against a real `ResourcePolicy`:

```
source=README.md  dest=ARCHIVE.md        -> ALLOW
source=README.md  dest=../../etc/passwd  -> ALLOW   (never checked)
source=README.md  dest=/etc/passwd       -> ALLOW   (absolute, allowed)
source=../../etc/passwd  dest=ok.md      -> DENY    (source IS checked)
```

The policy engine would bless an out-of-boundary destination, inverting
ADR-009: the capability's own sandbox check would become the *primary*
control rather than defence-in-depth.

**[FACT] Approval gap.** `ApprovalRequest` carries a singular `resource`;
`params_keys` holds **names only, never values**. A human approving a move
would see a bounded `source`, the *name* `dest`, and no destination value —
they could not see where the file is going. That defeats human-in-the-loop
for the one action whose danger is entirely in its destination.

**[FACT] Locking gap.** `move A→B` would lock only `A`, leaving a lost-update
window on `B` against a concurrent `write B`.

**[FACT] Fingerprinting is the one clean seam.** Declaring
`security_relevant_params=["dest"]` already yields distinct fingerprints for
A→B and A→C, so ADR-018 needs no amendment.

### 10.3 Deferred to ADR-061 / M8

Multi-resource authorization and locking, as a separate milestone. Governing
principle:

> **A multi-resource action is authorized, approved, fingerprinted and locked
> as a complete resource set; partial treatment of the set is not an
> acceptable compatibility shortcut.**

**Correction to an earlier assumption recorded in this ADR (R5):** `copy` is
**not** a viable smaller first mutation. It also has a destination and hits
the identical boundary/approval/locking gap. A genuinely single-resource
mutating capability would be required instead.
