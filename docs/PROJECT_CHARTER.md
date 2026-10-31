# Arion Project Charter

- **Date:** 2026-10-31
- **Status:** Proposed operating charter; first review requested from the project agents
- **Repository:** `yashmitvats1-hash/Arion`
- **Project lead:** ChatGPT (architecture, synthesis, milestone control during active sessions)
- **Human owner:** Repository owner; retains final authority over scope, risk, releases and deployments

## 1. Mission

Develop Arion into a useful, increasingly general autonomous-agent prototype: a system that can understand bounded user goals, plan multi-step work, select only registered capabilities, obtain the required authorization, execute in a constrained environment, verify real outcomes, recover from interruption, and improve future planning from recorded experience.

The project should demonstrate dependable agency, not merely produce convincing explanations. A successful demo must show the initial state, the proposed plan, authorization decisions, actual side effects, independently checkable postconditions, recovery behaviour, and an honest final report.

**Terminology:** Arion is an autonomous-agent prototype and a research stepping stone toward more general intelligence. The project must not describe it as human-level AGI, conscious, self-aware, or capable of unrestricted self-improvement unless those claims are independently established. The target is not achieved merely by adding more tools, model calls, code, or autonomy.

## 2. Product target

The first public-quality demonstration should be a local-first personal-computing agent that can complete a bounded, multi-step task in a disposable sandbox. It should:

1. Translate a natural-language goal into an explicit, inspectable plan.
2. Resolve every requested operation against a registered capability contract.
3. Validate parameters, resource declarations, verification policies and risk.
4. Refuse unsupported or ambiguous tasks rather than pretending they succeeded.
5. Request human approval before consequential mutations.
6. Execute bounded steps with durable checkpoints and restart recovery.
7. Verify the intended postcondition using evidence tied to the actual operation.
8. Preserve a concise, provenance-aware memory of outcomes without turning memories into permissions.
9. Explain what it did, what it could not do, what evidence supports completion, and what requires human attention.

A demo is complete only when a separate evaluator checks the resulting state. A successful tool call is not, by itself, proof that the user's goal was achieved.

## 3. Non-goals and deferred research

Until the bounded prototype is trustworthy, do not pursue:

- Claims of human-level AGI, consciousness, or subjective experience.
- An unrestricted autonomous internet agent, unattended computer operator, or unconstrained daemon.
- Self-modifying production code, self-replication, autonomous deployment, or modification of its own permission system.
- Arbitrary shell/process execution or a general-purpose code execution capability.
- Autonomous access to credentials, financial accounts, private communications, or high-impact external services.
- Massive distributed training infrastructure or custom accelerator hardware without a measured need and a funded, concrete experiment.
- Inventing a new neural architecture before a reproducible benchmark shows that the existing model-provider abstraction is the limiting factor.

Consciousness questions remain in BERRY and must not be treated as an engineering deliverable or an assumed property of Arion.

## 4. Operating principles

### 4.1 Authority and trust

- The model is an untrusted proposer, not an authority. It may suggest plans but cannot grant itself permissions, register capabilities, widen resource boundaries, approve operations, or alter verification rules.
- The live capability registry and policy layer are the authority for scopes, resource roles, risk, side effects, retries and verification.
- Memory, reflections, beliefs, retrieved documents and tool outputs are informational data. None of them grants permission.
- Authorization must be checked at the point of execution and rechecked after waits or changes that could stale a decision.
- Unknown, malformed, unsupported or ambiguous requests fail closed with a useful diagnostic.
- A successful task must have explicit evidence for its goal-level postconditions. Shape-only checks must never be represented as semantic success.

### 4.2 Execution boundaries

