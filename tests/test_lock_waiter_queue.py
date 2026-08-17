"""Durable FIFO mutation-lock wait queue - store semantics (ADR-023).

- durable FIFO waiter ordering: positions assigned in enqueue order, survive
  restart, head (oldest eligible) acquires first, newer waiters cannot
  overtake;
- queue state: waiter identity, task/goal/step, canonical resource, enqueue
  time, position, deadline, attempts, status - never contents/secrets;
- timeout / stale / terminal waiters leave the queue cleanly; removal never
  corrupts remaining positions;
- release + next-waiter selection is atomic at the SQLite layer;
- acquire without a waiter stays ADR-021-compatible (immediate contention).
"""

from datetime import datetime, timedelta, timezone

import pytest

from arion.state.locks import LockWaiterStatus, MutationLockError, canonical_resource
from arion.state.models import Task, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plus(iso: str, seconds: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()


def _store(db_path):
    return SQLiteStorage(db_path)


def _seed_task(st, task_id, status=TaskStatus.RUNNING):
    """Every waiter's task must exist in the tasks table (the FIFO head
    eligibility JOINs against it)."""
    st.save_task(Task(id=task_id, goal_id=f"goal-{task_id}", description="x",
                      status=status))
    return task_id


def _enqueue(st, task_id, resource="notes.txt", step=0, deadline_secs=60,
             now=None, seed=True):
    if seed:
        _seed_task(st, task_id)
    now = now or _now()
    return st.enqueue_waiter(FS, canonical_resource(FS, resource), task_id,
                             f"goal-{task_id}", step,
                             _plus(now, deadline_secs), now=now)


def test_fifo_positions_durable_in_enqueue_order(tmp_path):
    st = _store(tmp_path / "a.db")
    a = _enqueue(st, "task_a")
    b = _enqueue(st, "task_b")
    c = _enqueue(st, "task_c")
    assert (a.seq, b.seq, c.seq) == (1, 2, 3)
    # a different resource restarts its own sequence
    d = st.enqueue_waiter(FS, canonical_resource(FS, "other.txt"), "task_d",
                          "goal-d", 0, _plus(_now(), 60), now=_now())
    assert d.seq == 1
    st.close()


def test_queue_position_survives_restart(tmp_path):
    db = tmp_path / "b.db"
    st1 = _store(db)
    a = _enqueue(st1, "task_a")
    b = _enqueue(st1, "task_b")
    st1.close()

    st2 = _store(db)  # fresh process
    assert st2.get_waiter(a.waiter_id).seq == 1
    assert st2.get_waiter(b.waiter_id).seq == 2
    assert st2.get_waiter(a.waiter_id).status == LockWaiterStatus.QUEUED
    st2.close()


def test_head_waiter_acquires_first_fifo(tmp_path):
    """The oldest eligible waiter acquires; a newer waiter is refused until
    the older one has acquired AND released."""
    db = tmp_path / "c.db"
    st = _store(db)
    now = _now()
    a = _enqueue(st, "task_a", now=now)
    b = _enqueue(st, "task_b", now=now)
    # B tries first: not its turn
    with pytest.raises(MutationLockError, match="not this waiter's turn"):
        st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                   "write", "owner-b", 300, now=now, waiter_id=b.waiter_id)
    # A acquires
    lock_a = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-a", 300, now=now, waiter_id=a.waiter_id)
    assert lock_a is not None
    # A releases -> B (now head) acquires
    assert st.release(lock_a.lock_id, "owner-a") is True
    lock_b = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-b", 300, now=now, waiter_id=b.waiter_id)
    assert lock_b is not None
    st.close()


