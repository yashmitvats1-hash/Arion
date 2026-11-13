"""F10 fifth pass item G: SQLite durability contract (issue #23).

The mutation-intent journal and recovery registry are the evidence that
guards against duplicate side effects, so their rows must survive a power
loss, not only a process crash.  The store sets ``synchronous=FULL``
EXPLICITLY (not relying on build defaults), reads it back, and fails
closed if FULL cannot be obtained.  These tests pin the measured
effective settings on the actual connection and the fail-closed guard.
"""

from __future__ import annotations

import sqlite3

import pytest

from arion.state.store import SQLiteStorage


def test_effective_pragmas_are_wal_and_full(tmp_path):
    store = SQLiteStorage(tmp_path / "dur.db")
    try:
        sync = store._conn.execute("PRAGMA synchronous").fetchone()[0]
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert sync == 2  # 2 == FULL: committed rows survive power loss
        assert str(mode).lower() == "wal"
    finally:
        store.close()


def test_committed_rows_survive_reopen(tmp_path):
    """Process-crash model: WAL replay restores all committed rows."""
    db = tmp_path / "dur.db"
    store = SQLiteStorage(db)
    from arion.state.intents import MutationIntent
    store.record_mutation_intent(MutationIntent(
        intent_id="intent-dur-1", task_id="task-dur", step_index=0,
        attempt=1, capability="filesystem.append", action="append",
        resource="f.txt",
    ))
    store.close()
    reopened = SQLiteStorage(db)
    try:
        rows = reopened.list_mutation_intents(task_id="task-dur")
        assert len(rows) == 1
        assert rows[0].intent_id == "intent-dur-1"
    finally:
        reopened.close()


def test_store_fails_closed_without_full_synchronous(tmp_path, monkeypatch):
    """If the engine cannot obtain FULL durability the store refuses to
    open: an unverifiable journal cannot back the F10 recovery invariant."""

    class _Cursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return (self._value,)

    class _Wrapper:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            if sql.strip().upper() == "PRAGMA SYNCHRONOUS":
                return _Cursor(1)  # simulate the pragma not taking (NORMAL)
            return self._conn.execute(sql, *args)

    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        return _Wrapper(real_connect(*args, **kwargs))

    monkeypatch.setattr("arion.state.store.sqlite3.connect", fake_connect)
    with pytest.raises(RuntimeError) as excinfo:
        SQLiteStorage(tmp_path / "no-full.db")
    assert "power-loss durability" in str(excinfo.value)