- Start with a disposable sandbox and least-privilege local access.
- Keep read-only capabilities separate from mutations.
- Require explicit human approval for mutations and any external action with meaningful consequences.
- Keep resource boundaries explicit, bounded and enforced independently by the policy layer and capability implementation.
- No arbitrary shell, process execution, hidden network access, secret collection, or credential persistence.
- External network access, when deliberately enabled, must be narrow, allowlisted, size/time bounded, logged and tested against redirects and hostile content.
- No unattended or recurring task execution until outcome verification, cancellation, resource ceilings, restart recovery and operator visibility have passed adversarial tests.
- Stop controls and operator visibility must be available before any increase in autonomy.

### 4.3 Engineering discipline

- Preserve the deterministic, model-independent path as the reference and regression baseline.
- Keep model-backed planning opt-in until end-to-end evaluation is strong.
- Make source changes in isolated branches and propose them through pull requests. No agent should push directly to `main`.
- Every code change needs focused tests, full-suite results, and a concise explanation of its risk and rollback path.
- Architectural decisions must be recorded in ADRs only after evidence and cross-review. Do not rewrite historical reports to make the project appear more successful.
- Separate facts observed in code/tests, inferences, design proposals and unresolved questions.
- Prefer a small, reproducible vertical slice over speculative architecture.

## 5. Autonomy ladder

| Level | Behaviour | Release condition |
|---|---|---|
| 0 — Explain | Analyse a goal and explain a plan; no side effects | Accurate task interpretation and clear limitations |
| 1 — Read | Bounded read-only inspection in an explicit sandbox | Capability and resource-boundary tests pass |
| 2 — Approved action | Propose a mutation and wait for an explicit human approval | Approval identity, mutation locks, evidence and recovery are tested |
| 3 — Verified workflow | Complete bounded multi-step goals with durable checkpoints | External postcondition benchmark and adversarial suite pass |
| 4 — Scheduled assistance | Perform narrowly scoped, opt-in recurring work | Separate operational review, cancellation tests, monitoring and explicit owner opt-in |

No level grants self-modification, self-replication or permission escalation. Level 4 is not the default operating mode.

## 6. Near-term engineering priority

Continue from the actual state of `main`, not from an outdated milestone assumption. The repository's M9-B.2 completion note records 2,040 passed, 2 skipped and zero failures at the M9-B.2 HEAD. The later M9-B.3 P0 mutation-classification change is present in the merged history and needs a fresh full-suite run before that baseline is treated as current.

The outstanding M9-B.3 multi-resource mutation design records this order:

**V1 → A1 → R1 → L1 → V2 → C1 → T1**

The responsible engineers must verify each item against the current source and ADR-063 before implementation. In broad terms, the sequence establishes plan-time validation of all resource roles, role-sensitive approval identity, multi-resource recovery records, safe multi-resource locking, outcome verification, the first two-resource filesystem move capability, and end-to-end transit tests.

Do not add new mutation classes, unattended scheduling, or broader model autonomy ahead of these safety and correctness gates.

## 7. Prototype evaluation

Maintain a small versioned benchmark of realistic tasks, including supported tasks, unsupported tasks, ambiguous requests, malformed model plans, boundary escapes, partial failures, restarts and poisoned tool output.

For every case, record:

- User goal and initial fixture state.
- Expected plan properties and required authorization.
- Actual actions and side effects.
- Independently computed expected and observed postconditions.
- Whether Arion completed, correctly refused, or recovered.
- False-success rate, unsafe-action rate, missed-approval rate, recovery correctness, latency and model/tool cost where applicable.

A task that is unsupported but correctly refused is better than a task that performs an unrelated action and reports success. A model-generated explanation is never the evaluator.

## 8. Definition of a convincing prototype

Arion earns a release candidate only after it can repeatedly complete a small suite of meaningful end-to-end tasks in a disposable environment, with:

- No known path to bypass authorization or the sandbox.
- No false success in the release benchmark.
- Correct approval, cancellation, recovery and restart semantics.
- Clear, inspectable plans and audit trails.
- Reproducible tests and a documented clean-install/demo procedure.
- A human operator who can understand, approve, interrupt and review every consequential action.

The project should earn broader autonomy through measured performance, not assume it from the start.