def test_newer_waiter_cannot_repeatedly_overtake(tmp_path):
    st = _store(tmp_path / "d.db")
    now = _now()
    a = _enqueue(st, "task_a", now=now)
    b = _enqueue(st, "task_b", now=now)
    c = _enqueue(st, "task_c", now=now)
    # C tries many times: never its turn while A and B are queued
    for _ in range(5):
        with pytest.raises(MutationLockError, match="not this waiter's turn"):
            st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                       "write", "owner-c", 300, now=now, waiter_id=c.waiter_id)
    # A acquires, releases -> B, not C
    lock_a = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-a", 300, now=now, waiter_id=a.waiter_id)
    st.release(lock_a.lock_id, "owner-a")
    lock_b = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-b", 300, now=now, waiter_id=b.waiter_id)
    assert lock_b is not None
    # C IS now head (A and B already won), but the lock is held by B: it can
    # only ever contend - never acquire while B owns the lock
    with pytest.raises(MutationLockError, match="locked by another owner"):
        st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                   "write", "owner-c", 300, now=now, waiter_id=c.waiter_id)
    st.close()


def test_waiter_state_bounded_identifiers_only(tmp_path):
    st = _store(tmp_path / "e.db")
    w = _enqueue(st, "task_a")
    d = w.to_dict()
    assert set(d) == {"waiter_id", "resource_kind", "resource", "task_id",
                      "goal_id", "step_index", "seq", "enqueued_at", "deadline",
                      "attempts", "next_retry", "status", "created_at",
                      "updated_at"}
    assert "content" not in d and "secret" not in d and "data" not in d
    table_cols = [r[1] for r in st._conn.execute(
        "PRAGMA table_info(mutation_lock_waiters)").fetchall()]
    assert "content" not in table_cols and "payload" not in table_cols
    st.close()


def test_expired_waiter_leaves_queue_cleanly(tmp_path):
    """A waiter whose deadline passes is no longer eligible; its row is marked
    timed_out; a fresh waiter on the same resource is unaffected."""
    db = tmp_path / "f.db"
    st = _store(db)
    now = _now()
    _seed_task(st, "task_old")
    old = st.enqueue_waiter(FS, canonical_resource(FS, "notes.txt"), "task_old",
                            "g", 0, _plus(now, 1), now=now)
    # after expiry, the old waiter is not head: acquire marks it timed_out and
    # refuses (no eligible waiter for its id)
    later = _plus(now, 100)
    with pytest.raises(MutationLockError, match="not this waiter's turn"):
        st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                   "write", "owner-old", 300, now=later, waiter_id=old.waiter_id)
    assert st.get_waiter(old.waiter_id).status == LockWaiterStatus.TIMED_OUT
    # a fresh waiter acquires fine
    _seed_task(st, "task_new")
    fresh = st.enqueue_waiter(FS, canonical_resource(FS, "notes.txt"), "task_new",
                              "g2", 0, _plus(later, 60), now=later)
    lock = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                      "write", "owner-new", 300, now=later, waiter_id=fresh.waiter_id)
    assert lock is not None
    st.close()


def test_terminal_task_waiter_not_eligible(tmp_path):
    """A waiter whose task is terminal (failed/completed) is skipped: the next
    eligible waiter becomes head."""
    db = tmp_path / "g.db"
    st = _store(db)
    now = _now()
    # dead task: FAILED in the tasks table
    _seed_task(st, "task_dead", status=TaskStatus.FAILED)
    w_dead = st.enqueue_waiter(FS, canonical_resource(FS, "notes.txt"), "task_dead",
                               "g", 0, _plus(now, 600), now=now)
    w_alive = _enqueue(st, "task_alive", now=now)
    # the dead waiter is skipped; the alive one is head and acquires
    lock = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                      "write", "owner-alive", 300, now=now, waiter_id=w_alive.waiter_id)
    assert lock is not None
    # and the dead waiter cannot acquire
    with pytest.raises(MutationLockError, match="not this waiter's turn"):
        st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                   "write", "owner-dead", 300, now=now, waiter_id=w_dead.waiter_id)
    st.close()


