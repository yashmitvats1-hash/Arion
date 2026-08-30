# ADR-058 — Authenticated Human-in-the-Loop Approval API (M6-A)

- **Status:** Approved (M6-A)
- **Deciders:** Arena AI (engineering agent)

## Context

Arion operates with a strict default-deny posture for high-risk actions (e.g., `filesystem.write`). If a plan requires a write, it enters a `PENDING` state in the durable queue (`ApprovalStore`). Currently, the only way to resolve this is by manually polling the SQLite database via a local CLI (`arion approvals resolve`).

To function effectively as an agent, Arion needs a streamlined channel to notify humans, present the planned task, and capture their explicit authorization to proceed. 

This requires crossing a network boundary. However, Arion's architecture forbids exposing arbitrary remote execution APIs and strictly requires zero runtime dependencies.

## Decision

We will implement an authenticated HTTP API adapter strictly scoped to the durable approval queue (M6-A).

1. **Transport:** Use the Python standard library `http.server.ThreadingHTTPServer`. No external dependencies like FastAPI, preserving the zero-dependency posture.
2. **Authentication:** Implement a static Bearer token authentication map via a new environment variable `ARION_API_TOKENS`.
3. **Identity Mapping:** The API maps the authenticated token to a structural `Actor` identity (e.g., `token:user:alice`). The API strictly enforces this mapping; caller-provided identities in the request payload are ignored.
4. **API Boundary:** Expose only `/health`, `/api/v1/approvals` (list), `/api/v1/approvals/<id>` (show), and `/api/v1/approvals/<id>/resolve` (approve/deny).
5. **No Second Authorization Path:** The API does not authorize execution. It merely calls `engine.resolve_approval_request()`, relying entirely on the existing fingerprinting, schema validation, and SQLite atomicity.

## Consequences

- The `ArionEngine` remains unaware of HTTP, tokens, or network concepts.
- The model remains completely locked out of the approval layer.
- Deterministic integrity is preserved: the human sees the exact deterministic summary built from the capability ActionSpec. Operational parameters (like `content`) are excluded from the API response to bound information disclosure and avoid leaking arbitrary model-generated state.
- Idempotency and concurrency safety come natively from the existing SQLite `expected_task_updated_at` checks (atomic decisions).
- **Security:** Bearer tokens must be transported over a secure channel (e.g., TLS) in distributed deployments. 

## Related

- ADR-009 (Resource-aware authorization)
- ADR-018 (Approval queue)
- ADR-038 (Atomic approval decisions)
