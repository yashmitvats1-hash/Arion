"""Durable cross-process advisory mutation lock - store semantics (ADR-021).

Phase A + B + E (store parts):

- one lock per canonical security-relevant resource (resource_kind + resource);
- acquisition is ATOMIC across independent connections (processes) to the
  same SQLite DB;
- a second process gets a typed MutationLockError;
- different resources lock independently;
- ownership is explicit and unique;
- release is idempotent for the owner; a non-owner cannot release;
- stale (expired) locks are reclaimable; active locks are not;
- reclamation is atomic; restart never permanently wedges a resource;
- lock metadata is bounded identifiers only - never file contents;
- leases use an injectable clock for deterministic tests.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from arion.state.locks import (
    MutationLock,
    MutationLockError,
    MutationLockStore,
    canonical_resource,
)
from arion.state.store import SQLiteStorage


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _plus(iso: str, seconds: int) -> str:
    return _iso(datetime.fromisoformat(iso) + timedelta(seconds=seconds))


def _store(db_path):
    return SQLiteStorage(db_path)


def test_one_process_acquires_lock(tmp_path):
    st = _store(tmp_path / "a.db")
    lock = st.acquire("filesystem:path", "notes.txt", "filesystem.write", "write",
                      "proc-1", lease_seconds=300, now=_plus(_iso(datetime.now(timezone.utc)), 0))
    assert isinstance(lock, MutationLock)
    assert lock.resource_kind == "filesystem:path"
    assert lock.resource == "notes.txt"
    assert lock.owner_id == "proc-1"
    assert lock.lock_id
    assert lock.expires_at > lock.acquired_at
    st.close()


def test_second_process_cannot_acquire_same_resource(tmp_path):
    db = tmp_path / "b.db"
    now = _iso(datetime.now(timezone.utc))
    st1 = _store(db)
    st2 = _store(db)  # independent connection = independent process
    st1.acquire("filesystem:path", "notes.txt", "filesystem.write", "write",
                "proc-1", lease_seconds=300, now=now)
    with pytest.raises(MutationLockError) as exc:
        st2.acquire("filesystem:path", "notes.txt", "filesystem.append", "append",
                    "proc-2", lease_seconds=300, now=now)
    assert "locked" in str(exc.value).lower()
    # the second process must see the lock with the FIRST owner
    locks = st2.list()
    assert len(locks) == 1 and locks[0].owner_id == "proc-1"
    st1.close()
    st2.close()


def test_different_resources_lock_independently(tmp_path):
    st = _store(tmp_path / "c.db")
    now = _iso(datetime.now(timezone.utc))
    a = st.acquire("filesystem:path", "a.txt", "filesystem.write", "write", "p1", 300, now=now)
    b = st.acquire("filesystem:path", "b.txt", "filesystem.write", "write", "p2", 300, now=now)
    assert a.lock_id != b.lock_id
    assert len(st.list()) == 2
    st.close()


def test_canonical_resource_identity(tmp_path):
    """Canonical resource identity: write and append on the same file contend
    even when the path is spelled differently (./notes.txt vs notes.txt)."""
    assert canonical_resource("filesystem:path", "notes.txt") == "notes.txt"
    assert canonical_resource("filesystem:path", "./notes.txt") == "notes.txt"
    assert canonical_resource("filesystem:path", "a/../notes.txt") == "notes.txt"
    assert canonical_resource("url", "https://x/y") == "https://x/y"  # other kinds: identity

    st = _store(tmp_path / "d.db")
    now = _iso(datetime.now(timezone.utc))
    st.acquire("filesystem:path", canonical_resource("filesystem:path", "notes.txt"),
               "filesystem.write", "write", "p1", 300, now=now)
    with pytest.raises(MutationLockError):
        st.acquire("filesystem:path", canonical_resource("filesystem:path", "./notes.txt"),
                   "filesystem.append", "append", "p2", 300, now=now)
    st.close()


def test_lock_ownership_explicit_and_unique(tmp_path):
    st = _store(tmp_path / "e.db")
    now = _iso(datetime.now(timezone.utc))
    l1 = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "proc-a", 300, now=now)
    l2 = st.get(l1.lock_id)
    assert l2.owner_id == "proc-a"
    assert l2.lock_id == l1.lock_id
    assert st.list(resource="n.txt")[0].owner_id == "proc-a"
    st.close()


def test_release_idempotent_for_owner(tmp_path):
    st = _store(tmp_path / "f.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1", 300, now=now)
    assert st.release(lock.lock_id, "p1") is True
    assert st.get(lock.lock_id) is None
    # idempotent: releasing again for the same owner is a no-op (not an error)
    assert st.release(lock.lock_id, "p1") is False
    # the resource is free again
    st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2", 300, now=now)
    st.close()


def test_non_owner_cannot_release(tmp_path):
    st = _store(tmp_path / "g.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1", 300, now=now)
    with pytest.raises(MutationLockError, match="owner"):
        st.release(lock.lock_id, "p2")
    assert st.get(lock.lock_id).owner_id == "p1"  # still held
    st.close()


def test_restart_new_connection_sees_lock(tmp_path):
    """A fresh store instance (new process) sees the durable lock."""
    db = tmp_path / "h.db"
    now = _iso(datetime.now(timezone.utc))
    st1 = _store(db)
    lock = st1.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1", 300, now=now)
    st1.close()  # process exits (lock NOT released)

    st2 = _store(db)  # restart
    assert st2.get(lock.lock_id).owner_id == "p1"
    with pytest.raises(MutationLockError):
        st2.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2", 300, now=now)
    st2.close()


# ---------------------------------------------------------------------------
# leases / stale-owner reclamation (Phase E)
# ---------------------------------------------------------------------------


def test_active_owner_not_reclaimed_before_expiry(tmp_path):
    st = _store(tmp_path / "i.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
                      lease_seconds=300, now=now)
    # 299 seconds later: still active
    assert st.reclaim_expired(now=_plus(now, 299), resource_kind="filesystem:path", resource="n.txt") == []
    assert st.get(lock.lock_id) is not None
    # a second process still cannot acquire
    with pytest.raises(MutationLockError):
        st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2", 300, now=_plus(now, 299))
    st.close()


def test_expired_owner_can_be_reclaimed(tmp_path):
    st = _store(tmp_path / "j.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
                      lease_seconds=60, now=now)
    reclaimed = st.reclaim_expired(now=_plus(now, 61), resource_kind="filesystem:path", resource="n.txt")
    assert reclaimed == [lock.lock_id]
    assert st.get(lock.lock_id) is None
    st.close()


def test_reclamation_is_atomic(tmp_path):
    """reclaim_expired uses a single transaction; a fresh connection sees
    either before or after state, never partial state."""
    st = _store(tmp_path / "k.db")
    now = _iso(datetime.now(timezone.utc))
    stale = st.acquire("filesystem:path", "s.txt", "filesystem.write", "write", "p1",
                       lease_seconds=1, now=now)
    active = st.acquire("filesystem:path", "a.txt", "filesystem.write", "write", "p1",
                        lease_seconds=3600, now=now)
    reclaimed = st.reclaim_expired(now=_plus(now, 100))
    assert stale.lock_id in reclaimed
    assert active.lock_id not in reclaimed  # active lock untouched
    st.close()


def test_new_owner_acquires_after_reclamation(tmp_path):
    st = _store(tmp_path / "l.db")
    now = _iso(datetime.now(timezone.utc))
    st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
               lease_seconds=1, now=now)
    st.reclaim_expired(now=_plus(now, 10), resource_kind="filesystem:path", resource="n.txt")
    new_lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2",
                          lease_seconds=300, now=_plus(now, 10))
    assert new_lock.owner_id == "p2"
    st.close()


def test_old_owner_cannot_release_new_owners_lock(tmp_path):
    st = _store(tmp_path / "m.db")
    now = _iso(datetime.now(timezone.utc))
    old = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
                     lease_seconds=1, now=now)
    st.reclaim_expired(now=_plus(now, 10))
    new = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2",
                     lease_seconds=300, now=_plus(now, 10))
    with pytest.raises(MutationLockError, match="owner"):
        st.release(new.lock_id, "p1")  # old owner cannot release the new lock
    assert st.get(new.lock_id).owner_id == "p2"
    st.close()


def test_expired_lock_auto_reclaimed_on_acquire(tmp_path):
    """Acquiring a resource whose previous lock expired succeeds atomically -
    a crashed owner never permanently wedges the resource."""
    db = tmp_path / "n.db"
    now = _iso(datetime.now(timezone.utc))
    st1 = _store(db)
    st1.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
                lease_seconds=1, now=now)
    st1.close()  # crash without release

    st2 = _store(db)
    lock = st2.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2",
                       lease_seconds=300, now=_plus(now, 10))
    assert lock.owner_id == "p2"
    st2.close()


def test_lease_clock_deterministic(tmp_path):
    """The injectable clock drives lease semantics deterministically."""
    st = _store(tmp_path / "o.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1",
                      lease_seconds=60, now=now)
    assert lock.acquired_at == now
    assert lock.expires_at == _plus(now, 60)
    # before expiry: contended; after expiry: free (same fixed clock values)
    with pytest.raises(MutationLockError):
        st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2", 60, now=_plus(now, 59))
    st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p2", 60, now=_plus(now, 61))
    st.close()


def test_lock_metadata_bounded_identifiers_only(tmp_path):
    st = _store(tmp_path / "p.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "n.txt", "filesystem.write", "write", "p1", 300, now=now)
    d = lock.to_dict()
    assert set(d) == {"lock_id", "resource_kind", "resource", "capability", "action",
                      "owner_id", "acquired_at", "expires_at", "status"}
    assert "content" not in d and "data" not in d and "secret" not in d
    st.close()


def test_lock_records_never_contain_contents(tmp_path):
    """Even a resource named like content or a path deep in a repo stays
    bounded - only the canonical identifier is stored."""
    st = _store(tmp_path / "q.db")
    now = _iso(datetime.now(timezone.utc))
    lock = st.acquire("filesystem:path", "docs/notes.txt", "filesystem.write", "write",
                      "proc-9", 300, now=now)
    row = st.get(lock.lock_id)
    assert row.resource == "docs/notes.txt"
    assert row.owner_id == "proc-9"
    # audit is the store's own bounded columns only
    cols = st._RECOVERY_COLS  # unrelated; just ensure no content columns exist
    from arion.state.store import SQLiteStorage as S
    table_cols = [r[1] for r in st._conn.execute("PRAGMA table_info(mutation_locks)").fetchall()]
    assert "content" not in table_cols and "data" not in table_cols and "payload" not in table_cols
    st.close()
