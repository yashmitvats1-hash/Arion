# Arion

Autonomous personal computing system (JARVIS/FRIDAY-class). Agentic spine with
persistent state, planning, capabilities, verification and long-running goals
— not a chatbot.

## Status

Vertical slice (v0.1) — see [`docs/architecture.md`](docs/architecture.md) and
[`docs/adr/`](docs/adr/).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/arion run "summarize this repository"
.venv/bin/python -m pytest
```
