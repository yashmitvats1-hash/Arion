"""State layer: persistence behind a Storage protocol.

SQLite-first implementation (ADR-003). The protocol is the contract; swapping
in Postgres or a vector store later must not change any other layer.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol


def _threadsafe(method):
    """Guard a SQLiteStorage public method with the connection's RLock so the
    ADR-024 worker threads never touch the connection concurrently."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._sql_lock:
            return method(self, *args, **kwargs)

    return wrapper

from arion.state.models import (
    Checkpoint,
    Goal,
    TASK_TERMINAL_STATUSES,
    TASK_TRANSITIONS,
    Task,
    TaskStateError,
    TaskStatus,
    new_id,
    utcnow,
)
from arion.state.recovery import MutationRecovery
from arion.state.locks import (
    INTERNAL_LOCK_RESOURCE_KINDS,
    LockWaiter,
    LockWaiterStatus,
    MutationLock,
    MutationLockError,
)
from arion.state.scheduler_work import (
    SchedulerRegistryError,
    SchedulerStateError,
    SchedulerWork,
    SchedulerWorkStatus,
    legal_transition,
)

# Full snapshots remain the recovery representation; only historical count is
# bounded (ADR-036). Normal two-step tasks produce four checkpoints.
DEFAULT_CHECKPOINT_RETENTION = 8


def _audit_event(*args, **kwargs):
    """Cycle-safe lazy import of AuditEvent (events.py imports this
    package's models)."""
    from arion.observability.events import AuditEvent

    return AuditEvent(*args, **kwargs)

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id                 TEXT PRIMARY KEY,
    description        TEXT NOT NULL,
    source             TEXT NOT NULL,
    status             TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    strategy           TEXT,
    blockers           TEXT NOT NULL DEFAULT '[]',
    progress_metadata  TEXT NOT NULL DEFAULT '{}',
    last_evaluated_at  TEXT,
    last_replan_reason TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL,
    snapshot    TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    snapshot   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, created_at);
