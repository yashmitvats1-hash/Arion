"""Recovery-journal interface closure (issue #23 third-pass review).

The mutation-intent journal's settlement interface was incomplete:

- settle accepted ANY outcome string (typos silently corrupted the
  bookkeeping contract);
- a row could be settled as ``recovery`` with NO recovery record backing
  the claim (fake recovery settlement);
- a row settled by recovery did not record WHICH recovery record took over
  the obligation;
- there was no point query for one (task, step) row.

These tests pin the closed interface: validated outcomes, recovery-id
mandatory for recovery settlement, durable linkage to the recovery record,
and schema migration for pre-existing journals.
"""

from __future__ import annotations

import sqlite3

import pytest

from arion.state.intents import (
    INTENT_IN_FLIGHT,
    INTENT_OUTCOMES,
    INTENT_SETTLED,
    OUTCOME_RECOVERY,
    OUTCOME_SUCCEEDED,
    MutationIntent,
    MutationIntentError,
)
from arion.state.store import SQLiteStorage


def _intent(intent_id="intent-1", task_id="t1", step_index=0):
    return MutationIntent(
        intent_id=intent_id, task_id=task_id, step_index=step_index,
        attempt=1, capability="filesystem.append", action="append",
        resource="journal.txt",
    )


def _store(tmp_path):
    return SQLiteStorage(tmp_path / "journal.db")


# --------------------------------------------------------------------- #
# outcome validation (fail closed)
# --------------------------------------------------------------------- #

def test_invalid_outcome_refused_row_stays_open(tmp_path):
    storage = _store(tmp_path)
    storage.record_mutation_intent(_intent())
    with pytest.raises(MutationIntentError, match="invalid mutation-intent outcome"):
        storage.settle_mutation_intent("t1", 0, "succeeeded")  # typo
    row = storage.get_mutation_intent("t1", 0)
    assert row is not None
    assert row.status == INTENT_IN_FLIGHT
    assert row.outcome is None
    assert set(INTENT_OUTCOMES) == {"succeeded", "failed", "recovery"}
    storage.close()


def test_recovery_settlement_without_record_id_refused(tmp_path):
    """No fake recovery settlement: the journal cannot be closed with a
    recovery claim that names no durable recovery record."""
    storage = _store(tmp_path)
    storage.record_mutation_intent(_intent())
    with pytest.raises(MutationIntentError, match="requires the id"):
        storage.settle_mutation_intent("t1", 0, OUTCOME_RECOVERY)
    row = storage.get_mutation_intent("t1", 0)
    assert row is not None and row.status == INTENT_IN_FLIGHT
    storage.close()


# --------------------------------------------------------------------- #
# recovery linkage
# --------------------------------------------------------------------- #

def test_recovery_settlement_stores_recovery_id(tmp_path):
    storage = _store(tmp_path)
    storage.record_mutation_intent(_intent())
    settled = storage.settle_mutation_intent(
        "t1", 0, OUTCOME_RECOVERY, recovery_id="recovery-7",
    )
    assert settled == 1
    row = storage.get_mutation_intent("t1", 0)
    assert row is not None
    assert row.status == INTENT_SETTLED
    assert row.outcome == OUTCOME_RECOVERY
    assert row.recovery_id == "recovery-7"
    storage.close()


def test_ordinary_settlement_stores_no_recovery_id(tmp_path):
    storage = _store(tmp_path)
    storage.record_mutation_intent(_intent())
    storage.settle_mutation_intent("t1", 0, OUTCOME_SUCCEEDED)
    row = storage.get_mutation_intent("t1", 0)
    assert row is not None
    assert row.outcome == OUTCOME_SUCCEEDED
    assert row.recovery_id is None
    storage.close()


# --------------------------------------------------------------------- #
# point query + duplicates (unchanged guarantees)
# --------------------------------------------------------------------- #

def test_get_mutation_intent_point_query(tmp_path):
    storage = _store(tmp_path)
    assert storage.get_mutation_intent("t1", 0) is None
    storage.record_mutation_intent(_intent())
    row = storage.get_mutation_intent("t1", 0)
    assert row is not None and row.intent_id == "intent-1"
    assert storage.get_mutation_intent("t1", 1) is None
    storage.close()


def test_duplicate_intent_still_refused(tmp_path):
    storage = _store(tmp_path)
    storage.record_mutation_intent(_intent())
    with pytest.raises(MutationIntentError, match="already journaled"):
        storage.record_mutation_intent(_intent(intent_id="intent-2"))
    storage.close()


# --------------------------------------------------------------------- #
# migration of pre-existing journals
# --------------------------------------------------------------------- #

def test_migration_adds_recovery_id_column(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE mutation_intents ("
        "intent_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "step_index INTEGER NOT NULL, attempt INTEGER NOT NULL, "
        "capability TEXT NOT NULL, action TEXT NOT NULL, resource TEXT, "
        "status TEXT NOT NULL, outcome TEXT, created_at TEXT NOT NULL, "
        "settled_at TEXT)"
    )
    conn.execute(
        "INSERT INTO mutation_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("old-1", "t-old", 0, 1, "filesystem.write", "write", "f.txt",
         "in_flight", None, "2026-01-01T00:00:00Z", None),
    )
    conn.commit()
    conn.close()

    storage = SQLiteStorage(db)
    rows = storage.list_mutation_intents()
    assert len(rows) == 1
    assert rows[0].intent_id == "old-1"
    assert rows[0].recovery_id is None
    # the migrated journal enforces the closed interface too
    with pytest.raises(MutationIntentError):
        storage.settle_mutation_intent("t-old", 0, "bogus")
    storage.settle_mutation_intent(
        "t-old", 0, OUTCOME_RECOVERY, recovery_id="recovery-1",
    )
    row = storage.get_mutation_intent("t-old", 0)
    assert row is not None and row.recovery_id == "recovery-1"
    storage.close()
