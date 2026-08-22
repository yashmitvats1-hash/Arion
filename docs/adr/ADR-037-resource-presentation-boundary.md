# ADR-037 — Execution Resource vs Persistent Presentation Boundary

- **Status:** Approved and implemented (2026-08-21)
- **Scope:** Minimize non-execution copies of resource identifiers

## Context

Arion identifies an action's authoritative resource through
`ActionSpec.resource_kind` and `resource_param`. The exact parameter is required
for live policy evaluation, capability execution, durable retry/resume, stored
plan re-adoption, and mutation coordination/recovery.

The same exact string was also copied into audit, JSONL, approval display,
episodic memory, guidance, cognitive beliefs, and HTTP result metadata. Those
destinations do not execute the resource.

The Phase 29 baseline passed with **1,340 tests and 2 skips**.

## Demonstrated flow and risk

A successful HTTP request to a URL containing query and fragment secrets copied
them into task/checkpoint state, audit/JSONL, episodic memory, and cognitive
beliefs. A URL containing embedded userinfo was correctly denied by
`UrlBoundary`, but its username/password were still persisted in task state,
audit/JSONL, memory, and beliefs before/after denial. A medium-risk URL also
copied a query token into the approval resource, summary, task approval mirror,
and authorization fingerprint.

These are actual engine paths. Policy denial protects execution but does not
make every persistence destination safe.

## Ownership map

### Exact execution identifier — retain

- `PlanStep.params` in current task/checkpoint state;
- immutable goal-plan history used by re-adoption;
- transient `AuthorizationRequest` and `ResourcePolicy`;
- capability invocation;
- mutation lock/waiter canonical keys;
- mutation recovery authority.

### Persistent/display identifier — minimize

- audit and JSONL details;
- approval queue resource/summary and task approval mirror;
- HTTP result URL metadata;
- episodic-memory resource and denial metadata;
- guidance, strategy constraints, beliefs, and planning-context display;
- general CLI display derived from those records.

Authorization freshness needs exact-change detection, not the original value;
a stable fingerprint is sufficient.

## Decision

### 1. Resource presentation contract

Add a dependency-light `ResourcePresentation` with:

- `display`: bounded, one-line value safe for non-execution persistence;
- `fingerprint`: SHA-256 over resource kind plus exact identifier;
- `redacted`: whether display differs from the exact value.

URL presentation:

- removes username/password;
- retains normalized scheme/host/port/path;
- replaces all query content with a query-omitted marker;
- replaces fragments with a fragment-omitted marker;
- never stores query names or values;
- is bounded.

Filesystem/Git path displays retain their useful relative path, normalized to
one line and bounded. Unknown resource kinds receive bounded one-line display
plus fingerprint; no generic content/secret classifier is attempted.

### 2. Audit and HTTP result presentation

Authorization event details keep their existing `resource` key but write the
display value, add `resource_fingerprint` and `resource_redacted`, and replace
exact resource occurrences inside the reason. The authorization detail schema
version increments for new rows; legacy rows remain readable.

`plan.produced` events retain structured step metadata and parameter names, not
parameter values. A declared resource receives display/fingerprint metadata.
Stored task/goal plans retain exact params.

`http.get` returns display URL metadata plus fingerprint. The exact request URL
remains in step params for durable execution/resume.

### 3. Approval compatibility

New approval records store resource display and a fingerprint-based canonical
authorization fingerprint. Current task params remain exact and live
re-authorization still rebuilds from them.

Fingerprint matching accepts both:

- new fingerprint-based records;
- legacy records containing the exact resource.

Thus existing pending/approved tasks remain resumable without duplicate
requests or weaker stale-approval detection.

### 4. Memory/cognition safety

New episodes store resource display, kind, fingerprint, and redaction status.
Authorization denials copy the same safe metadata.

A redacted resource is informational evidence only. Guidance must not substitute
its display string into a future executable plan; exact correlation remains
possible through fingerprint without persisting the exact value. Filesystem/Git
paths, whose display remains exact, retain existing avoid/prefer behavior.

## Compatibility

- Existing task/checkpoint and stored-plan execution identifiers are unchanged.
- Existing audit events, approvals, episodes, guidance, and beliefs remain
  readable; no migration rewrites historical data.
- Existing exact-resource approval fingerprints are accepted during resume.
- Event/resource fields keep their existing names where practical; metadata is
  additive.
- Filesystem/Git user-facing behavior remains useful.
- HTTP direct result callers now receive a safe URL display and fingerprint,
  while request execution still uses the exact input.

## Test strategy

Tests prove:

1. URL presentation removes userinfo, query content, and fragment while a
   fingerprint distinguishes different exact identifiers;
2. exact URL remains in task/checkpoint execution state and restart uses it;
3. new audit/JSONL and HTTP result metadata contain no secret identifier parts;
4. approval display is safe, fingerprint change still invalidates approval,
   and a legacy exact fingerprint still resumes;
5. episodes and beliefs contain only safe presentation;
6. redacted memory resources never become executable guidance substitutions;
7. filesystem/Git identifiers remain readable and behavior-compatible;
8. oversized display identifiers are bounded;
9. existing persistence and full regression suites remain green.

## Explicit deferrals

- Vaults, credential handles, secret-reference resolution, and encrypted task
  parameters.
- Rewriting historical rows that already contain exact identifiers.
- Removing exact identifiers from task/checkpoint/goal-plan state.
- Replacing exact mutation lock/recovery authority keys.
- Generic DLP, PII classification, entropy scanning, and path-component secret
  detection.
- Per-resource-kind policy plugins beyond URL and bounded generic/path display.

## Verification

- Before implementation: **1,340 passed, 2 skipped**.
- ADR-037 resource-presentation tests: **7 passed**.
- Focused authorization, approval, legacy fingerprint, HTTP, memory/cognition,
  plan persistence, recovery, and lock regressions: **232 passed**.
- Complete suite after implementation: **1,347 passed, 2 skipped**.
- Reproduced successful-query and denied-userinfo scenarios after the change:
  exact identifiers remained in task/checkpoint execution state and exact HTTP
  transport calls, while query/fragment/userinfo values were absent from new
  audit, JSONL, memory, cognition, approval, and HTTP-result presentation.
