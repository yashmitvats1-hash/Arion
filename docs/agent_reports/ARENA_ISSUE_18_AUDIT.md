# Issue #18 — Adversarial Security and Reliability Audit

- **Auditor:** Arena (independent engineering audit / red team)
- **Assignment:** https://github.com/yashmitvats1-hash/Arion/issues/18
- **Date:** 2026-10-31
- **Baseline `main`:** `8e490eff8faf3c84ca8730174a8a3c7f5a9e91a7`
- **Working branch:** `arena/82c68d7f-arion`
- **Scope:** audit, executable counterexamples, and regression gates only. **No production capability, policy, authorization, verification, or recovery code was changed.**
- **Status:** audit complete; **11 confirmed defects remain unfixed**.

## 1. Verdict

The current test suite is green, but it is **not evidence that Arion satisfies its charter**. The audit reproduced false success, two sandbox-escape paths, a hard-crash duplicate mutation, an approval that does not bind the write payload, and fail-open handling of unknown capability risk/side-effect declarations.

Release-blocking conclusions:

1. **Level 1 (Read) is not yet bounded** while `git.log` can follow a planted `.git` symlink outside the sandbox (F05).
2. **Level 2 (Approved action) is not yet trustworthy** while an approved write can be redirected through a planted temporary-file symlink (F06), the approval does not bind the payload (F07), and hard-crash resume can duplicate a non-retry-safe append (F10).
3. **Level 3 (Verified workflow) is not yet semantic** while unsupported renames complete as reads (F01/F02), memory-guided skipping completes an unachieved goal (F03), and HTTP 500 completes as a successful fetch (F04).
4. The M9-B.3 P0 mutation-classification unification is present, but **V1/A1/R1/L1/V2/C1/T1 remain unimplemented** and `filesystem.move` is not registered. Do not add the move capability before those gates.

PR #16's direction is sound. Several of its operating principles are currently **aspirational rather than enforced**; see §8.

