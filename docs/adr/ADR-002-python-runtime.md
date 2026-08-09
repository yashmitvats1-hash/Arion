# ADR-002 — Python runtime

- **Status:** Approved (2026-08-09)

## Context

The vertical slice needs a runtime with strong stdlib support for persistence
(sqlite3), type hints, dataclasses, and a mature agentic-tooling ecosystem.

## Decision

**Python 3.12+** as the primary project target. `pyproject.toml` declares
`requires-python = ">=3.11"` so the package installs on the current dev
sandbox (3.11.2) while CI/deploy can target 3.12+. Code avoids 3.12-only
constructs so both runtimes are supported.

No heavyweight web framework in the core. No async runtime dependency for the
first slice. Packaging via `setuptools`/`pyproject.toml`; tests via `pytest`
(dev extra only).

## Consequences

- SQLite persistence, dataclasses and typing come from the stdlib — zero
  infrastructure to run the spine.
- The `Storage`/`Capability`/`Planner` protocols keep provider choices
  (Postgres, vector DB, alternative LLM SDKs) swappable.
- When agents/parallelism arrive, we can introduce asyncio or subprocess
  workers without changing layer contracts.

## Related

ADR-003 (SQLite persistence abstraction), ADR-008 (deterministic testing).