CREATE TABLE IF NOT EXISTS audit_events (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    task_id TEXT,
    step_id TEXT,
    kind    TEXT NOT NULL,
    actor   TEXT NOT NULL,
    success INTEGER NOT NULL,
    detail  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON audit_events(task_id, ts);
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id    TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    step_index     INTEGER NOT NULL,
    goal_id        TEXT,
    capability     TEXT NOT NULL,
    action         TEXT NOT NULL,
    scope          TEXT NOT NULL,
    risk           TEXT NOT NULL,
    side_effects   TEXT NOT NULL,
    resource_kind  TEXT,
    resource       TEXT,
    summary        TEXT NOT NULL,
    status         TEXT NOT NULL,
    requester_actor TEXT NOT NULL,
    actor_chain    TEXT NOT NULL,
    params_keys    TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    decision_actor TEXT,
    decided_at     TEXT,
    expired_at     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approval_requests(task_id, step_index);
CREATE TABLE IF NOT EXISTS mutation_recoveries (
    recovery_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    goal_id         TEXT,
    step_index      INTEGER NOT NULL,
    capability      TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource        TEXT,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_recoveries_goal ON mutation_recoveries(goal_id, status);
CREATE INDEX IF NOT EXISTS idx_recoveries_task ON mutation_recoveries(task_id, step_index);
CREATE TABLE IF NOT EXISTS mutation_locks (
    lock_id      TEXT PRIMARY KEY,
    resource_kind TEXT NOT NULL,
    resource     TEXT NOT NULL,
    capability   TEXT NOT NULL,
    action       TEXT NOT NULL,
    owner_id     TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    UNIQUE(resource_kind, resource)
);
CREATE INDEX IF NOT EXISTS idx_locks_resource ON mutation_locks(resource_kind, resource);
CREATE TABLE IF NOT EXISTS mutation_lock_waiters (
    waiter_id     TEXT PRIMARY KEY,
    resource_kind TEXT NOT NULL,
    resource      TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    goal_id       TEXT,
    step_index    INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    enqueued_at   TEXT NOT NULL,
    deadline      TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry    TEXT,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiters_resource ON mutation_lock_waiters(resource_kind, resource, status, seq);
CREATE INDEX IF NOT EXISTS idx_waiters_task ON mutation_lock_waiters(task_id);
CREATE TABLE IF NOT EXISTS scheduler_work (
    work_id          TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    goal_id          TEXT,
    step_index       INTEGER NOT NULL,
    scheduler_id     TEXT NOT NULL,
    worker_id        TEXT,
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    completed_at     TEXT,
    lease_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sched_work_status ON scheduler_work(status, scheduler_id);
CREATE INDEX IF NOT EXISTS idx_sched_work_task ON scheduler_work(task_id, step_index);
CREATE TABLE IF NOT EXISTS scheduler_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_instances (
    scheduler_id     TEXT PRIMARY KEY,
    pid              INTEGER NOT NULL,
    registered_at    TEXT NOT NULL,
    heartbeat_at     TEXT,
    lease_expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_goal_weights (
    goal_id    TEXT PRIMARY KEY,
    weight     INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_goal_state (
    goal_id    TEXT PRIMARY KEY,
    deficit    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_goal_reservations (
    goal_id     TEXT PRIMARY KEY,
    reservation INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_goal_ceilings (
    goal_id     TEXT PRIMARY KEY,
    ceiling     INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_events (
    id             TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    scheduler_id   TEXT,
    worker_id      TEXT,
    goal_id        TEXT,
    task_id        TEXT,
    work_id        TEXT,
    step_index     INTEGER,
    event_type     TEXT NOT NULL,
    reason         TEXT,
    success        INTEGER NOT NULL DEFAULT 1,
    detail         TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sched_events_ts ON scheduler_events(ts);
CREATE INDEX IF NOT EXISTS idx_sched_events_type ON scheduler_events(event_type);
CREATE INDEX IF NOT EXISTS idx_sched_events_work ON scheduler_events(work_id);
CREATE INDEX IF NOT EXISTS idx_sched_events_goal ON scheduler_events(goal_id);
CREATE INDEX IF NOT EXISTS idx_sched_events_sched ON scheduler_events(scheduler_id);
"""


class Storage(Protocol):
    """Persistence contract used by orchestration and observability."""

    def save_goal(self, goal: Goal) -> None: ...
    def cas_goal(self, goal: Goal, expected_version: int) -> bool: ...
    def cas_goal_fields(self, goal_id: str, expected_version: int, fields: dict) -> bool: ...
    def load_goal(self, goal_id: str) -> Goal | None: ...
    def list_goals(self, status: str | None = None) -> list[Goal]: ...

    def save_task(self, task: Task) -> None: ...
    def load_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, status: str | None = None) -> list[Task]: ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...
    def latest_checkpoint(self, task_id: str) -> Checkpoint | None: ...
    def list_checkpoints(self, task_id: str) -> list[Checkpoint]: ...

    def append_event(self, event: AuditEvent) -> None: ...
    def list_events(self, task_id: str | None = None) -> list[AuditEvent]: ...

    def close(self) -> None: ...


class CheckpointRetentionStore(Protocol):
    """Optional storage capability for bounded historical checkpoints."""

    def prune_checkpoints(
        self,
        task_id: str,
        keep_last: int = DEFAULT_CHECKPOINT_RETENTION,
    ) -> int: ...


class SQLiteStorage:
    """Durable SQLite storage. WAL mode; full task snapshots as JSON rows."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # check_same_thread=False: the ADR-024 in-process step scheduler runs
        # steps on bounded worker threads; every public method is guarded by
        # _sql_lock (a threading.RLock), so only one thread touches the
        # connection at a time. Cross-process atomicity is unchanged
        # (BEGIN IMMEDIATE + WAL).
        self._sql_lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._migrate_goals()
        self._migrate_tasks()
        self._migrate_approvals()
        self._conn.commit()

    @_threadsafe
    def _migrate_goals(self) -> None:
        """Lightweight additive migration: extend legacy goals rows with the
        goal-lifecycle columns (ADR-016)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(goals)").fetchall()}
        additions = {
            "version": "ALTER TABLE goals ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            "strategy": "ALTER TABLE goals ADD COLUMN strategy TEXT",
            "blockers": "ALTER TABLE goals ADD COLUMN blockers TEXT NOT NULL DEFAULT '[]'",
            "progress_metadata": "ALTER TABLE goals ADD COLUMN progress_metadata TEXT NOT NULL DEFAULT '{}'",
            "last_evaluated_at": "ALTER TABLE goals ADD COLUMN last_evaluated_at TEXT",
            "last_replan_reason": "ALTER TABLE goals ADD COLUMN last_replan_reason TEXT",
            "updated_at": "ALTER TABLE goals ADD COLUMN updated_at TEXT",
        }
        for col, ddl in additions.items():
            if col not in cols:
                self._conn.execute(ddl)

    @_threadsafe
    def _migrate_tasks(self) -> None:
        """Add the monotonic task CAS token without rewriting snapshots."""
        cols = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()
        }
        if "revision" not in cols:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        # ADR-041: default SQLite always promotes legacy revision-zero rows to
        # a real CAS generation.  The column is authoritative over the legacy
        # JSON snapshot, so historical task/checkpoint payloads are not
        # rewritten and stale revision-zero checkpoints lose timestamp
        # authority immediately on reopen.
        self._conn.execute(
            "UPDATE tasks SET revision=1 WHERE revision=0"
        )

    @_threadsafe
    def _migrate_approvals(self) -> None:
        """Lightweight additive migration: add the expiry column to
        approval_requests created before ADR-019. Never drops data (the
        queue record + audit trail stay intact)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(approval_requests)").fetchall()}
        if "expired_at" not in cols:
            self._conn.execute("ALTER TABLE approval_requests ADD COLUMN expired_at TEXT")

    # ---- goals ----

    @_threadsafe
    def save_goal(self, goal: Goal) -> None:
        """Create or seed a goal row (INSERT OR REPLACE).

        Production lifecycle / metadata writes MUST go through
        :meth:`cas_goal`. This primitive is for creation and explicit
        seeding only — it does not protect against lost updates.
        """
        import json as _json

        goal.updated_at = utcnow()
        self._conn.execute(
            "INSERT OR REPLACE INTO goals "
            "(id, description, source, status, version, strategy, blockers, progress_metadata,"
            " last_evaluated_at, last_replan_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                goal.id,
                goal.description,
                goal.source,
                goal.status_value,
                goal.version,
                goal.strategy,
                _json.dumps(goal.blockers),
                _json.dumps(goal.progress_metadata),
                goal.last_evaluated_at,
                goal.last_replan_reason,
                goal.created_at,
                goal.updated_at,
            ),
        )
        self._conn.commit()

    @_threadsafe
    def cas_goal(self, goal: Goal, expected_version: int) -> bool:
        """Compare-and-swap the authoritative goal row.

        The write is protected by the version::

            UPDATE goals SET ... WHERE id=? AND version=?

        Returns True iff this writer committed (rowcount == 1). A stale
        writer whose ``expected_version`` no longer matches the durable
        row returns False and mutates nothing. ``goal.version`` MUST be
        ``expected_version + 1`` (one increment per successful write).

        Uses one ``BEGIN IMMEDIATE`` transaction so independent
        connections / processes cannot interleave the version check and
        the write.
        """
        import json as _json

        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValueError(
                f"expected_version must be an int, got {expected_version!r} "
                f"(fail closed)")
        if expected_version < 1:
            raise ValueError(
                f"expected_version must be >= 1, got {expected_version!r} "
                f"(fail closed)")
        if goal.version != expected_version + 1:
            raise ValueError(
                f"cas_goal requires goal.version == expected_version + 1 "
                f"(got {goal.version} vs {expected_version}+1; fail closed)")

        now = utcnow()
        goal.updated_at = now
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE goals SET description=?, source=?, status=?, version=?, "
                "strategy=?, blockers=?, progress_metadata=?, last_evaluated_at=?, "
                "last_replan_reason=?, updated_at=? "
                "WHERE id=? AND version=?",
                (
                    goal.description,
                    goal.source,
                    goal.status_value,
                    goal.version,
                    goal.strategy,
                    _json.dumps(goal.blockers),
                    _json.dumps(goal.progress_metadata),
                    goal.last_evaluated_at,
                    goal.last_replan_reason,
                    now,
                    goal.id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def cas_goal_fields(self, goal_id: str, expected_version: int,
                        fields: dict) -> bool:
        """Column-scoped compare-and-swap.

        UPDATE only the supplied columns WHERE id=? AND version=?.
        Lifecycle writers pass ``version`` (expected+1) so a stale
        writer cannot clobber a newer row. Metadata writers may omit
        ``version`` so a progress/strategy patch cannot bump the CAS
        token or overwrite status/blockers.
        """
        import json as _json

        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValueError(
                f"expected_version must be an int, got {expected_version!r} "
                f"(fail closed)")
        if expected_version < 1:
            raise ValueError(
                f"expected_version must be >= 1, got {expected_version!r} "
                f"(fail closed)")
        allowed = {
            "description", "source", "status", "version", "strategy",
            "blockers", "progress_metadata", "last_evaluated_at",
            "last_replan_reason", "updated_at",
        }
        if not fields or not isinstance(fields, dict):
            raise ValueError("cas_goal_fields requires a non-empty fields dict")
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"cas_goal_fields unknown column(s) {sorted(unknown)} (fail closed)")
        if "version" in fields and fields["version"] != expected_version + 1:
            raise ValueError(
                f"cas_goal_fields version must be expected+1 "
                f"(got {fields['version']} vs {expected_version}+1; fail closed)")

        values = dict(fields)
        if "blockers" in values:
            values["blockers"] = _json.dumps(values["blockers"])
        if "progress_metadata" in values:
            values["progress_metadata"] = _json.dumps(values["progress_metadata"])
        if "updated_at" not in values:
            values["updated_at"] = utcnow()
        cols = list(values)
        assignments = ", ".join(f"{c}=?" for c in cols)
        params = [values[c] for c in cols] + [goal_id, expected_version]
        try:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                f"UPDATE goals SET {assignments} WHERE id=? AND version=?",
                params,
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def load_goal(self, goal_id: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_COLS) + " FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        return _goal_from_row(row) if row else None

    @_threadsafe
    def list_goals(self, status: str | None = None) -> list[Goal]:
        cols = ", ".join(_GOAL_COLS)
        if status:
            rows = self._conn.execute(
                f"SELECT {cols} FROM goals WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(f"SELECT {cols} FROM goals ORDER BY created_at").fetchall()
        return [_goal_from_row(r) for r in rows]

    # ---- tasks ----

    @_threadsafe
    def save_task(self, task: Task) -> None:
        """Create or revision-CAS one full task snapshot.

        ``Task.revision`` is the expected durable revision.  A successful
        existing-row write increments it exactly once.  Stale writers and
        attempts to move a terminal task back to a non-terminal status fail
        closed instead of replacing newer state (ADR-040).
        """
        if isinstance(task.revision, bool) or not isinstance(task.revision, int):
            raise TaskStateError("task revision must be a non-negative integer")
        if task.revision < 0:
            raise TaskStateError("task revision must be non-negative")
        expected = task.revision
        previous_updated_at = task.updated_at
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT status, revision FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            now = utcnow()
            if row is None:
                # New domain objects start at revision zero.  A non-zero
                # revision for an absent id is not proof of prior ownership.
                if expected != 0:
                    raise TaskStateError(
                        f"task {task.id} is missing at expected revision "
                        f"{expected} (fail closed)"
                    )
                task.revision = 1
                task.updated_at = now
                self._conn.execute(
                    "INSERT INTO tasks (id, goal_id, description, status, "
                    "snapshot, revision, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (task.id, task.goal_id, task.description,
                     task.status.value, json.dumps(task.to_dict()),
                     task.revision, task.updated_at),
                )
                self._conn.commit()
                return

            durable_status = TaskStatus(row[0])
            durable_revision = int(row[1])
            if durable_revision != expected:
                raise TaskStateError(
                    f"stale task revision for {task.id}: expected {expected}, "
                    f"durable {durable_revision} (fail closed)"
                )
            if durable_status in TASK_TERMINAL_STATUSES:
                raise TaskStateError(
                    f"terminal task {task.id} is immutable "
                    f"({durable_status.value}; fail closed)"
                )
            if task.status not in TASK_TRANSITIONS[durable_status]:
                raise TaskStateError(
                    f"invalid task transition {durable_status.value} -> "
                    f"{task.status.value} for {task.id} (fail closed)"
                )

            task.revision = expected + 1
            task.updated_at = now
            cursor = self._conn.execute(
                "UPDATE tasks SET goal_id=?, description=?, status=?, "
                "snapshot=?, revision=?, updated_at=? "
                "WHERE id=? AND revision=?",
                (task.goal_id, task.description, task.status.value,
                 json.dumps(task.to_dict()), task.revision, task.updated_at,
                 task.id, expected),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(
                    f"task {task.id} lost its revision race (fail closed)"
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            task.revision = expected
            task.updated_at = previous_updated_at
            raise

    @staticmethod
    def _task_from_storage_row(row: tuple[Any, ...]) -> Task:
        task = Task.from_dict(json.loads(row[0]))
        # The column is authoritative for migrated legacy snapshots that do
        # not yet contain a revision field.
        task.revision = int(row[1])
        task.updated_at = row[2]
        return task

    @_threadsafe
    def load_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT snapshot, revision, updated_at FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        return self._task_from_storage_row(row) if row else None

    @_threadsafe
    def list_tasks(self, status: str | None = None) -> list[Task]:
        if status:
            rows = self._conn.execute(
                "SELECT snapshot, revision, updated_at FROM tasks "
                "WHERE status=? ORDER BY updated_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT snapshot, revision, updated_at FROM tasks "
                "ORDER BY updated_at"
            ).fetchall()
        return [self._task_from_storage_row(row) for row in rows]

    # ---- checkpoints ----

    @_threadsafe
    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (id, task_id, status, step_index, snapshot, reason, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                checkpoint.id,
                checkpoint.task_id,
                checkpoint.status,
                checkpoint.step_index,
                json.dumps(checkpoint.snapshot),
                checkpoint.reason,
                checkpoint.created_at,
            ),
        )
        self._conn.commit()

    @_threadsafe
    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT id, task_id, status, step_index, snapshot, reason, created_at FROM checkpoints"
            " WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return Checkpoint.from_dict(_row_to_dict(row, _CKPT_COLS)) if row else None

    @_threadsafe
    def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        rows = self._conn.execute(
            "SELECT id, task_id, status, step_index, snapshot, reason, created_at FROM checkpoints"
            " WHERE task_id=? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        return [Checkpoint.from_dict(_row_to_dict(r, _CKPT_COLS)) for r in rows]

    @_threadsafe
    def prune_checkpoints(
        self,
        task_id: str,
        keep_last: int = DEFAULT_CHECKPOINT_RETENTION,
    ) -> int:
        """Delete only historical checkpoints older than the newest bound.

        Recovery reads the newest row by insertion order. The subquery always
        protects that row (and the other newest rows); failure rolls back this
        optimization without affecting the already committed checkpoint.
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        if (isinstance(keep_last, bool) or not isinstance(keep_last, int)
                or keep_last < 1 or keep_last > 1000):
            raise ValueError("keep_last must be an integer in [1, 1000]")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "DELETE FROM checkpoints WHERE task_id=? AND rowid NOT IN ("
                "SELECT rowid FROM checkpoints WHERE task_id=? "
                "ORDER BY rowid DESC LIMIT ?)",
                (task_id, task_id, keep_last),
            )
            removed = max(0, int(cursor.rowcount))
            self._conn.commit()
            return removed
        except Exception:
            self._conn.rollback()
            raise

    # ---- audit events ----

    @_threadsafe
    def append_event(self, event: AuditEvent) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor, success, detail) VALUES (?,?,?,?,?,?,?,?)",
            (event.id, event.ts, event.task_id, event.step_id, event.kind, event.actor, int(event.success), json.dumps(event.detail)),
        )
        self._conn.commit()

    @_threadsafe
    def list_events(self, task_id: str | None = None) -> list[AuditEvent]:
        if task_id:
            rows = self._conn.execute(
                "SELECT id, ts, task_id, step_id, kind, actor, success, detail FROM audit_events"
                " WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts, task_id, step_id, kind, actor, success, detail FROM audit_events ORDER BY rowid"
            ).fetchall()
        from arion.observability.events import AuditEvent  # noqa: F401 (cycle-safe)
        return [AuditEvent.from_row(r) for r in rows]

    # ---- EventSink protocol (observability -> storage) ----

    @_threadsafe
    def emit(self, event: AuditEvent) -> None:
        """Implement EventSink so the EventLogger can persist events directly."""
        self.append_event(event)

    # ---- approval queue (ADR-018) ----

    _APPROVAL_COLS = (
        "approval_id", "task_id", "step_index", "goal_id", "capability", "action",
        "scope", "risk", "side_effects", "resource_kind", "resource", "summary",
        "status", "requester_actor", "actor_chain", "params_keys", "fingerprint",
        "decision_actor", "decided_at", "expired_at", "created_at", "updated_at",
    )

    @_threadsafe
    def create_request(self, request: "ApprovalRequest") -> "ApprovalRequest":
        """Atomically create or adopt one canonical matching PENDING row."""
        from arion.state.approvals import ApprovalStatus

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                f"SELECT {', '.join(self._APPROVAL_COLS)} FROM approval_requests "
                "WHERE task_id=? AND step_index=? AND status=? ORDER BY rowid",
                (request.task_id, request.step_index,
                 ApprovalStatus.PENDING.value),
            ).fetchall()
            for row in rows:
                existing = _approval_from_row(row)
                if existing.fingerprint == request.fingerprint:
                    self._conn.commit()
                    return existing
            self._conn.execute(
                "INSERT INTO approval_requests "
                f"({', '.join(self._APPROVAL_COLS)}) "
                f"VALUES ({', '.join('?' * len(self._APPROVAL_COLS))})",
                _approval_row(request),
            )
            self._conn.commit()
            return request
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def get_request(self, approval_id: str) -> "ApprovalRequest | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._APPROVAL_COLS)} FROM approval_requests WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        return _approval_from_row(row) if row else None

    @_threadsafe
    def list_requests(self, status: str | None = None) -> list["ApprovalRequest"]:
        cols = ", ".join(self._APPROVAL_COLS)
        if status:
            rows = self._conn.execute(
                f"SELECT {cols} FROM approval_requests WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(f"SELECT {cols} FROM approval_requests ORDER BY created_at").fetchall()
        return [_approval_from_row(r) for r in rows]

    @_threadsafe
    def update_request(self, request: "ApprovalRequest") -> None:
        """Refresh summary without changing approval authority (ADR-044)."""
        from arion.state.approvals import ApprovalError

        previous_updated_at = request.updated_at
        request.updated_at = utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE approval_requests SET summary=?, updated_at=? "
                "WHERE approval_id=? AND status=?",
                (request.summary, request.updated_at, request.approval_id,
                 request.status.value),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                request.updated_at = previous_updated_at
                row = self._conn.execute(
                    "SELECT status FROM approval_requests WHERE approval_id=?",
                    (request.approval_id,),
                ).fetchone()
                if row is None:
                    raise ApprovalError(
                        f"unknown approval id: {request.approval_id}"
                    )
                raise ApprovalError(
                    f"stale approval status for {request.approval_id}: "
                    f"object={request.status.value}, durable={row[0]} "
                    f"(fail closed)"
                )
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            request.updated_at = previous_updated_at
            raise

    @_threadsafe
    def transition_request(
        self,
        request: "ApprovalRequest",
        expected_status: "ApprovalStatus",
    ) -> bool:
        """CAS cleanup-only PENDING -> DENIED | EXPIRED (ADR-044)."""
        from arion.state.approvals import ApprovalError, ApprovalStatus

        if (expected_status != ApprovalStatus.PENDING
                or request.status not in (
                    ApprovalStatus.DENIED, ApprovalStatus.EXPIRED,
                )):
            raise ApprovalError(
                f"request-only transition {expected_status.value} -> "
                f"{request.status.value} is not allowed; APPROVED requires "
                f"atomic request/task commit (fail closed)"
            )
        previous_updated_at = request.updated_at
        request.updated_at = utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE approval_requests SET status=?, decision_actor=?, "
                "decided_at=?, summary=?, expired_at=?, updated_at=? "
                "WHERE approval_id=? AND status=?",
                (request.status.value, request.decision_actor,
                 request.decided_at, request.summary, request.expired_at,
                 request.updated_at, request.approval_id,
                 expected_status.value),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                request.updated_at = previous_updated_at
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            request.updated_at = previous_updated_at
            raise

    @_threadsafe
    def commit_approval_decision(
        self,
        request: "ApprovalRequest",
        task: Task,
        expected_task_updated_at: str,
    ) -> bool:
        """Atomically decide PENDING approval and transition AWAITING task."""
        from arion.state.approvals import ApprovalStatus

        if (request.task_id != task.id
                or request.status not in (
                    ApprovalStatus.APPROVED,
                    ApprovalStatus.DENIED,
                    ApprovalStatus.EXPIRED,
                )):
            return False
        now = utcnow()
        request.updated_at = now
        previous_updated_at = task.updated_at
        expected_revision = task.revision
        task.revision = expected_revision + 1
        task.updated_at = now
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            approval = self._conn.execute(
                "UPDATE approval_requests SET status=?, decision_actor=?, "
                "decided_at=?, summary=?, expired_at=?, updated_at=? "
                "WHERE approval_id=? AND status=?",
                (request.status.value, request.decision_actor,
                 request.decided_at, request.summary, request.expired_at,
                 request.updated_at, request.approval_id,
                 ApprovalStatus.PENDING.value),
            )
            task_row = self._conn.execute(
                "UPDATE tasks SET status=?, snapshot=?, revision=?, updated_at=? "
                "WHERE id=? AND status=? AND revision=? AND updated_at=?",
                (task.status.value, json.dumps(task.to_dict()), task.revision,
                 task.updated_at, task.id,
                 TaskStatus.AWAITING_APPROVAL.value, expected_revision,
                 expected_task_updated_at),
            )
            if approval.rowcount != 1 or task_row.rowcount != 1:
                self._conn.rollback()
                task.revision = expected_revision
                task.updated_at = previous_updated_at
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            task.revision = expected_revision
            task.updated_at = previous_updated_at
            raise

    @_threadsafe
    def reconcile_approval_task(
        self,
        request: "ApprovalRequest",
        task: Task,
        expected_task_updated_at: str,
        expected_task_statuses: tuple[str, ...],
    ) -> bool:
        """CAS task mirror to an already-committed durable decision."""
        if not expected_task_statuses or request.task_id != task.id:
            return False
        now = utcnow()
        previous_updated_at = task.updated_at
        expected_revision = task.revision
        task.revision = expected_revision + 1
        task.updated_at = now
        placeholders = ",".join("?" * len(expected_task_statuses))
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT status FROM approval_requests WHERE approval_id=?",
                (request.approval_id,),
            ).fetchone()
            if row is None or row[0] != request.status.value:
                self._conn.rollback()
                task.revision = expected_revision
                task.updated_at = previous_updated_at
                return False
            cursor = self._conn.execute(
                "UPDATE tasks SET status=?, snapshot=?, revision=?, updated_at=? "
                f"WHERE id=? AND revision=? AND updated_at=? "
                f"AND status IN ({placeholders})",
                (task.status.value, json.dumps(task.to_dict()), task.revision,
                 task.updated_at, task.id, expected_revision,
                 expected_task_updated_at, *expected_task_statuses),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                task.revision = expected_revision
                task.updated_at = previous_updated_at
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            task.revision = expected_revision
            task.updated_at = previous_updated_at
            raise

    @_threadsafe
    def latest_request_for_step(self, task_id: str, step_index: int) -> "ApprovalRequest | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._APPROVAL_COLS)} FROM approval_requests"
            " WHERE task_id=? AND step_index=? ORDER BY rowid DESC LIMIT 1",
            (task_id, step_index),
        ).fetchone()
        return _approval_from_row(row) if row else None

    # ---- mutation recovery registry (ADR-020) ----

    _RECOVERY_COLS = (
        "recovery_id", "task_id", "goal_id", "step_index", "capability", "action",
        "resource", "reason", "status", "created_at", "acknowledged_at", "acknowledged_by",
    )

    @_threadsafe
    def create_recovery(self, recovery: "MutationRecovery") -> "MutationRecovery":
        """Transactionally create or adopt one REQUIRED record per task/step."""
        from arion.state.recovery import RecoveryStatus

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if recovery.status == RecoveryStatus.REQUIRED:
                row = self._conn.execute(
                    f"SELECT {', '.join(self._RECOVERY_COLS)} "
                    "FROM mutation_recoveries WHERE task_id=? AND step_index=? "
                    "AND status=? ORDER BY rowid LIMIT 1",
                    (recovery.task_id, recovery.step_index,
                     RecoveryStatus.REQUIRED.value),
                ).fetchone()
                if row is not None:
                    self._conn.commit()
                    return _recovery_from_row(row)
            self._conn.execute(
                "INSERT INTO mutation_recoveries "
                f"({', '.join(self._RECOVERY_COLS)}) "
                f"VALUES ({', '.join('?' * len(self._RECOVERY_COLS))})",
                _recovery_row(recovery),
            )
            self._conn.commit()
            return recovery
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def commit_recovery_requirement(
        self,
        recovery: "MutationRecovery",
        task: Task,
        expected_task_revision: int,
    ) -> tuple["MutationRecovery", bool, bool]:
        """Commit recovery authority and its failed-task mirror together.

        The REQUIRED record always wins: if the task revision is stale or the
        task is already terminal, the transaction still commits the recovery
        record so Phase 32 fencing cannot be weakened.  Returns
        ``(canonical_recovery, created, task_committed)``.
        """
        from arion.state.recovery import RecoveryStatus

        if (recovery.status != RecoveryStatus.REQUIRED
                or recovery.task_id != task.id
                or task.status != TaskStatus.FAILED
                or isinstance(expected_task_revision, bool)
                or not isinstance(expected_task_revision, int)
                or expected_task_revision < 0):
            raise ValueError("invalid recovery/task atomic commit (fail closed)")
        previous_revision = task.revision
        previous_updated_at = task.updated_at
        created = False
        task_committed = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT {', '.join(self._RECOVERY_COLS)} "
                "FROM mutation_recoveries WHERE task_id=? AND step_index=? "
                "AND status=? ORDER BY rowid LIMIT 1",
                (recovery.task_id, recovery.step_index,
                 RecoveryStatus.REQUIRED.value),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO mutation_recoveries "
                    f"({', '.join(self._RECOVERY_COLS)}) "
                    f"VALUES ({', '.join('?' * len(self._RECOVERY_COLS))})",
                    _recovery_row(recovery),
                )
                canonical = recovery
                created = True
            else:
                canonical = _recovery_from_row(row)

            now = utcnow()
            durable = self._conn.execute(
                "SELECT status, revision FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            if (durable is not None
                    and int(durable[1]) == expected_task_revision
                    and TaskStatus(durable[0]) not in TASK_TERMINAL_STATUSES):
                task.revision = expected_task_revision + 1
                task.updated_at = now
                cursor = self._conn.execute(
                    "UPDATE tasks SET goal_id=?, description=?, status=?, "
                    "snapshot=?, revision=?, updated_at=? "
                    "WHERE id=? AND revision=? AND status NOT IN (?, ?)",
                    (task.goal_id, task.description, task.status.value,
                     json.dumps(task.to_dict()), task.revision, task.updated_at,
                     task.id, expected_task_revision,
                     TaskStatus.COMPLETED.value, TaskStatus.FAILED.value),
                )
                task_committed = cursor.rowcount == 1
            self._conn.commit()
            if not task_committed:
                task.revision = previous_revision
                task.updated_at = previous_updated_at
            return canonical, created, task_committed
        except Exception:
            self._conn.rollback()
            task.revision = previous_revision
            task.updated_at = previous_updated_at
            raise

    @_threadsafe
    def get_recovery(self, recovery_id: str) -> "MutationRecovery | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._RECOVERY_COLS)} FROM mutation_recoveries WHERE recovery_id=?",
            (recovery_id,),
        ).fetchone()
        return _recovery_from_row(row) if row else None

    @_threadsafe
    def list_recoveries(self, status: str | None = None,
                        goal_id: str | None = None,
                        task_id: str | None = None) -> list["MutationRecovery"]:
        cols = ", ".join(self._RECOVERY_COLS)
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if goal_id:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {cols} FROM mutation_recoveries {where} ORDER BY created_at", params
        ).fetchall()
        return [_recovery_from_row(r) for r in rows]

    @_threadsafe
    def update_recovery(self, recovery: "MutationRecovery") -> None:
        """Refresh one recovery row without changing its durable status.

        Compatibility callers may update the bounded diagnostic reason for the
        state they loaded. Status and acknowledgement actor/time are immutable
        here; a stale object cannot reverse or rewrite a decision (ADR-043).
        """
        from arion.state.recovery import RecoveryError

        cursor = self._conn.execute(
            "UPDATE mutation_recoveries SET reason=? "
            "WHERE recovery_id=? AND status=?",
            (recovery.reason, recovery.recovery_id, recovery.status.value),
        )
        self._conn.commit()
        if cursor.rowcount == 1:
            return
        row = self._conn.execute(
            "SELECT status FROM mutation_recoveries WHERE recovery_id=?",
            (recovery.recovery_id,),
        ).fetchone()
        if row is None:
            raise RecoveryError(
                f"unknown recovery id: {recovery.recovery_id}"
            )
        raise RecoveryError(
            f"stale recovery status for {recovery.recovery_id}: "
            f"object={recovery.status.value}, durable={row[0]} (fail closed)"
        )

    @_threadsafe
    def transition_recovery(
        self,
        recovery: "MutationRecovery",
        expected_status: "RecoveryStatus",
    ) -> bool:
        """CAS the only legal recovery transition: REQUIRED -> ACKNOWLEDGED."""
        from arion.state.recovery import RecoveryError, RecoveryStatus

        if (expected_status != RecoveryStatus.REQUIRED
                or recovery.status != RecoveryStatus.ACKNOWLEDGED):
            raise RecoveryError(
                f"invalid recovery transition {expected_status.value} -> "
                f"{recovery.status.value} for {recovery.recovery_id} "
                f"(fail closed)"
            )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE mutation_recoveries SET status=?, acknowledged_at=?, "
                "acknowledged_by=?, reason=? WHERE recovery_id=? AND status=?",
                (recovery.status.value, recovery.acknowledged_at,
                 recovery.acknowledged_by, recovery.reason,
                 recovery.recovery_id, expected_status.value),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    # ---- advisory mutation locks (ADR-021) ----

    _LOCK_COLS = (
        "lock_id", "resource_kind", "resource", "capability", "action",
        "owner_id", "acquired_at", "expires_at",
    )

    @_threadsafe
    def acquire(self, resource_kind: str, resource: str, capability: str,
                action: str, owner_id: str, lease_seconds: float,
                now: str | None = None,
                waiter_id: str | None = None) -> "MutationLock":
        """Atomically acquire the advisory lock for a canonical resource.

        Cross-process safe: BEGIN IMMEDIATE takes the SQLite write lock so no
        other process can interleave; expired rows for the same resource are
        reclaimed inside the SAME transaction (a crashed owner never wedges
        the resource); a live row fails the insert (UNIQUE constraint) and is
        rolled back into a typed MutationLockError. Never 'check then insert'
        outside a transaction.

        ADR-023 fairness: when `waiter_id` is given, the caller may only
        acquire when it is the HEAD of the durable FIFO queue for this
        resource (oldest eligible waiter). Expired queued waiters are marked
        timed_out and terminal-task waiters are skipped inside the same
        transaction, so the head is always the oldest still-eligible waiter
        and a newer waiter can never overtake it. Omitting `waiter_id`
        preserves the ADR-021 immediate (non-queue) semantics.
        """
        from arion.state.locks import LockWaiter, LockWaiterStatus, MutationLock, MutationLockError, _add_seconds

        if now is None:
            now = utcnow()
        expires = _add_seconds(now, max(0.0, float(lease_seconds)))
        lock = MutationLock(
            lock_id=new_id("lock"),
            resource_kind=resource_kind,
            resource=resource,
            capability=capability,
            action=action,
            owner_id=owner_id,
            acquired_at=now,
            expires_at=expires,
        )
        # FIFO fairness (ADR-023): mark expired queued waiters for this
        # resource as timed_out in their OWN committed transaction BEFORE the
        # head check, so the hygiene update is durable even when the acquire
        # below fails (a failed acquire rolls back only the head-check/lock
        # insert, never the cleanup).
        if waiter_id is not None:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
                    "WHERE resource_kind=? AND resource=? AND status=? AND deadline <= ?",
                    (LockWaiterStatus.TIMED_OUT.value, now, resource_kind, resource,
                     LockWaiterStatus.QUEUED.value, now),
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise MutationLockError(
                    f"could not clean stale waiters (database busy): {exc}") from exc
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # FIFO fairness (ADR-023): the caller must be the HEAD of the
            # durable FIFO queue for this resource (oldest eligible waiter).
            # Terminal-task waiters are skipped via the JOIN; a newer waiter
            # can never overtake an older one.
            if waiter_id is not None:
                head = self._conn.execute(
                    "SELECT w.waiter_id FROM mutation_lock_waiters w "
                    "JOIN tasks t ON t.id = w.task_id "
                    "WHERE w.resource_kind=? AND w.resource=? AND w.status=? "
                    "AND w.deadline > ? AND t.status NOT IN (?, ?) "
                    "ORDER BY w.seq LIMIT 1",
                    (resource_kind, resource, LockWaiterStatus.QUEUED.value, now,
                     TaskStatus.COMPLETED.value, TaskStatus.FAILED.value),
                ).fetchone()
                if head is None or head[0] != waiter_id:
                    self._conn.rollback()
                    raise MutationLockError(
                        f"mutation lock queue: not this waiter's turn "
                        f"(waiter {waiter_id}) for {resource_kind!r} {resource!r}"
                    )
            # reclaim any expired row for this exact resource (atomic with insert)
            self._conn.execute(
                "DELETE FROM mutation_locks WHERE resource_kind=? AND resource=? AND expires_at <= ?",
                (resource_kind, resource, now),
            )
            self._conn.execute(
                "INSERT INTO mutation_locks "
                f"({', '.join(self._LOCK_COLS)}) VALUES ({', '.join('?' * len(self._LOCK_COLS))})",
                _lock_row(lock),
            )
            # the winning waiter leaves the queue in the SAME transaction:
            # acquire + dequeue is atomic, so a concurrent peek can never see
            # both a held lock AND a queued head for this waiter.
            if waiter_id is not None:
                self._conn.execute(
                    "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
                    "WHERE waiter_id=? AND status=?",
                    (LockWaiterStatus.ACQUIRED.value, now, waiter_id,
                     LockWaiterStatus.QUEUED.value),
                )
            self._conn.commit()
            return lock
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise MutationLockError(
                f"mutation resource is locked by another owner: {resource_kind!r} {resource!r}"
            ) from exc
        except sqlite3.OperationalError as exc:
            # e.g. 'database is locked' (another writer) - fail closed
            self._conn.rollback()
            raise MutationLockError(
                f"could not acquire mutation lock (database busy): {exc}"
            ) from exc
        except MutationLockError:
            self._conn.rollback()
            raise
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def renew(self, lock_id: str, owner_id: str, lease_seconds: float,
              now: str | None = None) -> "MutationLock":
        """Conditionally extend one live lock owned by the exact owner."""
        from arion.state.locks import (
            MutationLockError, _add_seconds, _parse_iso,
        )

        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT {', '.join(self._LOCK_COLS)} FROM mutation_locks "
                "WHERE lock_id=?", (lock_id,),
            ).fetchone()
            if row is None:
                raise MutationLockError(
                    f"mutation lock {lock_id} is missing; ownership lost"
                )
            lock = _lock_from_row(row)
            if lock.owner_id != owner_id:
                raise MutationLockError(
                    f"lock {lock_id} is owned by another owner; cannot renew"
                )
            if _parse_iso(now) < _parse_iso(lock.acquired_at):
                raise MutationLockError(
                    f"lock {lock_id} renewal time precedes acquisition"
                )
            if _parse_iso(now) >= _parse_iso(lock.expires_at):
                raise MutationLockError(
                    f"lock {lock_id} already expired; stale owner cannot renew"
                )
            candidate = _add_seconds(now, max(0.0, float(lease_seconds)))
            new_expiry = max(
                _parse_iso(lock.expires_at), _parse_iso(candidate)
            ).isoformat()
            self._conn.execute(
                "UPDATE mutation_locks SET expires_at=? "
                "WHERE lock_id=? AND owner_id=?",
                (new_expiry, lock_id, owner_id),
            )
            self._conn.commit()
            lock.expires_at = new_expiry
            return lock
        except MutationLockError:
            self._conn.rollback()
            raise
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(
                f"could not renew mutation lock (database busy): {exc}"
            ) from exc
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def release(self, lock_id: str, owner_id: str) -> bool:
        """Release a lock owned by `owner_id`. Returns True when this call
        removed the lock; False when it was already gone (idempotent for the
        owner). A non-owner cannot release an existing lock (typed error).

        Atomic across processes: the ownership check and the delete run in one
        BEGIN IMMEDIATE transaction."""
        from arion.state.locks import MutationLockError

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT {', '.join(self._LOCK_COLS)} FROM mutation_locks WHERE lock_id=?",
                (lock_id,),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False  # already released/reclaimed: idempotent
            if _lock_from_row(row).owner_id != owner_id:
                self._conn.rollback()
                raise MutationLockError(
                    f"lock {lock_id} is owned by another owner; cannot release"
                )
            self._conn.execute(
                "DELETE FROM mutation_locks WHERE lock_id=? AND owner_id=?", (lock_id, owner_id))
            self._conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(f"could not release mutation lock (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def get(self, lock_id: str) -> "MutationLock | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._LOCK_COLS)} FROM mutation_locks WHERE lock_id=?",
            (lock_id,),
        ).fetchone()
        return _lock_from_row(row) if row else None

    @_threadsafe
    def list(self, resource_kind: str | None = None,
             resource: str | None = None) -> list["MutationLock"]:
        cols = ", ".join(self._LOCK_COLS)
        clauses: list[str] = []
        params: list[Any] = []
        if resource_kind:
            clauses.append("resource_kind = ?")
            params.append(resource_kind)
        else:
            # Public mutation-lock enumeration excludes internal orchestration
            # lease namespaces. Explicit-kind queries remain available to the
            # owning engine and focused tests (ADR-045).
            placeholders = ",".join("?" * len(INTERNAL_LOCK_RESOURCE_KINDS))
            clauses.append(f"resource_kind NOT IN ({placeholders})")
            params.extend(sorted(INTERNAL_LOCK_RESOURCE_KINDS))
        if resource:
            clauses.append("resource = ?")
            params.append(resource)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {cols} FROM mutation_locks {where} ORDER BY acquired_at", params
        ).fetchall()
        return [_lock_from_row(r) for r in rows]

    @_threadsafe
    def reclaim_expired(self, now: str | None = None,
                        resource_kind: str | None = None,
                        resource: str | None = None) -> list[str]:
        """Atomically reclaim (delete) expired locks. Returns the reclaimed
        lock ids. Active locks are never touched. Cross-process safe."""
        if now is None:
            now = utcnow()
        clauses = ["expires_at <= ?"]
        params: list[Any] = [now]
        if resource_kind:
            clauses.append("resource_kind = ?")
            params.append(resource_kind)
        else:
            placeholders = ",".join("?" * len(INTERNAL_LOCK_RESOURCE_KINDS))
            clauses.append(f"resource_kind NOT IN ({placeholders})")
            params.extend(sorted(INTERNAL_LOCK_RESOURCE_KINDS))
        if resource:
            clauses.append("resource = ?")
            params.append(resource)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                f"SELECT lock_id FROM mutation_locks WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._conn.execute(
                    f"DELETE FROM mutation_locks WHERE {' AND '.join(clauses)}", params
                )
            self._conn.commit()
            return ids
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(f"could not reclaim expired locks (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise

    # ---- durable FIFO wait queue (ADR-023) ----

    _WAITER_COLS = (
        "waiter_id", "resource_kind", "resource", "task_id", "goal_id",
        "step_index", "seq", "enqueued_at", "deadline", "attempts",
        "next_retry", "status", "created_at", "updated_at",
    )

    @_threadsafe
    def enqueue_waiter(self, resource_kind: str, resource: str, task_id: str,
                       goal_id: str | None, step_index: int, deadline: str,
                       now: str | None = None) -> "LockWaiter":
        """Atomically enqueue a FIFO waiter for a canonical resource.

        Existing QUEUED membership for the same resource/task/step is adopted
        unchanged (position/deadline preserved across a row-before-checkpoint
        crash). Otherwise seq is allocated as 1 + MAX(seq) under the same
        BEGIN IMMEDIATE transaction.
        """
        from arion.state.locks import LockWaiter, LockWaiterStatus

        if now is None:
            now = utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                f"SELECT {', '.join(self._WAITER_COLS)} "
                "FROM mutation_lock_waiters WHERE resource_kind=? "
                "AND resource=? AND task_id=? AND step_index=? AND status=? "
                "ORDER BY seq LIMIT 1",
                (resource_kind, resource, task_id, step_index,
                 LockWaiterStatus.QUEUED.value),
            ).fetchone()
            if existing is not None:
                self._conn.commit()
                return _waiter_from_row(existing)
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM mutation_lock_waiters "
                "WHERE resource_kind=? AND resource=?",
                (resource_kind, resource),
            ).fetchone()
            seq = int(row[0])
            waiter = LockWaiter(
                waiter_id=new_id("waiter"),
                resource_kind=resource_kind,
                resource=resource,
                task_id=task_id,
                goal_id=goal_id,
                step_index=step_index,
                seq=seq,
                enqueued_at=now,
                deadline=deadline,
                status=LockWaiterStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
            self._conn.execute(
                "INSERT INTO mutation_lock_waiters "
                f"({', '.join(self._WAITER_COLS)}) VALUES ({', '.join('?' * len(self._WAITER_COLS))})",
                _waiter_row(waiter),
            )
            self._conn.commit()
            return waiter
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(f"could not enqueue mutation lock waiter (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def get_waiter(self, waiter_id: str) -> "LockWaiter | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._WAITER_COLS)} FROM mutation_lock_waiters WHERE waiter_id=?",
            (waiter_id,),
        ).fetchone()
        return _waiter_from_row(row) if row else None

    @_threadsafe
    def peek_waiter(self, resource_kind: str, resource: str,
                    now: str | None = None) -> "LockWaiter | None":
        """The oldest ELIGIBLE waiter for a resource (FIFO head).

        Eligible = status queued AND deadline not passed AND task not
        terminal. Recomputes on every call, so removing/expiring the head
        automatically promotes the next eligible waiter - positions of the
        remaining waiters are never rewritten.
        """
        if now is None:
            now = utcnow()
        row = self._conn.execute(
            "SELECT w.* FROM mutation_lock_waiters w "
            "JOIN tasks t ON t.id = w.task_id "
            "WHERE w.resource_kind=? AND w.resource=? AND w.status=? "
            "AND w.deadline > ? AND t.status NOT IN (?, ?) "
            "ORDER BY w.seq LIMIT 1",
            (resource_kind, resource, LockWaiterStatus.QUEUED.value, now,
             TaskStatus.COMPLETED.value, TaskStatus.FAILED.value),
        ).fetchone()
        return _waiter_from_row(row) if row else None

    @_threadsafe
    def update_waiter(self, waiter_id: str, attempts: int | None = None,
                      next_retry: str | None = None) -> None:
        sets, params = [], []
        if attempts is not None:
            sets.append("attempts = ?")
            params.append(int(attempts))
        if next_retry is not None:
            sets.append("next_retry = ?")
            params.append(next_retry)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utcnow())
        params.append(waiter_id)
        self._conn.execute(
            f"UPDATE mutation_lock_waiters SET {', '.join(sets)} WHERE waiter_id=?", params)
        self._conn.commit()

    @_threadsafe
    def dequeue_waiter(self, waiter_id: str, status: str = "acquired") -> bool:
        """Transition a queued waiter to a terminal status (acquired |
        timed_out | cancelled). Idempotent: a non-queued or unknown waiter
        returns False. The row is kept for audit (append-safe)."""
        from arion.state.locks import LockWaiterStatus

        status = LockWaiterStatus(status)
        cur = self._conn.execute(
            "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
            "WHERE waiter_id=? AND status=?",
            (status.value, utcnow(), waiter_id, LockWaiterStatus.QUEUED.value),
        )
        self._conn.commit()
        return cur.rowcount > 0

    @_threadsafe
    def cancel_waiter_for_task(self, task_id: str, status: str = "cancelled") -> int:
        """Cancel every queued waiter of a task (task became terminal)."""
        from arion.state.locks import LockWaiterStatus

        status = LockWaiterStatus(status)
        cur = self._conn.execute(
            "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
            "WHERE task_id=? AND status=?",
            (status.value, utcnow(), task_id, LockWaiterStatus.QUEUED.value),
        )
        self._conn.commit()
        return cur.rowcount

    @_threadsafe
    def reclaim_stale_waiters(self, now: str | None = None) -> list[str]:
        """Atomically mark expired queued waiters as timed_out. Idempotent:
        already-terminal waiters are never touched again."""
        if now is None:
            now = utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT waiter_id FROM mutation_lock_waiters "
                "WHERE status=? AND deadline <= ?",
                (LockWaiterStatus.QUEUED.value, now),
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._conn.execute(
                    "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
                    "WHERE status=? AND deadline <= ?",
                    (LockWaiterStatus.TIMED_OUT.value, now,
                     LockWaiterStatus.QUEUED.value, now),
                )
            self._conn.commit()
            return ids
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(f"could not reclaim stale waiters (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def list_waiters(self, resource_kind: str | None = None,
                     resource: str | None = None,
                     status: str | None = None) -> list["LockWaiter"]:
        cols = ", ".join(self._WAITER_COLS)
        clauses: list[str] = []
        params: list[Any] = []
        if resource_kind:
            clauses.append("resource_kind = ?")
            params.append(resource_kind)
        if resource:
            clauses.append("resource = ?")
            params.append(resource)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {cols} FROM mutation_lock_waiters {where} ORDER BY seq", params
        ).fetchall()
        return [_waiter_from_row(r) for r in rows]

    @_threadsafe
    def release_and_select_next(self, lock_id: str, owner_id: str,
                                now: str | None = None) -> tuple[bool, "LockWaiter | None"]:
        """Atomically release a lock AND select the next eligible FIFO waiter.

        Runs in ONE BEGIN IMMEDIATE transaction: the ownership check, the
        lock deletion, expired-waiter cleanup, and the next-head selection.
        Returns (released, next_head). A non-owner gets a typed error; an
        already-gone lock returns (False, None). The selected waiter is
        exactly what peek_waiter would return immediately after - no
        check-then-act window.
        """
        from arion.state.locks import MutationLockError

        if now is None:
            now = utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT {', '.join(self._LOCK_COLS)} FROM mutation_locks WHERE lock_id=?",
                (lock_id,),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False, None
            lock = _lock_from_row(row)
            if lock.owner_id != owner_id:
                self._conn.rollback()
                raise MutationLockError(
                    f"lock {lock_id} is owned by another owner; cannot release"
                )
            self._conn.execute(
                "DELETE FROM mutation_locks WHERE lock_id=? AND owner_id=?", (lock_id, owner_id))
            # mark expired waiters for the released resource (same transaction)
            self._conn.execute(
                "UPDATE mutation_lock_waiters SET status=?, updated_at=? "
                "WHERE resource_kind=? AND resource=? AND status=? AND deadline <= ?",
                (LockWaiterStatus.TIMED_OUT.value, now, lock.resource_kind,
                 lock.resource, LockWaiterStatus.QUEUED.value, now),
            )
            head = self._conn.execute(
                "SELECT w.* FROM mutation_lock_waiters w "
                "JOIN tasks t ON t.id = w.task_id "
                "WHERE w.resource_kind=? AND w.resource=? AND w.status=? "
                "AND w.deadline > ? AND t.status NOT IN (?, ?) "
                "ORDER BY w.seq LIMIT 1",
                (lock.resource_kind, lock.resource, LockWaiterStatus.QUEUED.value,
                 now, TaskStatus.COMPLETED.value, TaskStatus.FAILED.value),
            ).fetchone()
            self._conn.commit()
            return True, (_waiter_from_row(head) if head else None)
        except sqlite3.OperationalError as exc:
            self._conn.rollback()
            raise MutationLockError(f"could not release mutation lock (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise
            raise MutationLockError(f"could not reclaim expired locks (database busy): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def close(self) -> None:
        self._conn.close()

    # ---- durable scheduler/work registry (ADR-025) ----

    _SYS_COLS = [
        "work_id", "task_id", "goal_id", "step_index", "scheduler_id",
        "worker_id", "status", "attempts", "error", "created_at",
        "started_at", "completed_at", "lease_expires_at",
    ]

    def _sys_assert_transition(self, work_id: str, target: SchedulerWorkStatus,
                               actual: SchedulerWorkStatus) -> None:
        """Fail closed: illegal or unknown transitions raise a typed error
        carrying the ACTUAL durable state (never silently ignored)."""
        if not legal_transition(actual, target):
            raise SchedulerStateError(
                f"invalid scheduler state transition {actual.value} -> "
                f"{target.value} for {work_id} (fail closed)"
            )

    @_threadsafe
    def create(self, *, task_id: str, goal_id: str | None, step_index: int,
               scheduler_id: str, now: str | None = None) -> SchedulerWork:
        if not task_id or not scheduler_id or step_index < 0:
            raise SchedulerRegistryError(
                "invalid scheduler work metadata (task_id/scheduler_id required, "
                "step_index >= 0) - fail closed")
        created = now or utcnow()
        work = SchedulerWork(
            work_id=new_id("sw"), task_id=task_id, goal_id=goal_id,
            step_index=step_index, scheduler_id=scheduler_id,
            status=SchedulerWorkStatus.QUEUED, created_at=created,
        )
        self._conn.execute(
            "INSERT INTO scheduler_work (work_id, task_id, goal_id, step_index, "
            "scheduler_id, worker_id, status, attempts, error, created_at, "
            "started_at, completed_at, lease_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (work.work_id, work.task_id, work.goal_id, work.step_index,
             work.scheduler_id, work.worker_id, work.status.value, work.attempts,
             work.error, work.created_at, work.started_at, work.completed_at,
             work.lease_expires_at),
        )
        # ADR-028: queue admission event commits ATOMICALLY with the row.
        self._sech_insert_in_tx(_audit_event(
            kind="work.queued", ts=created,
            detail={"scheduler_id": scheduler_id, "goal_id": goal_id,
                    "task_id": task_id, "work_id": work.work_id,
                    "step_index": step_index, "outcome": "queued",
                    "ts": created}))
        self._conn.commit()
        return work

    @_threadsafe
    def mark_running(self, work_id: str, worker_id: str, lease_seconds: float,
                     now: str | None = None,
                     max_lease_seconds: float | None = None) -> SchedulerWork:
        now = now or utcnow()
        lease = float(lease_seconds)
        if max_lease_seconds is not None:
            # ADR-026: a forged enormous lease is capped (bounded ownership)
            lease = min(lease, max(0.0, float(max_lease_seconds)))
        cur = self._conn.execute(
            "UPDATE scheduler_work SET status=?, worker_id=?, started_at=?, "
            "lease_expires_at=? WHERE work_id=? AND status=?",
            (SchedulerWorkStatus.RUNNING.value, worker_id, now,
             _iso_plus(now, lease), work_id, SchedulerWorkStatus.QUEUED.value),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT work_id, status FROM scheduler_work WHERE work_id=?",
                (work_id,)).fetchone()
            if row is None:
                raise SchedulerStateError(f"unknown scheduler work id {work_id} (fail closed)")
            self._sys_assert_transition(
                work_id, SchedulerWorkStatus.RUNNING,
                SchedulerWorkStatus(row[1]))
        return self._sys_row(work_id)

    @_threadsafe
    def mark_terminal(self, work_id: str, status: SchedulerWorkStatus,
                      error: str | None = None, now: str | None = None,
                      owner_worker_id: str | None = None) -> SchedulerWork:
        """RUNNING -> COMPLETED/FAILED REQUIRES the current owner (ADR-026):
        a stale owner can never complete or fail work after its lease
        expired or the row was reassigned."""
        if status in (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED):
            if owner_worker_id is None:
                raise SchedulerStateError(
                    f"{status.value} requires owner_worker_id for {work_id} "
                    f"(fail closed)")
            now_value = now or utcnow()
            cur = self._conn.execute(
                "UPDATE scheduler_work SET status=?, error=?, completed_at=?, "
                "lease_expires_at=NULL WHERE work_id=? AND status=? "
                "AND worker_id=? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at > ?",
                (status.value, (error or "")[:500], now_value,
                 work_id, SchedulerWorkStatus.RUNNING.value,
                 owner_worker_id, now_value),
            )
            if cur.rowcount > 0:
                self._sech_insert_in_tx(_audit_event(
                    kind=("work.completed" if status == SchedulerWorkStatus.COMPLETED
                          else "work.failed"),
                    ts=now_value,
                    detail={"work_id": work_id, "worker_id": owner_worker_id,
                            "outcome": status.value,
                            "reason": (error or "")[:200], "ts": now_value}))
            self._conn.commit()
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT work_id, status, worker_id, lease_expires_at "
                    "FROM scheduler_work WHERE work_id=?",
                    (work_id,)).fetchone()
                if row is None:
                    raise SchedulerStateError(
                        f"unknown scheduler work id {work_id} (fail closed)")
                actual = SchedulerWorkStatus(row[1])
                if actual == SchedulerWorkStatus.RUNNING:
                    if row[2] != owner_worker_id:
                        raise SchedulerStateError(
                            f"stale owner: {work_id} is owned by {row[2]}, "
                            f"not {owner_worker_id} (fail closed)")
                    raise SchedulerStateError(
                        f"stale owner: work {work_id} lease expired "
                        f"{row[3]} before {now_value} (fail closed)"
                    )
                self._sys_assert_transition(work_id, status, actual)
            return self._sys_row(work_id)
        now_value = now or utcnow()
        sources = self._sys_terminal_sources(status)
        if status == SchedulerWorkStatus.ABANDONED:
            observed = self.get_work(work_id)
            if observed is None:
                raise SchedulerStateError(
                    f"unknown scheduler work id {work_id} (fail closed)"
                )
            if observed.status == SchedulerWorkStatus.RUNNING:
                # The observation only selects the API. reclaim_work repeats
                # status + expiry under BEGIN IMMEDIATE and emits telemetry in
                # the same commit, so a concurrent renewal still wins.
                return self.reclaim_work(work_id, now=now_value)
            # QUEUED abandonment is administrative pre-execution cleanup.
            cur = self._conn.execute(
                "UPDATE scheduler_work SET status=?, error=?, completed_at=?, "
                "lease_expires_at=NULL WHERE work_id=? AND status=?",
                (status.value, (error or "")[:500], now_value, work_id,
                 SchedulerWorkStatus.QUEUED.value),
            )
        else:
            cur = self._conn.execute(
                "UPDATE scheduler_work SET status=?, error=?, completed_at=?, "
                "lease_expires_at=NULL WHERE work_id=? AND status IN (%s)"
                % ",".join("?" * len(sources)),
                (status.value, (error or "")[:500], now_value,
                 work_id, *sources),
            )
        self._conn.commit()
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT work_id, status, lease_expires_at "
                "FROM scheduler_work WHERE work_id=?",
                (work_id,)).fetchone()
            if row is None:
                raise SchedulerStateError(f"unknown scheduler work id {work_id} (fail closed)")
            actual = SchedulerWorkStatus(row[1])
            if (status == SchedulerWorkStatus.ABANDONED
                    and actual == SchedulerWorkStatus.RUNNING):
                raise SchedulerStateError(
                    f"work {work_id} lease is still valid "
                    f"(expires {row[2]}); not reclaimed (fail closed)"
                )
            self._sys_assert_transition(work_id, status, actual)
        return self._sys_row(work_id)

    @staticmethod
    def _sys_terminal_sources(status: SchedulerWorkStatus) -> list[str]:
        """Source states a terminal transition may come from (mirrors
        legal_transition): CANCELLED only from QUEUED; ABANDONED from QUEUED
        or RUNNING; COMPLETED/FAILED only from RUNNING."""
        if status == SchedulerWorkStatus.CANCELLED:
            return [SchedulerWorkStatus.QUEUED.value]
        if status == SchedulerWorkStatus.ABANDONED:
            return [SchedulerWorkStatus.QUEUED.value, SchedulerWorkStatus.RUNNING.value]
        if status in (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED):
            return [SchedulerWorkStatus.RUNNING.value]
        raise SchedulerStateError(f"not a terminal status: {status.value}")

    @_threadsafe
    def get_work(self, work_id: str) -> SchedulerWork | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(self._SYS_COLS) + " FROM scheduler_work WHERE work_id=?",
            (work_id,)).fetchone()
        return _sys_work_from_row(row) if row else None

    @_threadsafe
    def list_work(self, status: SchedulerWorkStatus | None = None,
             scheduler_id: str | None = None,
             task_id: str | None = None,
             goal_id: str | None = None,
             step_index: int | None = None) -> list[SchedulerWork]:
        sql = "SELECT " + ", ".join(self._SYS_COLS) + " FROM scheduler_work"
        where, params = [], []
        if status is not None:
            where.append("status=?")
            params.append(status.value)
        if scheduler_id is not None:
            where.append("scheduler_id=?")
            params.append(scheduler_id)
        if task_id is not None:
            where.append("task_id=?")
            params.append(task_id)
        if goal_id is not None:
            where.append("goal_id=?")
            params.append(goal_id)
        if step_index is not None:
            where.append("step_index=?")
            params.append(step_index)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, work_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [_sys_work_from_row(r) for r in rows]

    @_threadsafe
    def reclaim_work(self, work_id: str,
                     now: str | None = None) -> SchedulerWork:
        """Atomically reclaim one expired RUNNING work lease (ADR-042)."""
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT work_id, goal_id, worker_id, scheduler_id, status, "
                "lease_expires_at FROM scheduler_work WHERE work_id=?",
                (work_id,),
            ).fetchone()
            if row is None:
                raise SchedulerStateError(
                    f"unknown scheduler work id {work_id} (fail closed)"
                )
            status = SchedulerWorkStatus(row[4])
            if status != SchedulerWorkStatus.RUNNING:
                raise SchedulerStateError(
                    f"work {work_id} is {status.value} "
                    f"(only RUNNING rows can be reclaimed)"
                )
            expiry = row[5]
            if expiry is None or expiry > now:
                raise SchedulerStateError(
                    f"work {work_id} lease is still valid "
                    f"(expires {expiry}); not reclaimed"
                )
            cursor = self._conn.execute(
                "UPDATE scheduler_work SET status=?, completed_at=?, "
                "lease_expires_at=NULL WHERE work_id=? AND status=? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                (SchedulerWorkStatus.ABANDONED.value, now, work_id,
                 SchedulerWorkStatus.RUNNING.value, now),
            )
            if cursor.rowcount != 1:
                raise SchedulerStateError(
                    f"work {work_id} changed during reclaim (fail closed)"
                )
            self._sech_insert_in_tx(_audit_event(
                kind="work.reclaimed", ts=now,
                detail={"work_id": row[0], "goal_id": row[1],
                        "worker_id": row[2], "scheduler_id": row[3],
                        "lease_expires_at": expiry,
                        "reason": "lease_expired",
                        "outcome": "reclaimed", "ts": now}))
            self._conn.commit()
            return self._sys_row(work_id)
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def reclaim_stale(self, now: str | None = None) -> list[str]:
        """Expired RUNNING leases -> ABANDONED. Idempotent: a terminal or
        still-valid row is never touched. Returns reclaimed work ids.
        Emits an atomic `work.reclaimed` event per row (same transaction)."""
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT work_id, goal_id, worker_id, scheduler_id FROM scheduler_work "
                "WHERE status=? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ?",
                (SchedulerWorkStatus.RUNNING.value, now)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._conn.execute(
                    "UPDATE scheduler_work SET status=?, completed_at=?, "
                    "lease_expires_at=NULL "
                    "WHERE work_id IN (%s) AND status=?"
                    % ",".join("?" * len(ids)),
                    [SchedulerWorkStatus.ABANDONED.value, now, *ids,
                     SchedulerWorkStatus.RUNNING.value],
                )
                for wid, gid, worker, sid in rows:
                    self._sech_insert_in_tx(_audit_event(
                        kind="work.reclaimed", ts=now,
                        detail={"work_id": wid, "goal_id": gid,
                                "worker_id": worker, "scheduler_id": sid,
                                "reason": "lease_expired",
                                "outcome": "reclaimed", "ts": now}))
            self._conn.commit()
            return ids
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def abandon_foreign_queued(self, scheduler_id: str,
                               now: str | None = None) -> int:
        """QUEUED rows whose scheduler has NO LIVE registration (presumed
        dead process) -> ABANDONED (ADR-026). A live peer's queue is never
        touched: liveness is the registration lease, so two processes can
        share one registry without abandoning each other's work. This
        engine's own QUEUED rows are always untouched. Idempotent."""
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT work_id, goal_id, scheduler_id FROM scheduler_work "
                "WHERE status=? AND scheduler_id<>? AND scheduler_id NOT IN "
                "(SELECT scheduler_id FROM scheduler_instances "
                " WHERE lease_expires_at > ?)",
                (SchedulerWorkStatus.QUEUED.value, scheduler_id, now)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._conn.execute(
                    "UPDATE scheduler_work SET status=?, completed_at=?, "
                    "lease_expires_at=NULL "
                    "WHERE status=? AND scheduler_id<>? AND scheduler_id NOT IN "
                    "(SELECT scheduler_id FROM scheduler_instances "
                    " WHERE lease_expires_at > ?)",
                    (SchedulerWorkStatus.ABANDONED.value, now,
                     SchedulerWorkStatus.QUEUED.value, scheduler_id, now),
                )
                for wid, gid, sid in rows:
                    self._sech_insert_in_tx(_audit_event(
                        kind="scheduler.abandoned", ts=now,
                        detail={"work_id": wid, "goal_id": gid,
                                "scheduler_id": sid,
                                "reason": "dead_registration",
                                "outcome": "abandoned", "ts": now}))
            self._conn.commit()
            return len(ids)
        except Exception:
            self._conn.rollback()
            raise

    def _sys_row(self, work_id: str) -> SchedulerWork:
        work = self.get_work(work_id)
        if work is None:
            raise SchedulerStateError(f"unknown scheduler work id {work_id} (fail closed)")
        return work

    # ------------------------------------------------------------------ #
    # ADR-026: scheduler registration + ownership leases
    # ------------------------------------------------------------------ #

    @_threadsafe
    def register_scheduler(self, scheduler_id: str, pid: int,
                           lease_seconds: float, now: str | None = None) -> None:
        """Durable scheduler registration (unique id, process-lifetime)."""
        now = now or utcnow()
        if not scheduler_id:
            raise SchedulerRegistryError("scheduler_id required (fail closed)")
        self._conn.execute(
            "INSERT OR REPLACE INTO scheduler_instances "
            "(scheduler_id, pid, registered_at, heartbeat_at, lease_expires_at) "
            "VALUES (?,?,?,?,?)",
            (scheduler_id, int(pid), now, now,
             _iso_plus(now, max(0.0, float(lease_seconds)))),
        )
        self._sech_insert_in_tx(_audit_event(
            kind="scheduler.registered", ts=now,
            detail={"scheduler_id": scheduler_id, "pid": int(pid),
                    "lease_expires_at": _iso_plus(now, max(0.0, float(lease_seconds))),
                    "outcome": "registered", "ts": now}))
        self._conn.commit()

    @_threadsafe
    def heartbeat_scheduler(self, scheduler_id: str, lease_seconds: float,
                            now: str | None = None,
                            max_lease_seconds: float | None = None) -> bool:
        """Extend a live scheduler registration lease.

        Each renewal is owner-time checked, monotonic, and bounded to at most
        ``max_lease_seconds`` beyond ``now``.  The bound is sliding: a live
        process may keep renewing indefinitely, while a lapsed registration
        cannot be resurrected (ADR-040).
        """
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT registered_at, lease_expires_at FROM scheduler_instances "
                "WHERE scheduler_id=?", (scheduler_id,)).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            registered_at, expiry = row
            if now < registered_at:  # forged/past heartbeat: no extension
                self._conn.rollback()
                return False
            if expiry is not None and expiry <= now:
                # the registration lease already lapsed at the caller's
                # claimed time: stale owner / forged future heartbeat
                self._conn.rollback()
                return False
            max_lease = (float(max_lease_seconds) if max_lease_seconds is not None
                         else float(lease_seconds))
            extension = min(
                max(0.0, float(lease_seconds)), max(0.0, max_lease)
            )
            new_expiry = _iso_plus(now, extension)
            if expiry is not None and new_expiry < expiry:
                new_expiry = expiry  # monotonic: never shrink
            self._conn.execute(
                "UPDATE scheduler_instances SET heartbeat_at=?, lease_expires_at=? "
                "WHERE scheduler_id=?",
                (now, new_expiry, scheduler_id))
            self._sech_insert_in_tx(_audit_event(
                kind="scheduler.heartbeat", ts=now,
                detail={"scheduler_id": scheduler_id,
                        "lease_expires_at": new_expiry,
                        "outcome": "heartbeat", "ts": now}))
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def unregister_scheduler(self, scheduler_id: str) -> None:
        """Remove a scheduler registration (clean shutdown). Idempotent."""
        self._conn.execute("DELETE FROM scheduler_instances WHERE scheduler_id=?",
                           (scheduler_id,))
        self._sech_insert_in_tx(_audit_event(
            kind="scheduler.shutdown", ts=utcnow(),
            detail={"scheduler_id": scheduler_id, "outcome": "shutdown",
                    "ts": utcnow()}))
        self._conn.commit()

    @_threadsafe
    def scheduler_registration_live(self, scheduler_id: str,
                                    now: str | None = None) -> bool:
        now = now or utcnow()
        row = self._conn.execute(
            "SELECT lease_expires_at FROM scheduler_instances WHERE scheduler_id=?",
            (scheduler_id,)).fetchone()
        return row is not None and row[0] is not None and row[0] > now

    @_threadsafe
    def set_scheduler_global_max(self, n: int) -> None:
        """Configure the durable cross-process capacity (>= 1). ADR-029:
        the cap may never drop below the sum of ENABLED goal reservations
        (an impossible guarantee is rejected, never silently accepted)."""
        n = int(n)
        if n < 1:
            raise SchedulerRegistryError(
                "global max concurrency must be >= 1 (fail closed)")
        reserved = self._conn.execute(
            "SELECT COALESCE(SUM(reservation), 0) FROM "
            "scheduler_goal_reservations WHERE enabled=1").fetchone()[0]
        if n < int(reserved):
            raise SchedulerRegistryError(
                f"global max {n} is below the enabled reservation total "
                f"{reserved} (ADR-029 oversubscription, fail closed)")
        self._conn.execute(
            "INSERT OR REPLACE INTO scheduler_config (key, value) VALUES (?, ?)",
            ("global_max_concurrency", str(n)))
        self._sech_insert_in_tx(_audit_event(
            kind="scheduler.config_changed", ts=utcnow(),
            detail={"scheduler_id": None, "config": "global_max_concurrency",
                    "reason": str(n), "outcome": "set", "ts": utcnow()}))
        self._conn.commit()

    @_threadsafe
    def get_scheduler_global_max(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM scheduler_config WHERE key=?",
            ("global_max_concurrency",)).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    # ---- internal claim helpers (caller holds an open transaction) ----

    def _sys_reclaim_stale_in_tx(self, now: str) -> None:
        """Reclaim expired RUNNING rows inside an open transaction (lazy
        crash recovery: a dead process never permanently consumes
        capacity)."""
        self._conn.execute(
            "UPDATE scheduler_work SET status=?, completed_at=?, "
            "lease_expires_at=NULL WHERE status=? AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at <= ?",
            (SchedulerWorkStatus.ABANDONED.value, now,
             SchedulerWorkStatus.RUNNING.value, now))

    def _sys_capacity_ok_in_tx(self,
                               claiming_scheduler_id: str | None = None,
                               ) -> tuple[bool, str | None]:
        """Cross-process capacity (ADR-026/028): when global_max_concurrency
        is configured, grant only below the cap (counts live RUNNING rows
        across ALL schedulers) AND below the fair share for the claiming
        scheduler. Returns (ok, reason) where reason is None on grant,
        "capacity" when the global cap is the binding constraint, or
        "scheduler_share" when the scheduler fair share is.

        Fair share: with `active` schedulers holding queued or running work,
        no single scheduler may hold more than ceil(global_max / active)
        RUNNING rows, so a peer process's step gets the next free slot
        instead of being starved by a hot claimer. With one active scheduler
        the full cap is available (ADR-025 behavior preserved)."""
        global_max = self.get_scheduler_global_max()
        if global_max is None:
            return True, None
        count = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_work WHERE status=?",
            (SchedulerWorkStatus.RUNNING.value,)).fetchone()[0]
        if count >= global_max:
            return False, "capacity"
        if claiming_scheduler_id is None:
            return True, None
        active = self._conn.execute(
            "SELECT COUNT(DISTINCT scheduler_id) FROM scheduler_work "
            "WHERE status IN (?, ?)",
            (SchedulerWorkStatus.QUEUED.value,
             SchedulerWorkStatus.RUNNING.value)).fetchone()[0]
        if active <= 1:
            return True, None
        share = max(1, -(-global_max // active))  # ceil(global_max / active)
        mine = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_work WHERE status=? AND scheduler_id=?",
            (SchedulerWorkStatus.RUNNING.value, claiming_scheduler_id)).fetchone()[0]
        if mine >= share:
            return False, "scheduler_share"
        return True, None

    def _sys_goal_admission_in_tx(self, goal_id: str) -> tuple[bool, dict]:
        """DWRR goal-weight gate (ADR-027). Caller holds BEGIN IMMEDIATE.

        Returns (granted, telemetry) where telemetry carries bounded
        observability info (weight, refill, credit before/after). The gate
        itself is authoritative; telemetry is observational ONLY.

        - contending goals = goals with QUEUED or RUNNING rows (an idle
          goal never reserves capacity);
        - every contending goal keeps a durable bounded deficit; when the
          attempting goal's deficit is below 1 AND no other contending
          goal holds credit, ALL contending enabled goals are refilled by
          their weight (deficit capped at max(weight, 2 * global_max));
        - a claim is granted iff the goal's deficit >= 1 (debited 1);
        - disabled goals are never admitted (fail closed);
        - no global_max configured => the gate is a no-op (ADR-026
          behavior exactly); unconfigured goals use the default weight 1.

        The entire decision derives from durable rows, so a restart
        reconstructs exactly the same policy (no in-memory counter).
        """
        telemetry: dict = {}
        global_max = self.get_scheduler_global_max()
        if global_max is None:
            return True, telemetry  # no cross-process policy scope
        cfg = self.get_goal_weight_config(goal_id)
        if cfg is not None and not cfg["enabled"]:
            return False, {"weight": int(cfg["weight"]), "disabled": True}
        weight = int(cfg["weight"]) if cfg is not None else 1
        telemetry["weight"] = weight
        contending = [
            r[0] for r in self._conn.execute(
                "SELECT DISTINCT goal_id FROM scheduler_work WHERE status IN (?, ?)",
                (SchedulerWorkStatus.QUEUED.value,
                 SchedulerWorkStatus.RUNNING.value)).fetchall()]
        if goal_id not in contending:
            return False, telemetry  # defensive: no work for this goal
        cap = max(1, int(global_max))
        row = self._conn.execute(
            "SELECT deficit FROM scheduler_goal_state WHERE goal_id=?",
            (goal_id,)).fetchone()
        # a forged/inflated durable deficit is clamped at spend time to
        # max(weight, 2 * cap): it can delay peers by at most a bounded
        # number of admissions and can never exceed cap/share gates
        deficit = min(int(row[0]) if row else 0,
                      max(weight, 2 * cap))
        telemetry["credit_before"] = deficit
        if deficit < 1:
            # A refill round starts ONLY when NO contending enabled goal
            # still holds credit (a true round boundary): a hot goal cannot
            # refill itself by repeatedly attempting. Every contending goal
            # then receives +weight credit (bounded).
            others_have_credit = False
            for g in contending:
                gcfg = self.get_goal_weight_config(g)
                if gcfg is not None and not gcfg["enabled"]:
                    continue
                # ADR-031: a goal AT its enabled ceiling cannot spend
                # credit, so it must not block refill rounds for peers
                # (no stranded-credit behavior).
                ccfg = self.get_goal_ceiling_config(g)
                if ccfg is not None and ccfg["enabled"]:
                    c = int(ccfg["ceiling"])
                    rg = self._conn.execute(
                        "SELECT COUNT(*) FROM scheduler_work WHERE status=? "
                        "AND goal_id=?",
                        (SchedulerWorkStatus.RUNNING.value, g)).fetchone()[0]
                    if int(rg) >= c:
                        continue
                r2 = self._conn.execute(
                    "SELECT deficit FROM scheduler_goal_state WHERE goal_id=?",
                    (g,)).fetchone()
                if r2 is not None and int(r2[0]) >= 1:
                    others_have_credit = True
                    break
            if others_have_credit:
                telemetry["refill"] = False
                telemetry["credit_after"] = deficit
                return False, telemetry  # credit spent; wait for its round
            now = utcnow()
            for g in contending:
                gcfg = self.get_goal_weight_config(g)
                if gcfg is not None and not gcfg["enabled"]:
                    continue
                gw = int(gcfg["weight"]) if gcfg is not None else 1
                r2 = self._conn.execute(
                    "SELECT deficit FROM scheduler_goal_state WHERE goal_id=?",
                    (g,)).fetchone()
                d = int(r2[0]) if r2 else 0
                nd = min(d + gw, max(gw, 2 * cap))
                if r2 is None:
                    self._conn.execute(
                        "INSERT INTO scheduler_goal_state "
                        "(goal_id, deficit, updated_at) VALUES (?,?,?)",
                        (g, nd, now))
                else:
                    self._conn.execute(
                        "UPDATE scheduler_goal_state SET deficit=?, updated_at=? "
                        "WHERE goal_id=?",
                        (nd, now, g))
            deficit = min(deficit + weight, max(weight, 2 * cap))
            telemetry["refill"] = True
        if deficit >= 1:
            self._conn.execute(
                "UPDATE scheduler_goal_state SET deficit=?, updated_at=? "
                "WHERE goal_id=?",
                (deficit - 1, utcnow(), goal_id))
            telemetry["credit_after"] = deficit - 1
            return True, telemetry
        telemetry["credit_after"] = deficit
        return False, telemetry

    def _sech_claim_denied_event(self, reason: str, work_id: str,
                                 goal_id: str | None, scheduler_id: str | None,
                                 worker_id: str, now: str,
                                 extra: dict | None = None) -> None:
        """Emit an atomic `work.claim_denied` (or specific) event inside the
        open transaction. OBSERVATIONAL ONLY."""
        kind = "work.claim_denied"
        if reason == "capacity":
            kind = "capacity.denied"
        elif reason == "scheduler_share":
            kind = "scheduler_share.denied"
        elif reason == "goal_weight":
            kind = "goal_weight.denied"
        elif reason == "reservation":
            kind = "reservation.denied"
        d = {"scheduler_id": scheduler_id, "worker_id": worker_id,
             "goal_id": goal_id, "work_id": work_id,
             "reason": reason, "outcome": "denied",
             "ts": now}
        if extra:
            d.update(extra)
        self._sech_insert_in_tx(_audit_event(kind=kind, ts=now, detail=d))

    def _sys_reservation_gate_in_tx(self, goal_id: str) -> tuple[str, dict]:
        """ADR-029 reservation gate. Caller holds BEGIN IMMEDIATE and has
        already passed the global-cap and scheduler fair-share gates.
        Returns (decision, telemetry) where decision is one of:

        - "floor": the claiming goal is BELOW its reservation floor with
          runnable work - grant the claim WITHOUT consulting the DWRR
          gate (the floor is a guarantee, not an opportunity). The goal
          is still subject to gates 1-3 (cap, fair share) and to the
          ADR-027 weight-disabled hard gate (checked by the caller).
        - "deny": this claim would consume a free slot that a runnable
          reserved goal still needs to reach its floor - deny with
          reservation.denied; the row stays QUEUED.
        - "pass": no reservation constraint applies; proceed to DWRR.

        With no global cap configured there is no capacity scope, so the
        gate is a no-op (exactly like the DWRR gate, ADR-026 behavior).
        Idle reserved goals (no QUEUED work) reserve nothing.
        """
        tele: dict = {}
        global_max = self.get_scheduler_global_max()
        if global_max is None:
            return "pass", tele
        cfg = self.get_goal_reservation_config(goal_id)
        reservation = int(cfg["reservation"]) if cfg is not None else 0
        enabled = bool(cfg["enabled"]) if cfg is not None else False
        running_h = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_work WHERE status=? AND goal_id=?",
            (SchedulerWorkStatus.RUNNING.value, goal_id)).fetchone()[0]
        if enabled and reservation >= 1 and running_h < reservation:
            tele["floor"] = True
            tele["reservation"] = reservation
            tele["running"] = running_h
            return "floor", tele
        # Protection: this claim consumes one free slot; deny it when the
        # slots that would remain free cannot cover the outstanding
        # reservation deficits of OTHER runnable reserved goals (the
        # claiming goal's own deficit is 0 here by definition).
        total_running = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_work WHERE status=?",
            (SchedulerWorkStatus.RUNNING.value,)).fetchone()[0]
        free = int(global_max) - int(total_running)
        outstanding = 0
        for r in self._conn.execute(
                "SELECT goal_id, reservation FROM scheduler_goal_reservations "
                "WHERE enabled=1 AND reservation>=1").fetchall():
            g, rv = r[0], int(r[1])
            if g == goal_id:
                continue
            queued = self._conn.execute(
                "SELECT COUNT(*) FROM scheduler_work WHERE status=? AND goal_id=?",
                (SchedulerWorkStatus.QUEUED.value, g)).fetchone()[0]
            if queued == 0:
                continue  # idle goals reserve nothing
            run_g = self._conn.execute(
                "SELECT COUNT(*) FROM scheduler_work WHERE status=? AND goal_id=?",
                (SchedulerWorkStatus.RUNNING.value, g)).fetchone()[0]
            if run_g < rv:
                outstanding += rv - run_g
        if free - 1 < outstanding:
            tele["reservation"] = reservation
            tele["pressure"] = outstanding
            tele["reserved_capacity"] = self._reservation_total_enabled_in_tx()
            return "deny", tele
        return "pass", tele

    def _sys_ceiling_denied_in_tx(self, goal_id: str, work_id: str,
                                 scheduler_id: str | None, worker_id: str,
                                 now: str, running: int, ceiling: int
                                 ) -> bool:
        """ADR-031 ceiling gate: True when the goal is at/above its
        ENABLED ceiling (deny, row stays QUEUED; no DWRR credit consumed,
        no refill). Runs BEFORE the DWRR gate in both claim paths, so the
        floor path can never bypass the ceiling."""
        cfg = self.get_goal_ceiling_config(goal_id)
        if cfg is None or not cfg["enabled"]:
            return False
        c = int(cfg["ceiling"])
        if running < c:
            return False
        self._sech_insert_in_tx(_audit_event(
            kind="ceiling.denied", ts=now,
            detail={"scheduler_id": scheduler_id, "worker_id": worker_id,
                    "goal_id": goal_id, "work_id": work_id,
                    "running": running, "ceiling": c,
                    "reason": "goal_ceiling", "outcome": "denied",
                    "ts": now}))
        return True

    def _sys_establish_claim_in_tx(self, work_id: str, goal_id: str,
                                   worker_id: str, lease: float, now: str,
                                   scheduler_id: str | None,
                                   floor_tele: dict | None = None
                                   ) -> SchedulerWork:
        """Ownership establishment (inside BEGIN IMMEDIATE): QUEUED ->
        RUNNING + work.claimed atomically; emits reservation.satisfied when
        a floor claim brings the goal exactly to its reservation."""
        self._conn.execute(
            "UPDATE scheduler_work SET status=?, worker_id=?, started_at=?, "
            "lease_expires_at=? WHERE work_id=? AND status=?",
            (SchedulerWorkStatus.RUNNING.value, worker_id, now,
             _iso_plus(now, lease), work_id, SchedulerWorkStatus.QUEUED.value))
        self._sech_insert_in_tx(_audit_event(
            kind="work.claimed", ts=now,
            detail={"scheduler_id": scheduler_id, "worker_id": worker_id,
                    "goal_id": goal_id, "work_id": work_id,
                    "step_index": self._sys_row(work_id).step_index,
                    "lease_expires_at": _iso_plus(now, lease),
                    "outcome": "claimed", "ts": now}))
        if floor_tele:
            rv = int(floor_tele.get("reservation") or 0)
            if rv >= 1:
                new_running = self._conn.execute(
                    "SELECT COUNT(*) FROM scheduler_work WHERE status=? "
                    "AND goal_id=?",
                    (SchedulerWorkStatus.RUNNING.value, goal_id)).fetchone()[0]
                if new_running == rv:
                    self._sech_insert_in_tx(_audit_event(
                        kind="reservation.satisfied", ts=now,
                        detail={"goal_id": goal_id, "work_id": work_id,
                                "reservation": rv, "running": new_running,
                                "satisfied": True, "outcome": "satisfied",
                                "ts": now}))
        return self._sys_row(work_id)

    def _sys_claim_in_tx(self, worker_id: str, lease_seconds: float, now: str,
                         max_lease_seconds: float | None,
                         work_id: str | None = None,
                         scheduler_id: str | None = None) -> SchedulerWork | None:
        """Claim inside an open BEGIN IMMEDIATE transaction. Returns the
        claimed row, or None when the capacity/fair share/reservation/
        goal-weight gate denies (row stays QUEUED). Raises
        SchedulerStateError when a SPECIFIC row is not claimable (raced /
        terminal).

        Admission order (ADR-029, documented in architecture.md):
        1 reclaim stale; 2 global capacity; 3 scheduler fair share;
        4 reservation floor/protection; 5 DWRR goal-weight; 6 ownership.
        """
        self._sys_reclaim_stale_in_tx(now)
        cap_ok, cap_reason = self._sys_capacity_ok_in_tx(
            claiming_scheduler_id=scheduler_id)
        if not cap_ok:
            if work_id is not None:
                g0 = self._conn.execute(
                    "SELECT goal_id FROM scheduler_work WHERE work_id=?",
                    (work_id,)).fetchone()
                reason = cap_reason or "capacity"
                self._sech_claim_denied_event(
                    reason, work_id, g0[0] if g0 else None,
                    scheduler_id, worker_id, now)
            return None
        lease = float(lease_seconds)
        if max_lease_seconds is not None:
            lease = min(lease, max(0.0, float(max_lease_seconds)))
        if work_id is not None:
            row = self._conn.execute(
                "SELECT work_id, goal_id, status FROM scheduler_work WHERE work_id=?",
                (work_id,)).fetchone()
            if row is None:
                raise SchedulerStateError(
                    f"unknown scheduler work id {work_id} (fail closed)")
            if SchedulerWorkStatus(row[2]) != SchedulerWorkStatus.QUEUED:
                self._sys_assert_transition(
                    work_id, SchedulerWorkStatus.RUNNING,
                    SchedulerWorkStatus(row[2]))
            goal_id = row[1]
            decision, rtele = self._sys_reservation_gate_in_tx(goal_id)
            if decision == "deny":
                self._sech_claim_denied_event(
                    "reservation", work_id, goal_id, scheduler_id, worker_id,
                    now, extra=rtele)
                return None  # reservation protected: row stays QUEUED
            run_g = self._conn.execute(
                "SELECT COUNT(*) FROM scheduler_work WHERE status=? "
                "AND goal_id=?",
                (SchedulerWorkStatus.RUNNING.value, goal_id)).fetchone()[0]
            if self._sys_ceiling_denied_in_tx(
                    goal_id, work_id, scheduler_id, worker_id, now,
                    int(run_g), self.get_goal_ceiling(goal_id) or 0):
                return None  # goal at its ceiling: row stays QUEUED
            granted, tele = self._sys_goal_admission_in_tx(goal_id)
            if decision == "floor" and not granted and not tele.get("disabled"):
                # Floor override: below its reservation the goal is admitted
                # even when the DWRR credit gate would deny (the floor is a
                # guarantee, not an opportunity). No credit is debited and
                # no refill is triggered here. The weight-DISABLED hard gate
                # (ADR-027) is never overridden.
                tele = {}
            elif not granted:
                self._sech_claim_denied_event(
                    "goal_weight", work_id, goal_id, scheduler_id, worker_id,
                    now, extra=tele)
                return None  # weighted gate denied: row stays QUEUED
            if tele.get("refill"):
                d = {"scheduler_id": scheduler_id, "worker_id": worker_id,
                     "goal_id": goal_id, "work_id": work_id, "ts": now}
                d.update({k: v for k, v in tele.items() if k in
                          ("weight", "credit_before", "credit_after", "refill")})
                self._sech_insert_in_tx(_audit_event(
                    kind="goal_weight.refill", ts=now, detail=d))
            return self._sys_establish_claim_in_tx(
                work_id, goal_id, worker_id, lease, now, scheduler_id,
                floor_tele=rtele if decision == "floor" else None)
        row = self._conn.execute(
            "SELECT work_id, goal_id FROM scheduler_work "
            "WHERE status=? AND scheduler_id=? ORDER BY created_at, work_id LIMIT 1",
            (SchedulerWorkStatus.QUEUED.value, scheduler_id)).fetchone()
        if row is None:
            return None
        claimed_work_id, goal_id = row[0], row[1]
        decision, rtele = self._sys_reservation_gate_in_tx(goal_id)
        if decision == "deny":
            self._sech_claim_denied_event(
                "reservation", claimed_work_id, goal_id, scheduler_id,
                worker_id, now, extra=rtele)
            return None  # reservation protected: row stays QUEUED
        run_g = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_work WHERE status=? "
            "AND goal_id=?",
            (SchedulerWorkStatus.RUNNING.value, goal_id)).fetchone()[0]
        if self._sys_ceiling_denied_in_tx(
                goal_id, claimed_work_id, scheduler_id, worker_id, now,
                int(run_g), self.get_goal_ceiling(goal_id) or 0):
            return None  # goal at its ceiling: row stays QUEUED
        granted, tele = self._sys_goal_admission_in_tx(goal_id)
        if decision == "floor" and not granted and not tele.get("disabled"):
            # Floor override (see the specific-work branch above).
            tele = {}
        elif not granted:
            self._sech_claim_denied_event(
                "goal_weight", claimed_work_id, goal_id, scheduler_id,
                worker_id, now, extra=tele)
            return None  # weighted gate denied: row stays QUEUED
        if tele.get("refill"):
            d = {"scheduler_id": scheduler_id, "worker_id": worker_id,
                 "goal_id": goal_id, "work_id": claimed_work_id, "ts": now}
            d.update({k: v for k, v in tele.items() if k in
                      ("weight", "credit_before", "credit_after", "refill")})
            self._sech_insert_in_tx(_audit_event(
                kind="goal_weight.refill", ts=now, detail=d))
        return self._sys_establish_claim_in_tx(
            claimed_work_id, goal_id, worker_id, lease, now, scheduler_id,
            floor_tele=rtele if decision == "floor" else None)

    @_threadsafe
    def claim(self, work_id: str, worker_id: str, lease_seconds: float,
              now: str | None = None,
              max_lease_seconds: float | None = None,
              scheduler_id: str | None = None) -> SchedulerWork | None:
        """Atomically claim ONE specific QUEUED row. Returns None when the
        cross-process capacity/fair share is full (row stays QUEUED).
        Exactly one owner under any race; a non-claimable row raises a
        typed error."""
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            got = self._sys_claim_in_tx(worker_id, lease_seconds, now,
                                        max_lease_seconds, work_id=work_id,
                                        scheduler_id=scheduler_id)
            self._conn.commit()
            return got
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def claim_next(self, scheduler_id: str, worker_id: str,
                   lease_seconds: float, now: str | None = None,
                   max_lease_seconds: float | None = None) -> SchedulerWork | None:
        """Atomically claim the oldest QUEUED row for this scheduler. None
        when empty or the cross-process capacity is full."""
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            got = self._sys_claim_in_tx(worker_id, lease_seconds, now,
                                        max_lease_seconds, scheduler_id=scheduler_id)
            self._conn.commit()
            return got
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def heartbeat(self, work_id: str, worker_id: str, lease_seconds: float,
                  now: str | None = None,
                  max_lease_seconds: float | None = None) -> SchedulerWork:
        """Ownership-checked sliding lease extension.

        Unknown, non-running, wrong-owner, past-time, and already-expired
        heartbeats fail closed.  Each renewal is bounded to at most
        ``max_lease_seconds`` beyond ``now`` and never shrinks, so a live
        worker can retain ownership without permitting stale resurrection.
        """
        now = now or utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT status, worker_id, started_at, lease_expires_at "
                "FROM scheduler_work WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise SchedulerStateError(
                    f"unknown scheduler work id {work_id} (fail closed)")
            status, owner, started_at, expiry = (
                SchedulerWorkStatus(row[0]), row[1], row[2], row[3])
            if status != SchedulerWorkStatus.RUNNING:
                raise SchedulerStateError(
                    f"heartbeat requires RUNNING, got {status.value} for {work_id}")
            if owner != worker_id:
                raise SchedulerStateError(
                    f"heartbeat owner mismatch: {work_id} owned by {owner}, "
                    f"not {worker_id} (fail closed)")
            if started_at is None or now < started_at:
                raise SchedulerStateError(
                    f"forged/past heartbeat timestamp for {work_id} (fail closed)")
            if expiry is not None and expiry <= now:
                raise SchedulerStateError(
                    f"stale owner cannot heartbeat expired work {work_id} "
                    f"(lease expired {expiry})")
            max_lease = (float(max_lease_seconds) if max_lease_seconds is not None
                         else float(lease_seconds))
            extension = min(
                max(0.0, float(lease_seconds)), max(0.0, max_lease)
            )
            new_expiry = _iso_plus(now, extension)
            if expiry is not None and new_expiry < expiry:
                new_expiry = expiry  # monotonic: never shrink
            self._conn.execute(
                "UPDATE scheduler_work SET lease_expires_at=? WHERE work_id=?",
                (new_expiry, work_id))
            self._sech_insert_in_tx(_audit_event(
                kind="work.heartbeat", ts=now,
                detail={"work_id": work_id, "worker_id": worker_id,
                        "lease_expires_at": new_expiry,
                        "outcome": "heartbeat", "ts": now}))
            self._conn.commit()
            return self._sys_row(work_id)
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def release_and_claim_next(self, work_id: str, owner_worker_id: str,
                               status: SchedulerWorkStatus,
                               error: str | None, scheduler_id: str,
                               worker_id: str, lease_seconds: float,
                               now: str | None = None,
                               max_lease_seconds: float | None = None,
                               ) -> tuple[SchedulerWork, SchedulerWork | None]:
        """Atomic handoff (release_and_select_next-style): ownership-checked
        terminal transition of ONE row AND claim of the next QUEUED row for
        this scheduler in ONE BEGIN IMMEDIATE transaction. Returns
        (terminal_row, next_row_or_None)."""
        now = now or utcnow()
        if status not in (SchedulerWorkStatus.COMPLETED, SchedulerWorkStatus.FAILED):
            raise SchedulerStateError(
                f"handoff terminal status must be COMPLETED or FAILED, "
                f"got {status.value}")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE scheduler_work SET status=?, error=?, completed_at=?, "
                "lease_expires_at=NULL WHERE work_id=? AND status=? "
                "AND worker_id=? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at > ?",
                (status.value, (error or "")[:500], now, work_id,
                 SchedulerWorkStatus.RUNNING.value, owner_worker_id, now))
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT status, worker_id, lease_expires_at "
                    "FROM scheduler_work WHERE work_id=?",
                    (work_id,)).fetchone()
                if row is None:
                    raise SchedulerStateError(
                        f"unknown scheduler work id {work_id} (fail closed)")
                actual = SchedulerWorkStatus(row[0])
                if actual == SchedulerWorkStatus.RUNNING:
                    if row[1] != owner_worker_id:
                        raise SchedulerStateError(
                            f"stale owner: {work_id} is owned by {row[1]}, "
                            f"not {owner_worker_id} (fail closed)")
                    raise SchedulerStateError(
                        f"stale owner: work {work_id} lease expired "
                        f"{row[2]} before {now} (fail closed)"
                    )
                self._sys_assert_transition(work_id, status, actual)
            nxt = self._sys_claim_in_tx(worker_id, lease_seconds, now,
                                        max_lease_seconds, scheduler_id=scheduler_id)
            self._sech_insert_in_tx(_audit_event(
                kind="work.handoff", ts=now,
                detail={"work_id": work_id, "worker_id": worker_id,
                        "scheduler_id": scheduler_id,
                        "next_work_id": nxt.work_id if nxt else None,
                        "outcome": status.value, "ts": now}))
            self._conn.commit()
            return self._sys_row(work_id), nxt
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------ #
    # ADR-028: scheduler telemetry (OBSERVATIONAL ONLY - never authority)
    # ------------------------------------------------------------------ #

    _SECH_MAX_LIMIT = 1000
    _SECH_DEFAULT_LIMIT = 100
    _SECH_DETAIL_KEYS = (
        "scheduler_id", "worker_id", "goal_id", "task_id", "work_id",
        "step_index", "lease_expires_at", "started_at", "pid", "reason",
        "weight", "credit_before", "credit_after", "refill", "outcome",
        "config", "position", "attempts", "deadline", "next_work_id",
        "disabled", "reservation", "reserved_capacity", "pressure",
        "satisfied", "running", "deficit", "reserved_goal_id",
        "ceiling", "headroom",
    )

    @staticmethod
    def _sech_sanitize_detail(detail: dict) -> dict:
        """Bounded metadata only: allow a fixed set of small keys, truncate
        values; NEVER secrets/prompts/payloads/contents."""
        out = {"schema_version": 1}
        for k, v in (detail or {}).items():
            if k not in SQLiteStorage._SECH_DETAIL_KEYS:
                continue
            if isinstance(v, str):
                v = v[:200]
            elif not isinstance(v, (int, float, bool, type(None))):
                continue
            out[k] = v
        return out

    def _sech_insert_in_tx(self, event) -> None:
        """Insert a scheduler telemetry event inside the CURRENT open
        transaction (caller holds BEGIN IMMEDIATE): the event commits
        atomically with the state transition, so a rolled-back transition
        leaves no phantom event. OBSERVATIONAL ONLY."""
        from arion.observability.events import AuditEvent  # noqa: F401 (cycle-safe)
        d = self._sech_sanitize_detail(event.detail)
        self._conn.execute(
            "INSERT INTO scheduler_events (id, ts, scheduler_id, worker_id, "
            "goal_id, task_id, work_id, step_index, event_type, reason, "
            "success, detail, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (event.id, event.ts,
             d.get("scheduler_id"), d.get("worker_id"), d.get("goal_id"),
             d.get("task_id"), d.get("work_id"),
             int(d["step_index"]) if d.get("step_index") is not None else None,
             event.kind, d.get("reason"), int(event.success),
             json.dumps(d)),
        )

    @_threadsafe
    def append_scheduler_event(self, event: "AuditEvent") -> None:
        """Durably persist one scheduler telemetry event (bounded detail).
        OBSERVATIONAL ONLY - never consulted by any admission/ownership
        path. Callers inside a transaction use `_sech_insert_in_tx`;
        standalone appends are sanitized here."""
        d = self._sech_sanitize_detail(event.detail)
        self._conn.execute(
            "INSERT OR IGNORE INTO scheduler_events (id, ts, scheduler_id, "
            "worker_id, goal_id, task_id, work_id, step_index, event_type, "
            "reason, success, detail, schema_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (event.id, event.ts,
             d.get("scheduler_id"), d.get("worker_id"), d.get("goal_id"),
             d.get("task_id"), d.get("work_id"),
             int(d["step_index"]) if d.get("step_index") is not None else None,
             event.kind, d.get("reason"), int(event.success),
             json.dumps(d)),
        )
        self._conn.commit()

    def _sech_events_sql(self) -> str:
        return ("SELECT id, ts, scheduler_id, worker_id, goal_id, task_id, "
                "work_id, step_index, event_type, reason, success, detail, "
                "schema_version FROM scheduler_events")

    @staticmethod
    def _sech_from_row(row) -> "AuditEvent":
        return _audit_event(
            id=row[0], ts=row[1],
            detail={**json.loads(row[11]),
                    **({"scheduler_id": row[2]} if row[2] else {}),
                    **({"worker_id": row[3]} if row[3] else {}),
                    **({"goal_id": row[4]} if row[4] else {}),
                    **({"task_id": row[5]} if row[5] else {}),
                    **({"work_id": row[6]} if row[6] else {}),
                    **({"step_index": row[7]} if row[7] is not None else {})},
            kind=row[8], success=bool(row[10]), work_id=row[6],
        )

    @_threadsafe
    def recent_scheduler_events(self, limit: int = 100) -> list["AuditEvent"]:
        limit = int(limit)
        if limit < 1 or limit > self._SECH_MAX_LIMIT:
            raise ValueError(
                f"scheduler event limit must be in [1, {self._SECH_MAX_LIMIT}] "
                f"(bounded; fail closed)")
        rows = self._conn.execute(
            self._sech_events_sql() + " ORDER BY rowid DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._sech_from_row(r) for r in reversed(rows)]

    @_threadsafe
    def scheduler_events(self, *, scheduler_id: str | None = None,
                         goal_id: str | None = None,
                         work_id: str | None = None,
                         event_type: str | None = None,
                         since: str | None = None,
                         limit: int = 100) -> list["AuditEvent"]:
        limit = int(limit)
        if limit < 1 or limit > self._SECH_MAX_LIMIT:
            raise ValueError(
                f"scheduler event limit must be in [1, {self._SECH_MAX_LIMIT}] "
                f"(bounded; fail closed)")
        where, params = [], []
        if scheduler_id is not None:
            where.append("scheduler_id=?")
            params.append(scheduler_id)
        if goal_id is not None:
            where.append("goal_id=?")
            params.append(goal_id)
        if work_id is not None:
            where.append("work_id=?")
            params.append(work_id)
        if event_type is not None:
            where.append("event_type=?")
            params.append(event_type)
        if since is not None:
            where.append("ts>=?")
            params.append(since)
        sql = self._sech_events_sql()
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [self._sech_from_row(r) for r in reversed(rows)]

    @_threadsafe
    def scheduler_event_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_events").fetchone()
        return int(row[0])

    @_threadsafe
    def oldest_scheduler_event(self) -> "AuditEvent | None":
        row = self._conn.execute(
            self._sech_events_sql() + " ORDER BY rowid ASC LIMIT 1").fetchone()
        return self._sech_from_row(row) if row else None

    @_threadsafe
    def scheduler_status(self, now: str | None = None) -> dict:
        """Read-only status snapshot computed from durable state (ADR-028
        Phase D). An OBSERVATION - never a cached authority; calling it
        never mutates anything. Bounded fields only."""
        now = now or utcnow()
        running = [r for r in self.list_work(status=SchedulerWorkStatus.RUNNING)]
        queued = [r for r in self.list_work(status=SchedulerWorkStatus.QUEUED)]
        instances = self._conn.execute(
            "SELECT scheduler_id, lease_expires_at FROM scheduler_instances"
        ).fetchall()
        active = [i[0] for i in instances if i[1] is not None and i[1] > now]
        stale = [i[0] for i in instances if i[1] is None or i[1] <= now]
        running_by_scheduler: dict[str, int] = {}
        running_by_goal: dict[str, int] = {}
        queued_by_goal: dict[str, int] = {}
        for r in running:
            running_by_scheduler[r.scheduler_id] = \
                running_by_scheduler.get(r.scheduler_id, 0) + 1
            running_by_goal[r.goal_id] = running_by_goal.get(r.goal_id, 0) + 1
        for r in queued:
            queued_by_goal[r.goal_id] = queued_by_goal.get(r.goal_id, 0) + 1
        credit: dict[str, int] = {}
        for row in self._conn.execute(
                "SELECT goal_id, deficit FROM scheduler_goal_state").fetchall():
            credit[row[0]] = int(row[1])
        recent = self.recent_scheduler_events(limit=200)
        recent_reclaim = sum(1 for e in recent if e.kind == "work.reclaimed")
        recent_failure = sum(1 for e in recent if e.kind == "work.failed")
        # ADR-029 reservation observation (computed from durable rows):
        # enabled configs, total protected capacity, per-goal satisfaction
        # (running >= reservation), and the outstanding pressure (sum of
        # deficits over runnable reserved goals; deterministic).
        reservations = self.list_goal_reservations()
        enabled = [r for r in reservations if r["enabled"]]
        running_by_goal_r = dict(running_by_goal)
        queued_by_goal_r = dict(queued_by_goal)
        reserved_capacity = sum(int(r["reservation"]) for r in enabled)
        satisfaction = {}
        pressure = 0
        for r in enabled:
            rv = int(r["reservation"])
            run_g = running_by_goal_r.get(r["goal_id"], 0)
            satisfaction[r["goal_id"]] = run_g >= rv
            if run_g < rv and queued_by_goal_r.get(r["goal_id"], 0) > 0:
                pressure += rv - run_g
        return {
            "global_max_concurrency": self.get_scheduler_global_max(),
            "running_count": len(running),
            "queued_count": len(queued),
            "active_schedulers": len(active),
            "stale_schedulers": len(stale),
            "running_by_scheduler": running_by_scheduler,
            "running_by_goal": running_by_goal,
            "queued_by_goal": queued_by_goal,
            "goal_weights": self.list_goal_weights(),
            "dwr_credit": credit,
            "goal_reservations": reservations,
            "reserved_capacity": reserved_capacity,
            "reservation_satisfied": satisfaction,
            "reservation_pressure": pressure,
            "recent_reclaim_count": recent_reclaim,
            "recent_failure_count": recent_failure,
            "now": now,
        }

    # ------------------------------------------------------------------ #
    # ADR-030: read-only capacity planning / status projection.          #
    # NEVER an authority: no claims, no heartbeats, no config writes,    #
    # no DWRR mutation, no ownership, no reclaims.                       #
    # ------------------------------------------------------------------ #

    def _sys_credit_for_goal(self, goal_id: str, cap: int | None) -> int:
        """Durable DWRR deficit, clamped exactly like the admission gate
        (ADR-027: min(deficit, max(weight, 2*cap)) when a cap exists)."""
        row = self._conn.execute(
            "SELECT deficit FROM scheduler_goal_state WHERE goal_id=?",
            (goal_id,)).fetchone()
        raw = int(row[0]) if row else 0
        if cap is None:
            return raw
        weight = self.get_goal_weight(goal_id)
        return min(raw, max(weight, 2 * int(cap)))

    def _sys_share_projection(self, cap: int,
                              active_schedulers: int) -> int:
        """The ADR-026 fair-share constant used by the claim path:
        ceil(cap / active) with a single active scheduler getting the
        full cap."""
        if active_schedulers <= 1:
            return int(cap)
        return max(1, -(-int(cap) // active_schedulers))

    def _sys_goal_state_projection(self, goal_id: str, cap: int | None,
                                   running_count: int,
                                   queued_count_g: int, running_g: int,
                                   reservation: int, res_enabled: bool,
                                   weight_enabled: bool, credit: int,
                                   queued_schedulers: set[str],
                                   active_schedulers: int
                                   ) -> tuple[str, bool]:
        """Per-goal eligibility STATE projection (ADR-030 Phase E).

        A READ-ONLY replica of the claim path's decision structure using
        the same durable tables and constants - it never runs the gates
        (which mutate DWRR credit) and never claims anything. Exact
        admission is still authoritative at claim time; another process
        may change state between this snapshot and the claim.
        """
        if queued_count_g == 0:
            return "idle", False
        if not weight_enabled:
            return "weight_disabled", False  # ADR-027 hard gate
        below_floor = (res_enabled and reservation >= 1
                       and running_g < reservation)
        if cap is not None and running_count >= int(cap):
            if below_floor:
                return "reservation_waiting", False
            return "global_capacity_exhausted", False
        # fair-share projection: are ALL of this goal's queued rows owned
        # by schedulers already at/above their share?
        share_limited = False
        if cap is not None and active_schedulers > 1:
            share = self._sys_share_projection(int(cap), active_schedulers)
            for sid in queued_schedulers:
                mine = self._conn.execute(
                    "SELECT COUNT(*) FROM scheduler_work WHERE status=? "
                    "AND scheduler_id=?",
                    (SchedulerWorkStatus.RUNNING.value, sid)).fetchone()[0]
                if int(mine) >= share:
                    share_limited = True
                    break
        if below_floor:
            if share_limited:
                return "reservation_waiting", False
            return "reserved_floor", True
        if share_limited:
            return "scheduler_share_limited", False
        # ADR-031: the goal ceiling binds before DWRR credit at claim
        # time, so it outranks the credit-based states here too.
        ccfg = self.get_goal_ceiling_config(goal_id)
        if ccfg is not None and ccfg["enabled"] \
                and running_g >= int(ccfg["ceiling"]):
            return "goal_ceiling_limited", False
        if credit >= 1:
            return "eligible", True
        # credit < 1: the gate would trigger a refill round iff NO other
        # contending ENABLED goal still holds credit; mirror that rule
        # (disabled goals are skipped, exactly like the gate).
        if cap is not None:
            contending = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT goal_id FROM scheduler_work "
                "WHERE status IN (?, ?)",
                (SchedulerWorkStatus.QUEUED.value,
                 SchedulerWorkStatus.RUNNING.value)).fetchall()]
            for g in contending:
                if g == goal_id:
                    continue
                gcfg = self.get_goal_weight_config(g)
                if gcfg is not None and not gcfg["enabled"]:
                    continue
                if self._sys_credit_for_goal(g, cap) >= 1:
                    return "goal_weight_limited", False
        return "eligible", True  # a refill round would fire at claim time

    def _reservation_pressure_projection(self,
                                         override: dict[str, int] | None
                                         = None) -> int:
        """ADR-029 pressure formula: sum of max(0, R - running) over
        enabled reserved goals WITH queued work; optional per-goal
        reservation override (used by the simulator)."""
        override = override or {}
        running_by_goal: dict[str, int] = {}
        queued_by_goal: dict[str, int] = {}
        for r in self.list_work(status=SchedulerWorkStatus.RUNNING):
            running_by_goal[r.goal_id] = running_by_goal.get(r.goal_id, 0) + 1
        for r in self.list_work(status=SchedulerWorkStatus.QUEUED):
            queued_by_goal[r.goal_id] = queued_by_goal.get(r.goal_id, 0) + 1
        pressure = 0
        for cfg in self.list_goal_reservations():
            if not cfg["enabled"]:
                continue
            g = cfg["goal_id"]
            R = int(override.get(g, int(cfg["reservation"])))
            if R < 1:
                continue
            if queued_by_goal.get(g, 0) == 0:
                continue  # idle goals reserve nothing
            pressure += max(R - running_by_goal.get(g, 0), 0)
        return pressure

    @_threadsafe
    def capacity_snapshot(self, now: str | None = None) -> dict:
        """ADR-030 read-only capacity-planning snapshot. Computed from
        durable scheduler state only; NEVER mutates anything; may be stale
        the instant it returns (admission is authoritative at claim time).
        Bounded fields: ids/counts/enums, no payloads or secrets."""
        now = now or utcnow()
        cap = self.get_scheduler_global_max()
        running = self.list_work(status=SchedulerWorkStatus.RUNNING)
        queued = self.list_work(status=SchedulerWorkStatus.QUEUED)
        running_count = len(running)
        queued_count = len(queued)
        available = None if cap is None else max(int(cap) - running_count, 0)

        running_by_goal: dict[str, int] = {}
        queued_by_goal: dict[str, int] = {}
        scheds: set[str] = set()
        for r in running:
            running_by_goal[r.goal_id] = running_by_goal.get(r.goal_id, 0) + 1
            scheds.add(r.scheduler_id)
        for r in queued:
            queued_by_goal[r.goal_id] = queued_by_goal.get(r.goal_id, 0) + 1
            scheds.add(r.scheduler_id)
        active_schedulers = len(scheds)

        reservations = self.list_goal_reservations()
        enabled_res = [r for r in reservations if r["enabled"]]
        configured_total = sum(int(r["reservation"]) for r in enabled_res)

        active_total = 0
        pressure = 0
        below: list[str] = []
        at: list[str] = []
        above: list[str] = []
        for r in enabled_res:
            R = int(r["reservation"])
            if R < 1:
                continue
            g = r["goal_id"]
            run_g = running_by_goal.get(g, 0)
            if queued_by_goal.get(g, 0) > 0:
                active_total += R
                pressure += max(R - run_g, 0)
            if run_g < R:
                below.append(g)
            elif run_g == R:
                at.append(g)
            else:
                above.append(g)
        below.sort()
        at.sort()
        above.sort()
        unreserved = (None if cap is None
                      else max(int(cap) - running_count - active_total, 0))
        recent = self.recent_scheduler_events(limit=200)

        weights = self.list_goal_weights()
        weight_by_goal = {w["goal_id"]: w for w in weights}
        ceilings = self.list_goal_ceilings()
        ceiling_by_goal = {c["goal_id"]: c for c in ceilings}

        goal_ids = sorted(set(running_by_goal) | set(queued_by_goal)
                          | {r["goal_id"] for r in reservations}
                          | set(weight_by_goal) | set(ceiling_by_goal))
        per_goal: list[dict] = []
        goals_at_ceiling: list[str] = []
        for g in goal_ids:
            wcfg = weight_by_goal.get(g)
            weight = int(wcfg["weight"]) if wcfg else 1
            weight_enabled = bool(wcfg["enabled"]) if wcfg else True
            rcfg = next((r for r in reservations if r["goal_id"] == g), None)
            R = int(rcfg["reservation"]) if rcfg else 0
            res_enabled = bool(rcfg["enabled"]) if rcfg else True
            ccfg = ceiling_by_goal.get(g)
            C = int(ccfg["ceiling"]) if ccfg else None
            c_enabled = bool(ccfg["enabled"]) if ccfg else True
            run_g = running_by_goal.get(g, 0)
            que_g = queued_by_goal.get(g, 0)
            deficit = max(R - run_g, 0)
            satisfied = R == 0 or run_g >= R
            pressure_g = deficit if que_g > 0 else 0
            credit = self._sys_credit_for_goal(g, cap)
            headroom = (None if C is None
                        else max(int(C) - run_g, 0))
            if C is not None and c_enabled and run_g >= int(C):
                goals_at_ceiling.append(g)
            queued_scheds = {r.scheduler_id for r in queued
                             if r.goal_id == g}
            state, eligible = self._sys_goal_state_projection(
                g, cap, running_count, que_g, run_g, R, res_enabled,
                weight_enabled, credit, queued_scheds, active_schedulers)
            per_goal.append({
                "goal_id": g,
                "weight": weight,
                "weight_enabled": weight_enabled,
                "reservation": R,
                "reservation_enabled": res_enabled,
                "ceiling": C,
                "ceiling_enabled": c_enabled,
                "ceiling_headroom": headroom,
                "running": run_g,
                "queued": que_g,
                "reservation_deficit": deficit,
                "reservation_satisfied": satisfied,
                "reservation_pressure": pressure_g,
                "dwr_credit": credit,
                "state": state,
                "eligible": eligible,
            })
        goals_at_ceiling.sort()

        return {
            "global_max_concurrency": cap,
            "running_count": running_count,
            "queued_count": queued_count,
            "available_capacity": available,
            "reserved_capacity": configured_total,
            "active_reserved_capacity": active_total,
            "reservation_pressure": pressure,
            "unreserved_capacity": unreserved,
            "active_scheduler_count": active_schedulers,
            "active_goal_count": len(goal_ids),
            "reserved_goal_count": len(enabled_res),
            "goals_below_reservation": below,
            "goals_at_reservation": at,
            "goals_above_reservation": above,
            "ceiling_limited_goal_count": len(
                [c for c in ceilings if c["enabled"]]),
            "goals_at_ceiling": goals_at_ceiling,
            "recent_ceiling_denials": sum(
                1 for e in recent if e.kind == "ceiling.denied"),
            "goals": per_goal,
            "goal_weights": weights,
            "goal_reservations": reservations,
            "goal_ceilings": ceilings,
            "now": now,
        }

    @_threadsafe
    def explain_goal_eligibility(self, goal_id: str) -> dict:
        """ADR-030 Phase E: read-only explanation of one goal's current
        admission state. A projection - never a gate; admission is still
        authoritative at claim time."""
        snap = self.capacity_snapshot()
        for g in snap["goals"]:
            if g["goal_id"] == goal_id:
                return {
                    "goal_id": goal_id,
                    "state": g["state"],
                    "eligible": g["eligible"],
                    "reservation": g["reservation"],
                    "reservation_satisfied": g["reservation_satisfied"],
                    "running": g["running"],
                    "queued": g["queued"],
                    "dwr_credit": g["dwr_credit"],
                    "note": "Eligible based on current snapshot; "
                            "admission is still authoritative at claim "
                            "time.",
                }
        return {
            "goal_id": goal_id,
            "state": "unknown",
            "eligible": False,
            "reservation": 0,
            "reservation_satisfied": True,
            "running": 0,
            "queued": 0,
            "dwr_credit": 0,
            "note": "Goal is not configured and has no scheduler rows; "
                    "admission is still authoritative at claim time.",
        }

    @_threadsafe
    def reservation_feasibility(self,
                                proposed: dict[str, int] | None = None
                                ) -> dict:
        """ADR-030 Phase C: deterministic read-only feasibility of a
        reservation configuration. `proposed=None` evaluates the CURRENT
        enabled configuration; `proposed={goal_id: reservation}` evaluates
        a FULL proposed configuration (values treated as enabled).
        Never mutates configuration."""
        cap = self.get_scheduler_global_max()
        current_total = sum(
            int(r["reservation"]) for r in self.list_goal_reservations()
            if r["enabled"])
        if proposed is None:
            total = current_total
            affected = sorted(
                r["goal_id"] for r in self.list_goal_reservations()
                if int(r["reservation"]) > 0)
        else:
            if not isinstance(proposed, dict) or not proposed:
                raise SchedulerRegistryError(
                    "proposed reservation config must be a non-empty "
                    "dict of goal_id -> int (fail closed)")
            total = 0
            for g, v in proposed.items():
                if not isinstance(g, str) or not g:
                    raise SchedulerRegistryError(
                        f"invalid goal id {g!r} in proposed config "
                        f"(fail closed)")
                if not isinstance(v, int) or isinstance(v, bool):
                    raise SchedulerRegistryError(
                        f"reservation for {g} must be an integer, got "
                        f"{v!r} (fail closed)")
                if v < 0 or v > self._RESERVATION_MAX:
                    raise SchedulerRegistryError(
                        f"reservation for {g} must be in [0, "
                        f"{self._RESERVATION_MAX}], got {v} (fail closed)")
                total += int(v)
            affected = sorted(proposed)
        # ADR-031: a proposed floor may never exceed the goal's ENABLED
        # ceiling (durable); ceilings never participate in the cap sum.
        for g in affected:
            ccfg = self.get_goal_ceiling_config(g)
            if ccfg is not None and ccfg["enabled"]:
                rv = int(proposed[g]) if proposed is not None else                     self.get_goal_reservation(g)
                if rv > int(ccfg["ceiling"]):
                    return {
                        "feasible": False,
                        "global_max": cap,
                        "configured_total": current_total,
                        "proposed_total": total,
                        "overflow": 0,
                        "affected_goals": affected,
                        "reason": "floor_exceeds_ceiling",
                    }
        if cap is None:
            return {
                "feasible": True,
                "global_max": None,
                "configured_total": current_total,
                "proposed_total": total,
                "overflow": 0,
                "affected_goals": affected,
                "reason": "no_global_cap",
            }
        overflow = max(total - int(cap), 0)
        return {
            "feasible": overflow == 0,
            "global_max": int(cap),
            "configured_total": current_total,
            "proposed_total": total,
            "overflow": overflow,
            "affected_goals": affected,
            "reason": "ok" if overflow == 0 else "oversubscribed",
        }

    @_threadsafe
    def simulate_reservation_change(self, goal_id: str,
                                    new_reservation: int) -> dict:
        """ADR-030 Phase D: read-only dry-run of replacing ONE goal's
        reservation. Never persists anything; never touches DWRR credit,
        events, or work rows."""
        if not isinstance(goal_id, str) or not goal_id:
            raise SchedulerRegistryError(
                "goal_id required (fail closed)")
        if not isinstance(new_reservation, int) or                 isinstance(new_reservation, bool):
            raise SchedulerRegistryError(
                f"reservation must be an integer, got {new_reservation!r} "
                f"(fail closed)")
        if new_reservation < 0 or new_reservation > self._RESERVATION_MAX:
            raise SchedulerRegistryError(
                f"reservation must be in [0, {self._RESERVATION_MAX}], "
                f"got {new_reservation} (fail closed)")
        cap = self.get_scheduler_global_max()
        cfg = self.get_goal_reservation_config(goal_id)
        current = int(cfg["reservation"]) if cfg else 0
        current_enabled = bool(cfg["enabled"]) if cfg else True
        enabled_total = sum(
            int(r["reservation"]) for r in self.list_goal_reservations()
            if r["enabled"])
        contribution = current if current_enabled else 0
        proposed_total = enabled_total - contribution + int(new_reservation)
        feasible = cap is None or proposed_total <= int(cap)
        overflow = (0 if cap is None
                    else max(proposed_total - int(cap), 0))
        remaining = (None if cap is None
                     else max(int(cap) - proposed_total, 0))
        pressure_now = self._reservation_pressure_projection()
        pressure_prop = self._reservation_pressure_projection(
            override={goal_id: int(new_reservation)})
        if pressure_prop > pressure_now:
            delta = "increase"
        elif pressure_prop < pressure_now:
            delta = "decrease"
        else:
            delta = "unchanged"
        # ADR-031: the proposed floor must not exceed an enabled ceiling
        ccfg = self.get_goal_ceiling_config(goal_id)
        floor_ceiling_valid = True
        if ccfg is not None and ccfg["enabled"]                 and int(new_reservation) > int(ccfg["ceiling"]):
            floor_ceiling_valid = False
        return {
            "goal_id": goal_id,
            "current_reservation": current,
            "current_enabled": current_enabled,
            "proposed_reservation": int(new_reservation),
            "current_total": enabled_total,
            "proposed_total": proposed_total,
            "global_max": cap,
            "remaining_capacity": remaining,
            "feasible": feasible and floor_ceiling_valid,
            "overflow": overflow,
            "floor_ceiling_valid": floor_ceiling_valid,
            "pressure_delta": delta,
            "reservation_pressure_now": pressure_now,
            "reservation_pressure_proposed": pressure_prop,
            "affected_goals": sorted(
                {goal_id} | {g["goal_id"]
                             for g in self.list_goal_reservations()
                             if g["enabled"] and int(g["reservation"]) > 0}),
            "note": "Dry-run only: nothing was persisted; admission is "
                    "authoritative at claim time.",
        }

    @_threadsafe
    def simulate_ceiling_change(self, goal_id: str,
                                new_ceiling: int | None) -> dict:
        """ADR-031 Phase I: read-only dry-run of changing ONE goal's
        ceiling (None = unbounded). Never persists anything."""
        if not isinstance(goal_id, str) or not goal_id:
            raise SchedulerRegistryError(
                "goal_id required (fail closed)")
        if new_ceiling is not None and (
                not isinstance(new_ceiling, int)
                or isinstance(new_ceiling, bool)):
            raise SchedulerRegistryError(
                f"ceiling must be an integer or None, got "
                f"{new_ceiling!r} (fail closed)")
        if new_ceiling is not None and                 (new_ceiling < 1 or new_ceiling > self._CEILING_MAX):
            raise SchedulerRegistryError(
                f"ceiling must be in [1, {self._CEILING_MAX}] or None "
                f"(fail closed)")
        cap = self.get_scheduler_global_max()
        cfg = self.get_goal_ceiling_config(goal_id)
        current = int(cfg["ceiling"]) if cfg else None
        current_enabled = bool(cfg["enabled"]) if cfg else True
        rcfg = self.get_goal_reservation_config(goal_id)
        floor = int(rcfg["reservation"]) if rcfg else 0
        floor_enabled = bool(rcfg["enabled"]) if rcfg else True
        # validity of the proposed ceiling against the ENABLED floor
        valid = True
        if new_ceiling is not None and floor_enabled                 and floor > int(new_ceiling):
            valid = False
        snap = self.capacity_snapshot()
        goal = next((g for g in snap["goals"] if g["goal_id"] == goal_id),
                    None)
        running = goal["running"] if goal else 0
        headroom_now = (None if current is None else max(current - running, 0))
        headroom_prop = (None if new_ceiling is None
                         else max(int(new_ceiling) - running, 0))
        if headroom_now is None and headroom_prop is None:
            hdelta = "unchanged"
        elif headroom_now is None:
            hdelta = "decrease"
        elif headroom_prop is None:
            hdelta = "increase"
        elif headroom_prop > headroom_now:
            hdelta = "increase"
        elif headroom_prop < headroom_now:
            hdelta = "decrease"
        else:
            hdelta = "unchanged"
        return {
            "goal_id": goal_id,
            "current_ceiling": current,
            "current_enabled": current_enabled,
            "proposed_ceiling": new_ceiling,
            "floor": floor if floor_enabled else None,
            "floor_ceiling_valid": valid,
            "global_max": cap,
            "running": running,
            "ceiling_headroom_now": headroom_now,
            "ceiling_headroom_proposed": headroom_prop,
            "headroom_delta": hdelta,
            "affected_goals": [goal_id],
            "note": "Dry-run only: nothing was persisted; admission is "
                    "authoritative at claim time.",
        }

    @_threadsafe
    def simulate_goal_policy(self, goal_id: str,
                             reservation: int | None = None,
                             ceiling: int | None = None,
                             weight: int | None = None) -> dict:
        """ADR-031 Phase I: general read-only policy dry-run for ONE goal.
        Each supplied dimension is validated and compared to the durable
        value; the resulting floor/ceiling pair and the global
        feasibility are reported. Never persists anything."""
        if not isinstance(goal_id, str) or not goal_id:
            raise SchedulerRegistryError(
                "goal_id required (fail closed)")
        rcfg = self.get_goal_reservation_config(goal_id)
        floor_now = int(rcfg["reservation"]) if rcfg else 0
        floor_en = bool(rcfg["enabled"]) if rcfg else True
        ccfg = self.get_goal_ceiling_config(goal_id)
        ceiling_now = int(ccfg["ceiling"]) if ccfg else None
        ceiling_en = bool(ccfg["enabled"]) if ccfg else True
        wcfg = self.get_goal_weight_config(goal_id)
        weight_now = int(wcfg["weight"]) if wcfg else 1
        floor_prop = floor_now if reservation is None else reservation
        ceiling_prop = ceiling_now if ceiling is None else ceiling
        weight_prop = weight_now if weight is None else weight
        # validate proposed values exactly like the config APIs
        if not isinstance(floor_prop, int) or isinstance(floor_prop, bool) \
                or floor_prop < 0 or floor_prop > self._RESERVATION_MAX:
            raise SchedulerRegistryError(
                f"reservation must be an integer in [0, "
                f"{self._RESERVATION_MAX}] (fail closed)")
        if ceiling_prop is not None and (
                not isinstance(ceiling_prop, int)
                or isinstance(ceiling_prop, bool)
                or ceiling_prop < 1 or ceiling_prop > self._CEILING_MAX):
            raise SchedulerRegistryError(
                f"ceiling must be an integer in [1, "
                f"{self._CEILING_MAX}] or None (fail closed)")
        if not isinstance(weight_prop, int) or isinstance(weight_prop, bool) \
                or weight_prop < 1 or weight_prop > self._WEIGHT_MAX:
            raise SchedulerRegistryError(
                f"weight must be an integer in [1, {self._WEIGHT_MAX}] "
                f"(fail closed)")
        pair_valid = (ceiling_prop is None or floor_prop <= ceiling_prop)
        cap = self.get_scheduler_global_max()
        feasibility = self.reservation_feasibility(
            proposed={goal_id: floor_prop}) if pair_valid else None
        snap = self.capacity_snapshot()
        goal = next((g for g in snap["goals"] if g["goal_id"] == goal_id),
                    None)
        running = goal["running"] if goal else 0
        headroom = (None if ceiling_prop is None
                    else max(int(ceiling_prop) - running, 0))
        return {
            "goal_id": goal_id,
            "current_floor": floor_now if floor_en else None,
            "proposed_floor": floor_prop,
            "current_ceiling": ceiling_now if ceiling_en else None,
            "proposed_ceiling": ceiling_prop,
            "current_weight": weight_now,
            "proposed_weight": weight_prop,
            "floor_ceiling_valid": pair_valid,
            "global_max": cap,
            "reservation_total": (feasibility["proposed_total"]
                                  if feasibility else None),
            "feasible": (pair_valid and feasibility["feasible"]
                         if feasibility else False),
            "reason": (None if pair_valid else "floor_exceeds_ceiling"),
            "ceiling_headroom": headroom,
            "reservation_pressure": snap["reservation_pressure"],
            "affected_goals": [goal_id],
            "note": "Dry-run only: nothing was persisted; admission is "
                    "authoritative at claim time.",
        }

    @_threadsafe
    def reservation_check(self) -> dict:
        """ADR-030 Phase G: read-only check of the CURRENT reservation
        configuration. Composed from the snapshot + feasibility; never
        mutates anything."""
        snap = self.capacity_snapshot()
        feas = self.reservation_feasibility()
        idle = sorted(
            g["goal_id"] for g in snap["goals"]
            if g["reservation"] >= 1 and g["queued"] == 0)
        return {
            "global_max": feas["global_max"],
            "configured_total": feas["configured_total"],
            "feasible": feas["feasible"],
            "overflow": feas["overflow"],
            "reason": feas["reason"],
            "active_reservation": snap["active_reserved_capacity"],
            "reservation_pressure": snap["reservation_pressure"],
            "goals_below": snap["goals_below_reservation"],
            "idle_reserved_goals": idle,
            "goals_at_ceiling": snap["goals_at_ceiling"],
            "unreserved_capacity": snap["unreserved_capacity"],
            "note": "Read-only check: admission is authoritative at "
                    "claim time.",
        }

    @_threadsafe
    def simulate_reservation_config(self,
                                    proposed: dict[str, int]) -> dict:
        """ADR-030 Phase D: read-only dry-run of a FULL proposed
        reservation configuration. Never persists anything."""
        feasibility = self.reservation_feasibility(proposed=proposed)
        pressure_now = self._reservation_pressure_projection()
        pressure_prop = self._reservation_pressure_projection(
            override=dict(proposed))
        if pressure_prop > pressure_now:
            delta = "increase"
        elif pressure_prop < pressure_now:
            delta = "decrease"
        else:
            delta = "unchanged"
        return {
            "feasible": feasibility["feasible"],
            "global_max": feasibility["global_max"],
            "current_total": feasibility["configured_total"],
            "proposed_total": feasibility["proposed_total"],
            "overflow": feasibility["overflow"],
            "affected_goals": feasibility["affected_goals"],
            "reason": feasibility["reason"],
            "pressure_delta": delta,
            "reservation_pressure_now": pressure_now,
            "reservation_pressure_proposed": pressure_prop,
            "note": "Dry-run only: nothing was persisted; admission is "
                    "authoritative at claim time.",
        }

    @_threadsafe
    def prune_scheduler_events(self, cutoff: str, batch_size: int = 500) -> int:
        """Explicit retention: delete events older than `cutoff` in bounded
        batches. NEVER touches scheduler authority tables; pruning cannot
        affect execution. Returns the number of events removed."""
        batch_size = int(batch_size)
        if batch_size < 1 or batch_size > 5000:
            raise ValueError("prune batch size must be in [1, 5000]")
        removed = 0
        while True:
            batch = [r[0] for r in self._conn.execute(
                "SELECT rowid FROM scheduler_events WHERE ts<? "
                " ORDER BY rowid LIMIT ?",
                (cutoff, batch_size)).fetchall()]
            if not batch:
                break
            self._conn.execute(
                "DELETE FROM scheduler_events WHERE rowid IN (%s)"
                % ",".join("?" * len(batch)), batch)
            self._conn.commit()
            removed += len(batch)
        return removed

    # ------------------------------------------------------------------ #
    # ADR-027: durable per-goal scheduling weights (scheduler POLICY,
    # never execution authority)
    # ------------------------------------------------------------------ #

    _WEIGHT_MAX = 10000

    @_threadsafe
    def set_goal_weight(self, goal_id: str, weight: int, *,
                        enabled: bool = True, by: str = "operator",
                        now: str | None = None) -> None:
        """Set/update a goal's durable scheduling weight. Fail closed:
        goal_id required, weight must be a positive integer <= 10000."""
        now = now or utcnow()
        if not goal_id:
            raise SchedulerRegistryError("goal_id required (fail closed)")
        if not isinstance(weight, int) or isinstance(weight, bool):
            raise SchedulerRegistryError(
                f"weight must be a positive integer, got {weight!r} (fail closed)")
        if weight < 1 or weight > self._WEIGHT_MAX:
            raise SchedulerRegistryError(
                f"weight must be in [1, {self._WEIGHT_MAX}], got {weight} "
                f"(fail closed)")
        self._conn.execute(
            "INSERT OR REPLACE INTO scheduler_goal_weights "
            "(goal_id, weight, enabled, updated_at, updated_by) VALUES (?,?,?,?,?)",
            (goal_id, int(weight), 1 if enabled else 0, now, str(by)[:100]))
        self._sech_insert_in_tx(_audit_event(
            kind="scheduler.config_changed", ts=now,
            detail={"goal_id": goal_id, "config": "goal_weight",
                    "reason": f"weight={int(weight)} enabled={enabled}",
                    "outcome": "set", "ts": now}))
        self._conn.commit()

    @_threadsafe
    def get_goal_weight(self, goal_id: str) -> int:
        row = self._conn.execute(
            "SELECT weight FROM scheduler_goal_weights WHERE goal_id=?",
            (goal_id,)).fetchone()
        if row is None:
            return 1  # deterministic default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 1

    @_threadsafe
    def get_goal_weight_config(self, goal_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT goal_id, weight, enabled, updated_at, updated_by "
            "FROM scheduler_goal_weights WHERE goal_id=?",
            (goal_id,)).fetchone()
        if row is None:
            return None
        return {
            "goal_id": row[0],
            "weight": int(row[1]),
            "enabled": bool(row[2]),
            "updated_at": row[3],
            "updated_by": row[4],
        }

    @_threadsafe
    def list_goal_weights(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT goal_id, weight, enabled, updated_at, updated_by "
            "FROM scheduler_goal_weights ORDER BY goal_id").fetchall()
        return [{
            "goal_id": r[0], "weight": int(r[1]), "enabled": bool(r[2]),
            "updated_at": r[3], "updated_by": r[4],
        } for r in rows]

    @_threadsafe
    def remove_goal_weight(self, goal_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM scheduler_goal_weights WHERE goal_id=?", (goal_id,))
        if cur.rowcount > 0:
            self._sech_insert_in_tx(_audit_event(
                kind="scheduler.config_changed", ts=utcnow(),
                detail={"goal_id": goal_id, "config": "goal_weight",
                        "outcome": "removed", "ts": utcnow()}))
        self._conn.commit()
        return cur.rowcount > 0

    @_threadsafe
    def set_goal_weight_enabled(self, goal_id: str,
                                enabled: bool) -> dict | None:
        cur = self._conn.execute(
            "UPDATE scheduler_goal_weights SET enabled=?, updated_at=? "
            "WHERE goal_id=?",
            (1 if enabled else 0, utcnow(), goal_id))
        if cur.rowcount > 0:
            self._sech_insert_in_tx(_audit_event(
                kind="scheduler.config_changed", ts=utcnow(),
                detail={"goal_id": goal_id, "config": "goal_weight",
                        "reason": "enabled" if enabled else "disabled",
                        "outcome": "set", "ts": utcnow()}))
        self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_goal_weight_config(goal_id)

    _RESERVATION_MAX = 10000

    def _reservation_total_enabled_in_tx(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(reservation), 0) FROM "
            "scheduler_goal_reservations WHERE enabled=1").fetchone()
        return int(row[0]) if row else 0

    @_threadsafe
    def set_goal_reservation(self, goal_id: str, reservation: int, *,
                             enabled: bool = True, by: str = "operator",
                             now: str | None = None) -> None:
        """Set/update a goal's durable minimum capacity reservation
        (ADR-029). Fail closed: goal_id required; reservation must be an
        integer in [0, _RESERVATION_MAX]; with a global cap configured the
        TOTAL of enabled reservations may never exceed the cap (REJECT,
        never normalize - an impossible guarantee is not silently
        accepted). Emits goal_reservation_changed atomically."""
        now = now or utcnow()
        if not goal_id:
            raise SchedulerRegistryError(
                "goal_id required (fail closed)")
        if not isinstance(reservation, int) or isinstance(reservation, bool):
            raise SchedulerRegistryError(
                f"reservation must be an integer, got {reservation!r} "
                f"(fail closed)")
        if reservation < 0 or reservation > self._RESERVATION_MAX:
            raise SchedulerRegistryError(
                f"reservation must be in [0, {self._RESERVATION_MAX}], "
                f"got {reservation} (fail closed)")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            global_max = self.get_scheduler_global_max()
            if global_max is not None:
                current = self._conn.execute(
                    "SELECT COALESCE(SUM(reservation), 0) FROM "
                    "scheduler_goal_reservations WHERE enabled=1 AND "
                    "goal_id<>?", (goal_id,)).fetchone()[0]
                new_total = int(current) + (int(reservation) if enabled else 0)
                if new_total > int(global_max):
                    self._conn.rollback()
                    raise SchedulerRegistryError(
                        f"reservation total {new_total} exceeds global max "
                        f"{global_max} (ADR-029 oversubscription, fail "
                        f"closed)")
            if enabled:
                ccfg = self._conn.execute(
                    "SELECT ceiling FROM scheduler_goal_ceilings "
                    "WHERE goal_id=? AND enabled=1", (goal_id,)).fetchone()
                if ccfg is not None and int(reservation) > int(ccfg[0]):
                    self._conn.rollback()
                    raise SchedulerRegistryError(
                        f"reservation {reservation} exceeds enabled ceiling "
                        f"{ccfg[0]} for {goal_id} "
                        f"(ADR-031 floor<=ceiling, fail closed)")
            self._conn.execute(
                "INSERT OR REPLACE INTO scheduler_goal_reservations "
                "(goal_id, reservation, enabled, updated_at, updated_by) "
                "VALUES (?,?,?,?,?)",
                (goal_id, int(reservation), 1 if enabled else 0, now,
                 str(by)[:100]))
            self._sech_insert_in_tx(_audit_event(
                kind="goal_reservation_changed", ts=now,
                detail={"goal_id": goal_id,
                        "config": "goal_reservation",
                        "reason": f"reservation={int(reservation)} "
                                  f"enabled={enabled}",
                        "outcome": "set", "ts": now}))
            self._conn.commit()
        except SchedulerRegistryError:
            raise
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def get_goal_reservation(self, goal_id: str) -> int:
        row = self._conn.execute(
            "SELECT reservation FROM scheduler_goal_reservations "
            "WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            return 0  # deterministic default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    @_threadsafe
    def get_goal_reservation_config(self, goal_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT goal_id, reservation, enabled, updated_at, updated_by "
            "FROM scheduler_goal_reservations WHERE goal_id=?",
            (goal_id,)).fetchone()
        if row is None:
            return None
        return {
            "goal_id": row[0],
            "reservation": int(row[1]),
            "enabled": bool(row[2]),
            "updated_at": row[3],
            "updated_by": row[4],
        }

    @_threadsafe
    def list_goal_reservations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT goal_id, reservation, enabled, updated_at, updated_by "
            "FROM scheduler_goal_reservations ORDER BY goal_id").fetchall()
        return [{
            "goal_id": r[0], "reservation": int(r[1]),
            "enabled": bool(r[2]), "updated_at": r[3], "updated_by": r[4],
        } for r in rows]

    @_threadsafe
    def remove_goal_reservation(self, goal_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM scheduler_goal_reservations WHERE goal_id=?",
            (goal_id,))
        if cur.rowcount > 0:
            self._sech_insert_in_tx(_audit_event(
                kind="goal_reservation_changed", ts=utcnow(),
                detail={"goal_id": goal_id,
                        "config": "goal_reservation",
                        "outcome": "removed", "ts": utcnow()}))
        self._conn.commit()
        return cur.rowcount > 0

    @_threadsafe
    def set_goal_reservation_enabled(self, goal_id: str,
                                     enabled: bool) -> dict | None:
        cfg = self.get_goal_reservation_config(goal_id)
        if cfg is None:
            return None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if enabled:
                global_max = self.get_scheduler_global_max()
                if global_max is not None:
                    total = self._reservation_total_enabled_in_tx()
                    if total + int(cfg["reservation"]) > int(global_max):
                        self._conn.rollback()
                        raise SchedulerRegistryError(
                            f"enabling reservation {cfg['reservation']} for "
                            f"{goal_id} would exceed global max {global_max} "
                            f"(ADR-029 oversubscription, fail closed)")
                # ADR-031: floor <= ceiling when both are enabled
                ccfg = self._conn.execute(
                    "SELECT ceiling FROM scheduler_goal_ceilings "
                    "WHERE goal_id=? AND enabled=1", (goal_id,)).fetchone()
                if ccfg is not None and int(cfg["reservation"]) > int(ccfg[0]):
                    self._conn.rollback()
                    raise SchedulerRegistryError(
                        f"reservation {cfg['reservation']} exceeds enabled "
                        f"ceiling {ccfg[0]} for {goal_id} "
                        f"(ADR-031 floor<=ceiling, fail closed)")
            cur = self._conn.execute(
                "UPDATE scheduler_goal_reservations SET enabled=?, "
                "updated_at=? WHERE goal_id=?",
                (1 if enabled else 0, utcnow(), goal_id))
            if cur.rowcount > 0:
                self._sech_insert_in_tx(_audit_event(
                    kind="goal_reservation_changed", ts=utcnow(),
                    detail={"goal_id": goal_id,
                            "config": "goal_reservation",
                            "reason": "enabled" if enabled else "disabled",
                            "outcome": "set", "ts": utcnow()}))
            self._conn.commit()
        except SchedulerRegistryError:
            raise
        except Exception:
            self._conn.rollback()
            raise
        return self.get_goal_reservation_config(goal_id)

    _CEILING_MAX = 10000

    @_threadsafe
    def set_goal_ceiling(self, goal_id: str, ceiling: int, *,
                         enabled: bool = True, by: str = "operator",
                         now: str | None = None) -> None:
        """Set/update a goal's durable maximum concurrency ceiling
        (ADR-031). Fail closed: goal_id required; ceiling must be an
        integer in [1, _CEILING_MAX] (0 is NOT a ceiling - remove/
        disable for unbounded); with an ENABLED reservation floor R the
        pair must satisfy R <= ceiling. Emits goal_ceiling_changed
        atomically."""
        now = now or utcnow()
        if not goal_id:
            raise SchedulerRegistryError(
                "goal_id required (fail closed)")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool):
            raise SchedulerRegistryError(
                f"ceiling must be an integer, got {ceiling!r} (fail closed)")
        if ceiling < 1 or ceiling > self._CEILING_MAX:
            raise SchedulerRegistryError(
                f"ceiling must be in [1, {self._CEILING_MAX}], got "
                f"{ceiling} (fail closed; remove/disable for unbounded)")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if enabled:
                rcfg = self._conn.execute(
                    "SELECT reservation FROM scheduler_goal_reservations "
                    "WHERE goal_id=? AND enabled=1", (goal_id,)).fetchone()
                if rcfg is not None and int(rcfg[0]) > int(ceiling):
                    self._conn.rollback()
                    raise SchedulerRegistryError(
                        f"ceiling {ceiling} below enabled reservation "
                        f"{rcfg[0]} for {goal_id} "
                        f"(ADR-031 floor<=ceiling, fail closed)")
            self._conn.execute(
                "INSERT OR REPLACE INTO scheduler_goal_ceilings "
                "(goal_id, ceiling, enabled, updated_at, updated_by) "
                "VALUES (?,?,?,?,?)",
                (goal_id, int(ceiling), 1 if enabled else 0, now,
                 str(by)[:100]))
            self._sech_insert_in_tx(_audit_event(
                kind="goal_ceiling_changed", ts=now,
                detail={"goal_id": goal_id, "config": "goal_ceiling",
                        "reason": f"ceiling={int(ceiling)} enabled={enabled}",
                        "outcome": "set", "ts": now}))
            self._conn.commit()
        except SchedulerRegistryError:
            raise
        except Exception:
            self._conn.rollback()
            raise

    @_threadsafe
    def get_goal_ceiling(self, goal_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT ceiling FROM scheduler_goal_ceilings WHERE goal_id=?",
            (goal_id,)).fetchone()
        if row is None:
            return None  # unbounded by any goal-specific ceiling
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    @_threadsafe
    def get_goal_ceiling_config(self, goal_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT goal_id, ceiling, enabled, updated_at, updated_by "
            "FROM scheduler_goal_ceilings WHERE goal_id=?",
            (goal_id,)).fetchone()
        if row is None:
            return None
        return {
            "goal_id": row[0],
            "ceiling": int(row[1]),
            "enabled": bool(row[2]),
            "updated_at": row[3],
            "updated_by": row[4],
        }

    @_threadsafe
    def list_goal_ceilings(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT goal_id, ceiling, enabled, updated_at, updated_by "
            "FROM scheduler_goal_ceilings ORDER BY goal_id").fetchall()
        return [{
            "goal_id": r[0], "ceiling": int(r[1]),
            "enabled": bool(r[2]), "updated_at": r[3], "updated_by": r[4],
        } for r in rows]

    @_threadsafe
    def remove_goal_ceiling(self, goal_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM scheduler_goal_ceilings WHERE goal_id=?",
            (goal_id,))
        if cur.rowcount > 0:
            self._sech_insert_in_tx(_audit_event(
                kind="goal_ceiling_changed", ts=utcnow(),
                detail={"goal_id": goal_id, "config": "goal_ceiling",
                        "outcome": "removed", "ts": utcnow()}))
        self._conn.commit()
        return cur.rowcount > 0

    @_threadsafe
    def set_goal_ceiling_enabled(self, goal_id: str,
                                 enabled: bool) -> dict | None:
        cfg = self.get_goal_ceiling_config(goal_id)
        if cfg is None:
            return None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if enabled:
                rcfg = self._conn.execute(
                    "SELECT reservation FROM scheduler_goal_reservations "
                    "WHERE goal_id=? AND enabled=1", (goal_id,)).fetchone()
                if rcfg is not None and int(rcfg[0]) > int(cfg["ceiling"]):
                    self._conn.rollback()
                    raise SchedulerRegistryError(
                        f"enabling ceiling {cfg['ceiling']} for {goal_id} "
                        f"would violate enabled reservation {rcfg[0]} "
                        f"(ADR-031 floor<=ceiling, fail closed)")
            cur = self._conn.execute(
                "UPDATE scheduler_goal_ceilings SET enabled=?, "
                "updated_at=? WHERE goal_id=?",
                (1 if enabled else 0, utcnow(), goal_id))
            if cur.rowcount > 0:
                self._sech_insert_in_tx(_audit_event(
                    kind="goal_ceiling_changed", ts=utcnow(),
                    detail={"goal_id": goal_id, "config": "goal_ceiling",
                            "reason": "enabled" if enabled else "disabled",
                            "outcome": "set", "ts": utcnow()}))
            self._conn.commit()
        except SchedulerRegistryError:
            raise
        except Exception:
            self._conn.rollback()
            raise
        return self.get_goal_ceiling_config(goal_id)


_GOAL_COLS = ["id", "description", "source", "status", "version", "strategy", "blockers",
              "progress_metadata", "last_evaluated_at", "last_replan_reason", "created_at", "updated_at"]
_CKPT_COLS = ["id", "task_id", "status", "step_index", "snapshot", "reason", "created_at"]


def _row_to_dict(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    if row is None:
        return {}
    return {c: v for c, v in zip(cols, row)}


def _approval_row(request: "ApprovalRequest") -> tuple[Any, ...]:
    return (
        request.approval_id, request.task_id, request.step_index, request.goal_id,
        request.capability, request.action, request.scope, request.risk,
        request.side_effects, request.resource_kind, request.resource, request.summary,
        request.status.value, request.requester_actor, json.dumps(request.actor_chain),
        json.dumps(request.params_keys), json.dumps(request.fingerprint),
        request.decision_actor, request.decided_at, request.expired_at,
        request.created_at, request.updated_at,
    )


def _approval_from_row(row: tuple[Any, ...]) -> "ApprovalRequest":
    from arion.state.approvals import ApprovalRequest

    d = {c: v for c, v in zip(SQLiteStorage._APPROVAL_COLS, row)}
    for key in ("actor_chain", "params_keys"):
        try:
            d[key] = json.loads(d[key])
        except (TypeError, json.JSONDecodeError):
            d[key] = []
    try:
        d["fingerprint"] = json.loads(d["fingerprint"])
    except (TypeError, json.JSONDecodeError):
        d["fingerprint"] = {}
    return ApprovalRequest.from_dict(d)


def _recovery_row(recovery: "MutationRecovery") -> tuple[Any, ...]:
    return (
        recovery.recovery_id, recovery.task_id, recovery.goal_id, recovery.step_index,
        recovery.capability, recovery.action, recovery.resource, recovery.reason,
        recovery.status.value, recovery.created_at, recovery.acknowledged_at,
        recovery.acknowledged_by,
    )


def _recovery_from_row(row: tuple[Any, ...]) -> "MutationRecovery":
    from arion.state.recovery import MutationRecovery

    d = {c: v for c, v in zip(SQLiteStorage._RECOVERY_COLS, row)}
    return MutationRecovery.from_dict(d)


def _lock_row(lock: "MutationLock") -> tuple[Any, ...]:
    return (
        lock.lock_id, lock.resource_kind, lock.resource, lock.capability,
        lock.action, lock.owner_id, lock.acquired_at, lock.expires_at,
    )


def _lock_from_row(row: tuple[Any, ...]) -> "MutationLock":
    from arion.state.locks import MutationLock

    d = {c: v for c, v in zip(SQLiteStorage._LOCK_COLS, row)}
    return MutationLock.from_dict(d)


def _waiter_row(waiter: "LockWaiter") -> tuple[Any, ...]:
    return (
        waiter.waiter_id, waiter.resource_kind, waiter.resource, waiter.task_id,
        waiter.goal_id, waiter.step_index, waiter.seq, waiter.enqueued_at,
        waiter.deadline, waiter.attempts, waiter.next_retry, waiter.status.value,
        waiter.created_at, waiter.updated_at,
    )


def _waiter_from_row(row: tuple[Any, ...]) -> "LockWaiter":
    from arion.state.locks import LockWaiter

    d = {c: v for c, v in zip(SQLiteStorage._WAITER_COLS, row)}
    return LockWaiter.from_dict(d)


def _goal_from_row(row: tuple[Any, ...]) -> Goal:
    d = {c: v for c, v in zip(_GOAL_COLS, row)}
    # legacy rows may lack the new columns (migration adds them with defaults,
    # but a row read before migration or with NULLs needs safe defaults)
    d.setdefault("version", 1)
    d.setdefault("strategy", None)
    d.setdefault("blockers", "[]")
    d.setdefault("progress_metadata", "{}")
    d.setdefault("last_evaluated_at", None)
    d.setdefault("last_replan_reason", None)
    d.setdefault("updated_at", d.get("created_at") or utcnow())
    try:
        d["blockers"] = json.loads(d["blockers"])
    except (TypeError, json.JSONDecodeError):
        d["blockers"] = []
    try:
        d["progress_metadata"] = json.loads(d["progress_metadata"])
    except (TypeError, json.JSONDecodeError):
        d["progress_metadata"] = {}
    return Goal.from_dict(d)


def _iso_plus(iso: str, seconds: float) -> str:
    """iso + seconds (aware/naive-safe), for scheduler lease deadlines."""
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _sys_work_from_row(row: tuple[Any, ...]) -> SchedulerWork:
    d = {c: v for c, v in zip(SQLiteStorage._SYS_COLS, row)}
    return SchedulerWork.from_dict(d)