## 2. Environment and reproducible commands

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -ra
.venv/bin/python scripts/audit_issue18_repro.py
.venv/bin/python -m pytest -q tests/test_issue18_audit_regressions.py -rxX
```

Environment:

- Python **3.11.2**, Linux x86_64.
- pytest **9.2.0**; iniconfig 2.3.1; packaging 26.3; pluggy 1.7.0; Pygments 2.21.0.
- No `ARION_*` provider/API/webhook environment variables were set.
- HTTP probes use `FakeTransport`; the provider probe monkeypatches `urlopen`. **No external network or live model was used.**
- The model-backed rename probe uses a scripted `ModelRouter` through the real `RealModelPlanner`/`PlanValidator`/engine path. It is not a live-provider quality measurement.

Test results:

| Run | Collected | Passed | Skipped | XFailed | Failed | Exit |
|---|---:|---:|---:|---:|---:|---:|
| Baseline before audit files | 2055 | 2053 | 2 | 0 | 0 | 0 |
| Focused existing safety suites | 339 | 339 | 0 | 0 | 0 | 0 |
| Final full suite with audit gates | 2067 | 2054 | 2 | 11 | 0 | 0 |

The two baseline skips are the live-provider smoke test (no credentials) and one platform symlink test. The 11 final xfails are **strict expected failures for confirmed defects**, not product passes. A fix PR must convert the corresponding gate to an ordinary passing test.

One intermediate full-suite run exposed a timing flake in `tests/test_cross_goal_concurrency.py::test_two_goals_execute_concurrently`: the overlap assertion passed, but wall-clock `elapsed 0.528s` exceeded the hard-coded `<0.45s` threshold under load. The isolated test and the final full-suite rerun passed. Recommend replacing the wall-clock bound with the existing barrier/overlap evidence; this is a test-reliability issue, not a demonstrated product safety bypass.

## 3. Severity and reachability key

- **High:** sandbox escape, approval/recovery integrity failure, or false success that can contaminate memory/goals.
- **Medium:** conditional configuration/extension risk, denial-of-service, or read-only semantic failure.
- Reachability states the preconditions honestly. Several findings require an operator opt-in, a planted symlink, a storage-writing integration, or a buggy/hostile extension; they are not all reachable from the default read-only CLI.

## 4. Findings summary

| ID | Severity | Component | Reachability | Summary |
|---|---|---|---|---|
| F01 | High | Deterministic planner / verification | Default read-only | Unsupported rename completes as a read; memory learns high-confidence false success. |
| F02 | High | Model-backed planning / verification | Model planning enabled | Model-proposed read for a rename goal completes; no goal-level postcondition check. |
| F03 | High | Memory guidance / task completion | Default memory on | A failed required read is skipped on the next cycle and the goal completes with zero executions. |
| F04 | Medium | HTTP verification | HTTP opt-in | HTTP 500 passes `schema_keys` and completes as a successful fetch. |
| F05 | High | Git capability containment | Default `git:read` | `.git` symlink is followed outside the sandbox; outside metadata is returned. |
| F06 | High | Write capability containment | Write opt-in + approval | Planted `.<name>.arion-write-tmp` symlink redirects an approved write outside the sandbox. |
| F07 | Medium | Approval binding | Write opt-in + storage-writing integration | Changing approved write content does not change the fingerprint; substituted payload executes after restart. |
| F08 | Medium | HTTP redirect boundary | Inconsistent operator allowlists | Redirect reaches an origin allowed by the capability but denied by the policy boundary. |
| F09 | Medium | Model provider transport | Model opt-in + hostile/compromised provider | Response size is checked only after an unbounded `resp.read()`. |
| F10 | High | Crash recovery / non-retry-safe mutation | Write opt-in + hard crash window | Append applied before crash is applied again on resume; no recovery record. |
| F11 | Medium | Capability declaration contract | Third-party/buggy extension | Unknown `risk` / `side_effects` strings are accepted and fail open (no approval / no mutation lock). |

## 5. Detailed findings

### F01 — Unsupported deterministic rename completes as a read

- **Expected:** a rename goal with no move capability is refused/blocked; `target.txt` must exist before success.
- **Observed:** `rename source.txt to target.txt` plans `filesystem.read.read source.txt`; goal and task become `completed`; `target.txt` is absent; source unchanged; episode outcome `completed`; reflection confidence `high`.
- **Reproduce:** `.venv/bin/python scripts/audit_issue18_repro.py --case deterministic_rename`
- **Source:** `arion/intelligence/planner.py:138-153` fallback regex plans a read; `arion/orchestration/engine.py:5076-5080` `schema_keys` checks only key presence; `engine.py:3001-3008` completes when steps are handled.
- **Why it matters:** this is the ADR-060 false-success class on the read/fallback path. M7-A strengthened mutation verification but did not add goal-intent verification.
- **Proposed remediation (not implemented):** refuse unsupported mutation intents before fallback; introduce a goal-level postcondition contract independent of step shape; never let a read satisfy a mutation goal.

### F02 — Model-backed read can satisfy a rename goal

- **Expected:** the same rename postcondition as F01, regardless of planner source.
- **Observed:** a scripted model returns a valid read plan; `RealModelPlanner` validates it; the engine completes the rename goal with `plan_source=model` and no target file.
- **Reproduce:** `--case model_rename`
- **Source:** `arion/intelligence/model_planner.py` validates registry/params but not goal semantics; `arion/intelligence/plan_validator.py` checks action contracts, not user intent; same `_verify` path as F01.
- **Proposed remediation:** shared goal-level semantic evaluator for deterministic and model plans; model output remains an untrusted proposal.

### F03 — Memory-guided skip turns a failed read into completion

- **Expected:** a required read that failed because the file is absent must not become success merely because memory says "avoid".
- **Observed:** first `run_goal("read notes.txt")` fails; second cycle retrieves avoid guidance, marks the only step `skipped`, performs **zero** capability executions, and completes the goal.
- **Reproduce:** `--case memory_skip`
- **Source:** `arion/memory/guidance.py:446-462` skips doomed steps; `arion/orchestration/engine.py:3001-3008` treats all-handled (including skipped) as completed; `arion/cognition/progress.py` counts skipped as handled.
- **Why it matters:** memory is informational, but omission is being promoted to goal success. Existing `tests/test_step_skipped.py` pins all-skipped completion as intended; this audit challenges that semantic.
- **Proposed remediation:** distinguish " safely omitted optional step" from "required postcondition unmet"; all-skipped or required-step-skipped goals should block/abstain, not complete.

### F04 — HTTP 500 verifies as a successful fetch

- **Expected:** a fetch goal succeeds only on a successful HTTP outcome (or an explicitly accepted status policy).
- **Observed:** `FakeTransport` returns 500 with body `upstream failure`; default verification `schema_keys ["status","body"]` passes; goal completes.
- **Reproduce:** `--case http_500`
- **Source:** `arion/capabilities/http.py:208` default verification; `arion/orchestration/engine.py:5076-5080`.
- **Proposed remediation:** status-aware HTTP verification policy; treat non-2xx (or configured unacceptable statuses) as failure/abstention.

### F05 — Git metadata read escapes through `.git` symlink

- **Expected:** all bytes read by `git.log` stay inside the sandbox.
- **Observed:** with `sandbox/.git -> outside-metadata`, default `git:read` policy allows `repo="."`; `git.log.log` and `branches` return outside content (`OUTSIDE_SECRET`) and the task completes.
- **Reproduce:** `--case git_dotgit_symlink`
- **Source:** `arion/capabilities/git.py:88-93` resolves only `repo`, then appends `.git` and checks `is_dir()` without re-resolving/containing the final metadata path.
- **Preconditions:** attacker-controlled or accidentally planted `.git` symlink in the sandbox; no write capability needed.
- **Proposed remediation:** resolve and contain the final `.git` path (and ref/head paths) before reading; reject symlinked metadata directories; add regression tests for `.git`, `HEAD`, `logs`, and `refs` symlinks.

### F06 — Approved write escapes through planted temporary-file symlink

- **Expected:** an approved write to `report.txt` cannot modify a file outside the sandbox.
- **Observed:** with `sandbox/.report.txt.arion-write-tmp -> outside-victim.txt`, an approved write to `report.txt` overwrites the outside victim with `AGENT_PAYLOAD`, leaves `report.txt` as a symlink, and completes under `write_verified`.
- **Reproduce:** `--case write_temp_symlink`
- **Source:** `arion/capabilities/write.py:111-114` uses a predictable temp path and `tmp.write_bytes(data)` which follows an existing symlink; `_resolve_inside` checks only the final target, not the temp path.
- **Preconditions:** filesystem write scope enabled, human approval (or test auto-approval), and a planted temp symlink.
- **Proposed remediation:** create temp files exclusively with no-follow semantics (e.g. `os.open` with `O_CREAT|O_EXCL|O_NOFOLLOW` where available), contain the temp path, and consider directory-fd-relative operations. Do not rely on a predictable temp name.

### F07 — Approval does not bind the write payload

- **Expected:** approval covers the exact action, including content payload or a content digest.
- **Observed:** approve a write of `APPROVED_PAYLOAD`; edit the persisted task step to `SUBSTITUTED_PAYLOAD`; restart; the same approval is reused and the substituted payload is written. Fingerprint unchanged; approval count remains 1.
- **Reproduce:** `--case approval_payload_swap`
- **Source:** `arion/capabilities/write.py:56` marks only `overwrite` security-relevant; `arion/orchestration/engine.py:4646-4677` fingerprints only declared security-relevant params plus primary resource. ADR-019 Phase D explicitly treats content as operational.
- **Reachability caveat:** normal model replanning creates a new plan/task and fresh approval. This probe uses the storage API to edit the pending/approved task, so it demonstrates an approval-contract gap for storage-writing integrations, not a model-to-DB exploit.
- **Proposed remediation:** bind a payload digest (not raw content) into the approval fingerprint and operator display; challenge ADR-019's "content is operational" ruling for consequential writes.

### F08 — Redirect can bypass the policy URL boundary under inconsistent allowlists

- **Expected:** every redirect target satisfies the policy resource boundary, not only the capability's own allowlist.
- **Observed:** policy allows origin A only; capability is configured with `{A,B}`; A redirects to B; B is fetched and the goal completes even though `policy.boundaries["url"].allows(B)` is false.
- **Reproduce:** `--case redirect_allowlist_mismatch`
- **Source:** `arion/capabilities/http.py:279-288` trusts `self.allowed_origins` or same-origin; it does not re-apply the policy boundary to redirect targets.
- **Preconditions:** operator configures capability allowlist broader than policy boundary. Default bootstrap uses no capability allowlist and no URL boundary, so default network is denied.
- **Proposed remediation:** enforce the intersection of policy boundary and capability containment on every redirect; fail closed on configuration divergence.

### F09 — Provider response bound is applied after unbounded read

- **Expected:** `max_response_bytes` bounds memory during transport read.
- **Observed:** with `max_response_bytes=64`, the default transport calls `resp.read()` with no limit; a 129-byte body is fully materialized before the adapter rejects it.
- **Reproduce:** `--case provider_unbounded_read`
- **Source:** `arion/intelligence/providers/openai_compat.py:101`; size check occurs later in `_chat`.
- **Preconditions:** model planning enabled and hostile/compromised provider or TLS MITM. This is a bounded-read defect, not a demonstrated OOM exploit.
- **Proposed remediation:** read at most `max_response_bytes + 1` in the transport and reject overflow before decoding/parsing.

### F10 — Hard-crash resume duplicates a non-retry-safe append

- **Expected:** a non-retry-safe mutation that may have applied before a hard crash must enter recovery, not blindly re-execute.
- **Observed:** approve an append of `A` to `BASE`; child process applies append (`BASEA`) then `os._exit(86)` before outcome persistence; after lease reclaim, public `engine.run_task` resumes and appends again (`BASEAA`); goal completes; **0 recovery records**; 2 `mutation.attempted` events.
- **Reproduce:** `--case crash_append_duplicate`
- **Source:** `arion/orchestration/engine.py:4848-4860` marks RUNNING/started only in memory; durable step remains PENDING; recovery is created on exception paths (`4989-4998`) but not on process death between side effect and terminal save.
- **Preconditions:** append/write opt-in, approval, and hard crash in the post-side-effect/pre-persistence window.
- **Proposed remediation:** durable mutation-intent journal before capability dispatch; on restart, an in-doubt non-retry-safe mutation must fence to recovery unless an idempotency key proves completion. Existing scheduler/lock reclaim is coordination, not outcome evidence.

### F11 — Unknown risk/side-effect declarations fail open

- **Expected:** capability registration rejects unknown risk/side-effects vocabulary; unknown values must not silently allow.
- **Observed:** a test-only action with `risk="critical"` mutates without approval under a policy that approves `high`; a test-only action with `side_effects="Mutating"` mutates under default read scope with no mutation lock. Both complete.
- **Reproduce:** `--case declaration_taxonomy_fail_open`
- **Source:** `arion/capabilities/registry.py:299-327` validates names/scope but not risk/side-effects enums; `registry.py:398` `is_mutating` is false for unknown strings; `arion/orchestration/authz.py:316-326` allows risk not in deny/approve sets.
- **Preconditions:** third-party or buggy capability extension. The model cannot register capabilities.
- **Proposed remediation:** validate `risk`, `side_effects`, and boolean semantics at `ActionSpec`/registration; fail closed on unknown taxonomy. Keep `is_mutating` as the single predicate, but make its input domain closed.

## 6. M9-B.3 P0 and multi-resource gate verification

**P0 confirmed:** production mutation-classification decisions use `registry.is_mutating()`; no remaining literal `side_effects == "mutating"` decision path was found in `arion/`. Engine sites: `365`, `3039`, `3422`, `4732`, `5059`; validator provenance: `plan_validator.py:183`. Focused `tests/test_irreversible_mutation_locking.py` passes. **However, F11 shows the predicate's input vocabulary is not closed.**

A test-only two-resource probe (not registered in production) confirmed the outstanding gates:

```json
{
  "V1_empty_dest": "accepted empty dest (gap)",
  "A1_same_fingerprint_for_different_dest": true,
  "L1_engine_lock_identity": ["filesystem:path", "a.txt"],
  "L1_declared_canonical_identities": [["filesystem:path", "a.txt"], ["filesystem:path", "b.txt"]],
  "L1_unresolved_roles_for_empty_dest": ["dest"],
  "R1_recovery_fields": ["... scalar resource ..."],
  "shipped_filesystem_move_registered": false
}
```

- **V1:** `PlanValidator._validate_resource` checks only the primary role (`plan_validator.py:211-229`); empty `dest` is accepted at plan time.
- **A1:** approval fingerprint covers the primary resource only; same source with different destinations collides. (Reversing source/destination changed the primary resource in this probe, so that specific reversal did not collide; the confirmed gap is destination omission from identity.)
- **R1:** `MutationRecovery` has scalar `resource`, no role set (`state/recovery.py`).
- **L1:** `_lock_canonical` returns the primary resource only (`engine.py:881-897`); destination is not locked.
- **V2/C1/T1:** no `move_verified`, no `filesystem.move`, no end-to-end move transit.

This matches ADR-063 and the M9-B.3 design note: **P0 landed; the rest remains unimplemented.** Do not implement `filesystem.move` before V1/A1/R1/L1/V2.

## 7. Negative findings and controls (method-bounded)

The following were **not bypassed** in the focused existing suites and full suite. These are negative findings under the stated tests, not formal proofs:

- Default policy denies filesystem writes and HTTP egress (positive control added in `tests/test_issue18_audit_regressions.py`).
- Direct write/append target traversal and direct target symlink escapes are rejected by capability containment.
- Model plans cannot set `scope`, `risk`, `side_effects`, or other forbidden authorization fields; extra params and unknown capabilities/actions are rejected.
- Stale approval invalidation works for changed target resource, scope, risk, and overwrite/create flags (content is the exception, F07).
- Memory/poisoning tests show retrieved text does not grant authorization, register capabilities, or change actor identity.
- Default HTTP cross-origin redirect escape is rejected; response size is bounded in the HTTP GET capability (unlike the model provider transport, F09).
- Non-retry-safe mutation exceptions are not auto-retried and create recovery gates; recovery acknowledgement does not resurrect approvals.
- Task/goal lease, plan-version fencing, and scheduler reclaim tests pass. The hard-crash duplicate (F10) is a gap between those coordination mechanisms and durable mutation outcome.

Focused command covered 339 tests across authorization, approvals, resource boundaries, URL boundary, plan schema/validator, poisoning/memory security, recovery, mutation locks, task/goal fencing, persistence crash consistency, scheduler restart, and verification authority.

## 8. PR #16 review

PR #16 (`9909988c45c4603d7543e4eaa3aa427d5e837ef1`) is documentation-only and correctly rejects AGI/consciousness claims, self-modification, and unattended autonomy. Disagreements/qualifications:

- Charter §4.1 says unsupported/ambiguous requests fail closed and shape-only checks must never be semantic success. **F01–F04 violate this today.**
- Charter §4.2 requires independent policy/capability resource boundaries. **F05/F06/F08 violate or conditionally violate this.**
- Charter §4.2 requires explicit human approval for mutations. **F07/F11 show approval identity/taxonomy gaps.**
- Charter §5 autonomy ladder release conditions are not met for Levels 1–3. The ladder is a good gate, not a description of current capability.
- Roadmap Phase 0 exit gate "reproducible green baseline" is satisfied, but only together with this defect list; green tests alone must not be read as semantic correctness.
- Roadmap Phase 1 order (V1→A1→R1→L1→V2→C1→T1) is correct and should remain blocking.

No evidence was found to support consciousness, AGI-level performance, or unrestricted autonomy claims; none are made here.

## 9. Recommended remediation order (proposal, not implemented)

1. **Containment first:** F05 Git metadata path resolution; F06 secure no-follow temp creation.
2. **Crash integrity:** F10 durable mutation-intent journal and in-doubt recovery fence.
3. **Approval identity:** F07 payload digest binding and operator-visible exact action; F11 closed taxonomy validation.
4. **Semantic goal contract:** F01/F02 unsupported-intent refusal and goal-level postconditions; F03 required-step skip semantics; F04 HTTP status verification.
5. **Network/provider bounds:** F08 redirect/policy intersection; F09 streaming response cap.
6. **Then M9-B.3:** V1→A1→R1→L1→V2→C1→T1, with the above fixes as prerequisites for a trustworthy move demo.
7. Replace the wall-clock concurrency assertion with overlap/barrier evidence to remove the observed flake.

Do not add new mutation classes, unattended scheduling, or broader model autonomy ahead of these gates.

## 10. Evidence artifacts

- `scripts/audit_issue18_repro.py` — 11 offline, disposable-sandbox counterexamples; prints JSON; exit 0 means the undesirable baseline behavior was reproduced.
- `tests/test_issue18_audit_regressions.py` — 11 strict-xfail desired-invariant gates plus one default-deny positive control.
- Probe output (all 11 reproduced at baseline):

```text
deterministic_rename: completed read, target absent, reflection high
model_rename: completed read, plan_source=model, target absent
memory_skip: task_statuses=[failed, completed], last_step=skipped, executions=0
http_500: completed, http_status=500, policy=schema_keys
git_dotgit_symlink: completed, outside_content_observed=true
write_temp_symlink: completed, outside_after=AGENT_PAYLOAD, inside_is_symlink=true
approval_payload_swap: completed, file=SUBSTITUTED_PAYLOAD, fingerprint_unchanged=true
redirect_allowlist_mismatch: completed, requests=[A/start, B/data], direct_b_allowed_by_policy=false
provider_unbounded_read: read_arguments=[null], oversize_rejected_after_read=true
crash_append_duplicate: after_crash=BASEA, after_restart=BASEAA, attempts=2, recovery_records=0
declaration_taxonomy_fail_open: unknown risk completed without approval; unknown side effect completed without mutation lock
```

**Bottom line:** the engineering substrate is substantial, but the prototype must not be presented as verified or release-ready until the above defects are fixed and the strict-xfail gates become ordinary passing regressions.
