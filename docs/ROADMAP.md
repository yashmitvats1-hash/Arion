# Arion Roadmap — Trustworthy Agent Prototype

- **Date:** 2026-10-31
- **Current source of truth:** current `main`, the ADRs and verified test results
- **Planning note:** proposed sequence; re-baseline before implementation

## Current state (repository inspection)

Arion already has a substantial Python 3.11+ architecture: persistent SQLite state and memory, goal/task state machines, deterministic and OpenAI-compatible model-planning paths, capability registration, resource-aware authorization, human approval records, checkpointing and recovery, audit events, and scheduling.

The capability package currently contains filesystem read/write/append, structured filesystem search, Git metadata inspection, HTTP GET, and observations/registry support. The architecture is therefore more developed than its user-visible action set. The immediate risk is not a lack of orchestration machinery; it is ensuring the exposed capabilities can be trusted to complete exactly the requested goal.

The M9-B.2 completion note documents 2,040 passed / 2 skipped / 0 failed at that milestone's HEAD. The latest merged main commit includes the later M9-B.3 P0 single mutation-classification change. A new full-suite run on the exact current HEAD is a prerequisite for making a current baseline claim. The repository tree inspected on 2026-10-31 contains no GitHub Actions workflow under `.github/workflows`; evaluate adding CI after checking current repository settings and test runtime.

## Phase 0 — Re-baseline and audit

**Goal:** know what is actually shipped and working before changing it.

- Pin the current `main` SHA.
- Run the full suite from a clean environment; record Python version, dependency state, command and exact result.
- Run CLI smoke tests with model configuration unset and confirm deterministic fail-closed behaviour.
- Inspect the latest merge diff, outstanding branches/PRs and ADR-to-code consistency.
- Confirm the current capability/action catalogue, approval defaults and actual sandbox root.
- Have the agents independently audit architecture, safety, evaluation and adversarial cases.

**Exit gate:** reproducible green baseline, list of confirmed defects, and a reviewed subsystem/status map.

## Phase 1 — Finish the verified multi-resource mutation vertical slice

**Goal:** complete the planned filesystem move only after every relevant authorization, locking, recovery and verification invariant is enforceable.

Follow the current normative design and verify the sequence before each change:

1. **V1:** validate every declared resource role at plan time.
2. **A1:** make approval identity bind every declared role and preserve direction-sensitive intent.
3. **R1:** make mutation recovery identify every resource involved.
4. **L1:** acquire the full resource lock set safely and consistently.
5. **V2:** implement a two-resource outcome verification policy tied to declared source and destination.
6. **C1:** implement `filesystem.move` with strict path containment, explicit risk, no unsafe retry, and complete evidence.
7. **T1:** exercise the real plan → validate → authorize → approve → execute → observe → verify → recover path end to end.

The order is important. Do not implement a two-resource mutation before its approvals, recovery records, locks and verification cover both resources.

**Exit gate:** all M9-B.3 invariants are tested; no lock/approval/recovery mismatch; move succeeds only with independently verifiable evidence; failure and restart cases are covered; full suite passes.

## Phase 2 — Semantic task evaluation

**Goal:** ensure that COMPLETED means the user's goal was achieved.

- Build a fixed benchmark of 20–30 bounded tasks, with exact fixture and postcondition assertions.
- Include supported, unsupported, ambiguous and adversarial goals.
- Check for false success, accidental side effects, missing approvals, wrong-resource operations and memory contamination.
- Keep the evaluator separate from Arion's own model and verification claims.
- Add test cases for partial completion, task interruption, restart, stale approval and hostile tool output.

**Exit gate:** zero false-success cases in the release benchmark; correct refusal for unsupported tasks; transparent partial-failure reporting; reproducible results.

## Phase 3 — A compelling supervised demo

**Goal:** demonstrate useful autonomy without removing operator control.

- Use a disposable workspace with synthetic documents and folders.
- Support read-only discovery, bounded search, a transparent multi-step proposal, explicit approval for a move, execution, verification and durable audit.
- Interrupt the process during a task and demonstrate correct recovery.
- Show memory recording the result and influencing a later plan without granting permissions.
- Provide a short demo script, expected outputs and clean reset procedure.

**Exit gate:** a new operator can reproduce the demo from the README and verify the resulting files independently.

## Phase 4 — Stronger planning and learning

**Goal:** improve task generality only where benchmarks identify a real bottleneck.

- Keep the deterministic planner as a reference implementation.
- Run model-backed planning in opt-in mode against the same benchmark.
- Measure plan validity, semantic success, refusal quality, repair rate, latency and cost.
- Make the model produce structured plans that pass the same strict registry validation and authorization path.
- Explore hierarchical task decomposition, planning critique and memory-guided strategy selection behind explicit interfaces.
- Treat retrieved memory and external data as untrusted; preserve provenance and ensure they cannot alter authority.
- Require robust abstention and bounded re-planning. Do not allow an infinite retry loop.

**Exit gate:** a measured improvement over the deterministic baseline without a regression in safety, false success, or approval correctness.

## Phase 5 — Controlled integrations and opt-in autonomy

**Goal:** expand usefulness one capability at a time.

Potential later areas include a human-facing approval UI, tightly scoped external integrations, notifications, and scheduled tasks. Each requires a separate threat model, permission declaration, resource bounds, audit events, cancellation path, recovery strategy and tests.

Do not enable unattended recurring execution by default. Do not connect private accounts or credentials until there is an explicit need and a reviewed permission model.

**Exit gate:** per-integration safety review, operator-visible configuration, clear disable/stop path, and explicit owner opt-in.

## Deferred research track

Novel neural architectures, distributed training, synthetic environments, mechanistic interpretability research, and formal verification should be pursued as bounded experiments with falsifiable hypotheses. They are not prerequisites for the first useful prototype.

For any proposed research:
- State the specific limitation of the current system.
- Define a measurable hypothesis and baseline.
- Identify what observation would falsify the proposal.
- Build the smallest experiment.
- Do not promote the result into the product until it improves a benchmark and survives independent review.

## Next action

The immediate next action is Phase 0: obtain independent reports from Gemini, Arena, Claude and DeepSeek, and re-run the suite on exact current `main`. In parallel, Arena should review the already merged P0 change and the M9-B.3 design sequence without implementing new capabilities.
