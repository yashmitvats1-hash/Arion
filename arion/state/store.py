"""State layer: persistence behind a Storage protocol.

SQLite-first implementation (ADR-003). The protocol is the contract; swapping
in Postgres or a vector store later must not change any other layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from arion.state.models import Checkpoint, Goal, Task, utcnow
from arion.observability.events import AuditEvent

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
"""


class Storage(Protocol):
    """Persistence contract used by orchestration and observability."""

    def save_goal(self, goal: Goal) -> None: ...
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


class SQLiteStorage:
    """Durable SQLite storage. WAL mode; full task snapshots as JSON rows."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._migrate_goals()
        self._migrate_approvals()
        self._conn.commit()

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

    def _migrate_approvals(self) -> None:
        """Lightweight additive migration: add the expiry column to
        approval_requests created before ADR-019. Never drops data (the
        queue record + audit trail stay intact)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(approval_requests)").fetchall()}
        if "expired_at" not in cols:
            self._conn.execute("ALTER TABLE approval_requests ADD COLUMN expired_at TEXT")

    # ---- goals ----

    def save_goal(self, goal: Goal) -> None:
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

    def load_goal(self, goal_id: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT " + ", ".join(_GOAL_COLS) + " FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        return _goal_from_row(row) if row else None

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

    def save_task(self, task: Task) -> None:
        task.updated_at = utcnow()
        self._conn.execute(
            "INSERT OR REPLACE INTO tasks (id, goal_id, description, status, snapshot, updated_at) VALUES (?,?,?,?,?,?)",
            (task.id, task.goal_id, task.description, task.status.value, json.dumps(task.to_dict()), task.updated_at),
        )
        self._conn.commit()

    def load_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT snapshot FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_dict(json.loads(row[0])) if row else None

    def list_tasks(self, status: str | None = None) -> list[Task]:
        if status:
            rows = self._conn.execute("SELECT snapshot FROM tasks WHERE status=? ORDER BY updated_at", (status,)).fetchall()
        else:
            rows = self._conn.execute("SELECT snapshot FROM tasks ORDER BY updated_at").fetchall()
        return [Task.from_dict(json.loads(r[0])) for r in rows]

    # ---- checkpoints ----

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

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT id, task_id, status, step_index, snapshot, reason, created_at FROM checkpoints"
            " WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return Checkpoint.from_dict(_row_to_dict(row, _CKPT_COLS)) if row else None

    def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        rows = self._conn.execute(
            "SELECT id, task_id, status, step_index, snapshot, reason, created_at FROM checkpoints"
            " WHERE task_id=? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        return [Checkpoint.from_dict(_row_to_dict(r, _CKPT_COLS)) for r in rows]

    # ---- audit events ----

    def append_event(self, event: AuditEvent) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (id, ts, task_id, step_id, kind, actor, success, detail) VALUES (?,?,?,?,?,?,?,?)",
            (event.id, event.ts, event.task_id, event.step_id, event.kind, event.actor, int(event.success), json.dumps(event.detail)),
        )
        self._conn.commit()

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
        return [AuditEvent.from_row(r) for r in rows]

    # ---- EventSink protocol (observability -> storage) ----

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

    def create_request(self, request: "ApprovalRequest") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO approval_requests "
            f"({', '.join(self._APPROVAL_COLS)}) VALUES ({', '.join('?' * len(self._APPROVAL_COLS))})",
            _approval_row(request),
        )
        self._conn.commit()

    def get_request(self, approval_id: str) -> "ApprovalRequest | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._APPROVAL_COLS)} FROM approval_requests WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        return _approval_from_row(row) if row else None

    def list_requests(self, status: str | None = None) -> list["ApprovalRequest"]:
        cols = ", ".join(self._APPROVAL_COLS)
        if status:
            rows = self._conn.execute(
                f"SELECT {cols} FROM approval_requests WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(f"SELECT {cols} FROM approval_requests ORDER BY created_at").fetchall()
        return [_approval_from_row(r) for r in rows]

    def update_request(self, request: "ApprovalRequest") -> None:
        request.updated_at = utcnow()
        self._conn.execute(
            "UPDATE approval_requests SET status=?, decision_actor=?, decided_at=?, "
            "summary=?, expired_at=?, updated_at=? WHERE approval_id=?",
            (request.status.value, request.decision_actor, request.decided_at,
             request.summary, request.expired_at, request.updated_at, request.approval_id),
        )
        self._conn.commit()

    def latest_request_for_step(self, task_id: str, step_index: int) -> "ApprovalRequest | None":
        row = self._conn.execute(
            f"SELECT {', '.join(self._APPROVAL_COLS)} FROM approval_requests"
            " WHERE task_id=? AND step_index=? ORDER BY rowid DESC LIMIT 1",
            (task_id, step_index),
        ).fetchone()
        return _approval_from_row(row) if row else None

    def close(self) -> None:
        self._conn.close()


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