def test_removing_waiter_does_not_corrupt_positions(tmp_path):
    st = _store(tmp_path / "h.db")
    now = _now()
    a = _enqueue(st, "task_a", now=now)
    b = _enqueue(st, "task_b", now=now)
    c = _enqueue(st, "task_c", now=now)
    # B is cancelled (leaves the queue); A is still head, C still after A
    assert st.dequeue_waiter(b.waiter_id, "cancelled") is True
    assert st.peek_waiter(FS, canonical_resource(FS, "notes.txt"), now=now).waiter_id == a.waiter_id
    lock_a = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-a", 300, now=now, waiter_id=a.waiter_id)
    st.release(lock_a.lock_id, "owner-a")
    # next head is C (B is cancelled), positions unchanged
    assert st.peek_waiter(FS, canonical_resource(FS, "notes.txt"), now=now).waiter_id == c.waiter_id
    assert st.get_waiter(c.waiter_id).seq == 3
    st.close()


def test_release_and_select_next_waiter_atomic(tmp_path):
    """Release + next-waiter selection happen in ONE transaction: the returned
    head is exactly what a subsequent peek returns."""
    db = tmp_path / "i.db"
    st = _store(db)
    now = _now()
    a = _enqueue(st, "task_a", now=now)
    b = _enqueue(st, "task_b", now=now)
    lock_a = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-a", 300, now=now, waiter_id=a.waiter_id)
    released, next_head = st.release_and_select_next(lock_a.lock_id, "owner-a", now=now)
    assert released is True
    assert next_head is not None and next_head.waiter_id == b.waiter_id
    assert st.peek_waiter(FS, canonical_resource(FS, "notes.txt"), now=now).waiter_id == b.waiter_id
    st.close()


def test_release_handoff_skips_ineligible_next(tmp_path):
    """If the next waiter is ineligible (deadline passed), release selects the
    following eligible waiter (or None)."""
    db = tmp_path / "j.db"
    st = _store(db)
    now = _now()
    a = _enqueue(st, "task_a", now=now)
    b = st.enqueue_waiter(FS, canonical_resource(FS, "notes.txt"), "task_b",
                          "g", 0, _plus(now, 1), now=now)  # short deadline
    lock_a = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                        "write", "owner-a", 300, now=now, waiter_id=a.waiter_id)
    later = _plus(now, 100)  # B's deadline long passed
    released, next_head = st.release_and_select_next(lock_a.lock_id, "owner-a", now=later)
    assert released is True
    assert next_head is None  # B was ineligible (marked timed_out)
    assert st.get_waiter(b.waiter_id).status == LockWaiterStatus.TIMED_OUT
    st.close()


def test_reclaim_stale_waiters_idempotent(tmp_path):
    db = tmp_path / "k.db"
    st = _store(db)
    now = _now()
    w = st.enqueue_waiter(FS, canonical_resource(FS, "notes.txt"), "task_x",
                          "g", 0, _plus(now, 1), now=now)
    later = _plus(now, 100)
    assert st.reclaim_stale_waiters(now=later) == [w.waiter_id]
    assert st.reclaim_stale_waiters(now=later) == []  # idempotent
    assert st.get_waiter(w.waiter_id).status == LockWaiterStatus.TIMED_OUT
    st.close()


def test_acquire_without_waiter_remains_adr021_compatible(tmp_path):
    """acquire() without a waiter_id keeps the immediate ADR-021 semantics."""
    st = _store(tmp_path / "l.db")
    now = _now()
    lock = st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                      "write", "owner-x", 300, now=now)
    assert lock is not None
    with pytest.raises(MutationLockError, match="locked by another owner"):
        st.acquire(FS, canonical_resource(FS, "notes.txt"), "filesystem.write",
                   "write", "owner-y", 300, now=now)
    assert st.release(lock.lock_id, "owner-x") is True
    st.close()


def test_dequeue_idempotent_and_unknown(tmp_path):
    st = _store(tmp_path / "m.db")
    now = _now()
    w = _enqueue(st, "task_a", now=now)
    assert st.dequeue_waiter(w.waiter_id, "acquired") is True
    assert st.dequeue_waiter(w.waiter_id, "acquired") is False  # idempotent
    assert st.dequeue_waiter("waiter_nope", "cancelled") is False
    assert st.get_waiter(w.waiter_id).status == LockWaiterStatus.ACQUIRED
    st.close()
