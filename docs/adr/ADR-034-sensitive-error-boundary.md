# ADR-034 — Sensitive Error and External Diagnostic Boundary

- **Status:** Approved and implemented (2026-08-19)
- **Scope:** Provider/model failures and mixed-trust capability diagnostics

## Context

ADR-033 established a typed, normalized event-detail boundary. Normalization
makes payloads serializable and snapshots mutable values, but deliberately does
not decide whether a string is safe to persist.

The Phase 26 audit found one concrete trust-boundary violation. The
OpenAI-compatible adapter included up to 300 characters of a non-success HTTP
response body in a typed exception. That exception then crossed several durable
boundaries:

```text
provider response body / transport exception
  -> Provider*Error
  -> model.response.received
  -> plan.validation.failed
  -> ArionEngine Task.error + task snapshot
  -> error + task.failed audit events
  -> failed-memory episode
  -> optional model-reflection context
  -> SQLite / JSONL / CLI
```

A temporary end-to-end reproduction used a provider HTTP 500 body that echoed
an Authorization bearer value and a private goal. Both strings were present in
all four audit event kinds, the durable task error, and the memory episode.
This is an actual data path, not a theoretical logging concern.

The verified pre-change suite passed with **1,318 tests and 2 skips**.

## Trust-boundary map

### External/untrusted diagnostic text

- provider HTTP error bodies;
- provider transport exception messages (an injected transport can include
  request headers/body, including a prompt or Authorization header);
- model-produced field names/values repeated by plan-schema validation;
- model-reflector failures and validation messages derived from model output;
- HTTP capability transport exceptions.

### Trusted or mixed-trust diagnostic text

- SQLite/scheduler coordination failures are system-generated and generally
  bounded;
- filesystem/Git capability messages are system templates but include
  caller-controlled resource names and OS exception text;
- authorization failures intentionally contain exact audited resource
  identifiers and generated policy reasons;
- sink failures are internal delivery errors, but a custom sink controls its
  exception text.

### Existing bounds

Bounds are inconsistent: provider exception bodies are 300 characters,
provider metadata 200, memory failure summaries 500, scheduler work errors 500,
scheduler telemetry reasons 200, sink failures 300, while task errors and
several audit event errors are unbounded. Truncation alone does not remove a
credential appearing near the beginning of a string.

The ADR-028 scheduler sanitizer is a useful precedent (allowlisted fields plus
bounds), but it is coupled to the scheduler schema and state transaction path.
It is not a reusable external-error policy.

## Decision

### 1. Central error summary contract

Add a dependency-light observability utility with:

- `ErrorSource.TRUSTED`, `ErrorSource.MIXED`, and `ErrorSource.EXTERNAL`;
- `ErrorSummary` carrying bounded `message`, `error_type`, `category`, and
  `source`, and adapting to an ordinary event-detail dictionary;
- `sanitize_error_text()` for trusted/mixed messages: one-line normalization,
  exact-secret replacement, conservative bearer/API-key/token/password pattern
  redaction, and an explicit maximum length;
- `summarize_error()` for exceptions.

Fully external provider summaries never retain arbitrary source text. They
preserve the typed error category, exception type, and an HTTP status code when
one can be safely extracted. Category-specific messages retain operational
meaning such as "provider authentication failed" without retaining a body,
prompt, completion fragment, or header.

Arion-generated plan/schema/capability validation templates are mixed-trust:
the templates are useful, while identifiers may originate in model output.
They retain bounded text after conservative redaction. This is intentionally
not a general secret scanner or PII classifier.

### 2. Provider boundary drops bodies

The OpenAI-compatible adapter will no longer include response text in
400/401/403/5xx exceptions. Transport exceptions retain only a safe provider
category/type summary. The public typed exception hierarchy and HTTP status in
messages remain compatible.

`model.response.received` failure details gain structured `error_type`,
`category`, and `error_source`; its existing `error` field remains present.

### 3. Targeted downstream migration

Use fully external summaries for provider transport/body categories and
model-reflector provider/validation failure events. Use bounded/redacted
mixed-trust summaries for Arion-generated `RealModelPlanner` and engine
plan-validation messages, preserving fail-closed reasons without preserving
unbounded or obvious credential-shaped identifiers.

Use bounded/redacted trusted-text handling at the central engine capability
exception catches and in `SinkFailure`. This preserves useful filesystem,
network timeout, and extension diagnostics while preventing unbounded or
obvious credential-shaped values from propagating to task state, scheduler
terminal metadata, events, and memory.

No global exception interception is added.

## Data retained for diagnosis

- typed category (`provider_unavailable`, `provider_auth`,
  `schema_validation`, and existing categories);
- exception class name;
- trust source (`trusted`, `mixed`, or `external`);
- HTTP status code where applicable;
- bounded safe capability/internal message text;
- existing task/event IDs and timestamps.

## Compatibility

- No database or JSONL schema migration is required.
- Existing persisted events/tasks/memories remain readable and unchanged.
- Existing event `error`, `error_type`, and `category` keys remain available;
  structured source metadata is additive.
- Provider exception classes and categories are unchanged.
- HTTP status remains in provider exception messages; raw response body text is
  intentionally removed.
- Capability interfaces continue to raise `CapabilityError`; sanitization
  occurs only when errors cross into durable engine state/observability.

## Test strategy

Tests must prove through real paths that:

1. a provider body echoing API key, Authorization header, prompt, and completion
   text is absent from provider exceptions and all durable task/event/memory
   representations;
2. arbitrary transport exception text is not emitted;
3. model-controlled schema diagnostics are bounded/redacted while useful
   fail-closed reasons and category/type survive;
4. mixed-trust capability errors are bounded and credential patterns redacted;
5. `SinkFailure` diagnostics are bounded/redacted;
6. safe internal diagnostic text remains useful;
7. legacy event loading and scheduler telemetry behavior remain unchanged;
8. the complete regression suite remains green.

## Explicit deferrals

- Sanitizing intentional authorization resource identifiers (including URL
  query parameters); this needs a resource-specific audit policy.
- HTTP capability result/header/body retention. Those values are task results,
  not error observability, and need a separate result-data ADR.
- General DLP, entropy scanning, PII classification, enterprise secret
  scanners, SIEM integration, encrypted audit storage, and key management.
- Distributed tracing/logging policy and cross-service propagation.
- Rewriting every repository exception or historical persisted row.
- Sanitizing end-user goal descriptions outside error paths.

## Verification

- Verified combined Phase 24/25 baseline: **1,318 passed, 2 skipped**.
- ADR-034 security-boundary tests: **9 passed**.
- Broad focused provider, planning, reflection, capability, event, scheduler,
  and memory regression run: **156 passed, 1 skipped**.
- Complete suite after implementation: **1,327 passed, 2 skipped**.
