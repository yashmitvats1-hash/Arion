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
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
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
"""


class Storage(Protocol):
    """Persistence contract used by orchestration and observability."""

    def save_goal(self, goal: Goal) -> None: ...
    def load_goal(self, goal_id: str) -> Goal | None: ...
    def list_goals(self) -> list[Goal]: ...

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
        self._conn.commit()

    # ---- goals ----

    def save_goal(self, goal: Goal) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goals (id, description, source, status, created_at) VALUES (?,?,?,?,?)",
            (goal.id, goal.description, goal.source, goal.status, goal.created_at),
        )
        self._conn.commit()

    def load_goal(self, goal_id: str) -> Goal | None:
        row = self._conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return Goal.from_dict(_row_to_dict(row, _GOAL_COLS)) if row else None

    def list_goals(self) -> list[Goal]:
        rows = self._conn.execute("SELECT * FROM goals ORDER BY created_at").fetchall()
        return [Goal.from_dict(_row_to_dict(r, _GOAL_COLS)) for r in rows]

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

    def close(self) -> None:
        self._conn.close()


_GOAL_COLS = ["id", "description", "source", "status", "created_at"]
_CKPT_COLS = ["id", "task_id", "status", "step_index", "snapshot", "reason", "created_at"]


def _row_to_dict(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    if row is None:
        return {}
    return {c: v for c, v in zip(cols, row)}
