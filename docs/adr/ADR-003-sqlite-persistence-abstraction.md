# ADR-003 — SQLite persistence abstraction

- **Status:** Approved (2026-08-09)

## Context

Arion's state (goals, tasks, plans, checkpoints, audit events, later memories
and preferences) must survive process restarts and grow into multiple stores.
Today there is no infrastructure; tomorrow there may be Postgres and a vector
store.

## Decision

**SQLite-first, behind a `Storage` protocol.** All persistence goes through
`Storage` (save/load goals, tasks, checkpoints, events). The initial
implementation `SQLiteStorage`:

- WAL journal mode for crash safety and concurrent readers;
- tasks stored as **full JSON snapshots** (not normalized rows) so every
  checkpoint is a faithful, restorable image of task state;
- checkpoints as immutable, ordered history per task;
- audit events as structured JSON rows.

Layer separation means nothing outside `state/store.py` knows SQL exists.

## Consequences

- Zero-infra durability; `arion_data/arion.db` is the whole state.
- Swapping in Postgres/vector store = one new `Storage` implementation.
- Snapshot-style storage costs space but buys crash-recovery simplicity.

## Related

ADR-001 (state layer), ADR-007 (audit events stored here).
