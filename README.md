# Arion

A local-first autonomous-agent prototype with persistent state, planning,
capability-governed tool use, human approval, verification, recovery and
long-horizon goal management. Arion is a research stepping stone toward more
general intelligence; it is not claimed to be human-level AGI or conscious.

## Project direction

- [Project charter](docs/PROJECT_CHARTER.md) — mission, non-goals, safety boundaries and autonomy ladder.
- [Roadmap](docs/ROADMAP.md) — phased plan from re-baselining through a reproducible supervised demo.
- [Agent workstreams](docs/AGENT_WORKSTREAMS.md) — responsibilities and cross-review protocol.
- [Architecture](docs/architecture.md) and [ADRs](docs/adr/) — current implementation and recorded decisions.

The immediate priority is trustworthy end-to-end execution: a task must be
verified against its intended outcome before Arion reports success. Model
planning remains opt-in; mutations remain subject to authorization and human
approval. See the charter for release gates and deferred work.

## Status

Vertical slice (v0.1); the M9-B.3 multi-resource mutation sequence is the
near-term engineering target. Re-baseline the exact current `main` commit
before relying on historical test totals.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/arion run "summarize this repository"
.venv/bin/python -m pytest
```
