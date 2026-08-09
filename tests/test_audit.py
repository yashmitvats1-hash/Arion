"""Audit/observability tests: event vocabulary, ordering, persistence, JSONL."""

import json

from arion.observability.events import AuditEvent, EVENT_KINDS, EventLogger, JsonlFileSink
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy

# Events that must appear scoped to the task itself
TASK_REQUIRED_KINDS = [
    "task.created",
    "task.planning",
    "plan.produced",
    "step.started",
    "permission.checked",
    "capability.discovered",
    "capability.executed",
    "observation.recorded",
    "verification.passed",
    "checkpoint.persisted",
    "task.completed",
]

ALL_REQUIRED_KINDS = TASK_REQUIRED_KINDS + ["goal.submitted"]


def test_successful_run_emits_full_lifecycle(engine, storage):
    task = engine.execute_goal("summarize this repository")
    events = storage.list_events(task.id)
    kinds = [e.kind for e in events]
    for expected in TASK_REQUIRED_KINDS:
        assert expected in kinds, f"missing event {expected}"
    # goal.submitted is global (predates task creation): visible in the full trail
    all_kinds = [e.kind for e in storage.list_events()]
    assert "goal.submitted" in all_kinds
    # order sanity: permission before execution, execution before verification
    assert kinds.index("permission.checked") < kinds.index("capability.executed")
    assert kinds.index("capability.executed") < kinds.index("verification.passed")
    # every task-scoped event is structured and tagged
    for event in events:
        assert event.task_id == task.id
        assert isinstance(event.detail, dict)
        assert event.success in (True, False)


def test_events_persist_across_restart(engine, storage, sandbox, db_path, fresh_engine):
    task = engine.execute_goal("summarize this repository")
    engine.storage.close()

    engine2 = fresh_engine(db_path, sandbox)
    events = engine2.storage.list_events(task.id)
    assert len(events) >= len(TASK_REQUIRED_KINDS)
    assert "task.completed" in [e.kind for e in events]
    engine2.storage.close()


def test_jsonl_sink(tmp_path):
    sink = JsonlFileSink(tmp_path / "events.jsonl")
    logger = EventLogger(sinks=[sink])
    logger.emit(AuditEvent(kind="task.created", task_id="t1", detail={"n": 1}))
    logger.emit(AuditEvent(kind="task.completed", task_id="t1"))
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "task.created"
    assert first["task_id"] == "t1"
    assert first["detail"] == {"n": 1}


def test_unknown_kind_rejected():
    import pytest

    with pytest.raises(ValueError):
        AuditEvent(kind="not.a.kind")
    # vocabulary is explicit and finite
    assert "task.completed" in EVENT_KINDS
    assert "permission.denied" in EVENT_KINDS


def test_event_filtering_by_task(engine, storage):
    t1 = engine.execute_goal("summarize this repository")
    t2 = engine.execute_goal("summarize this repository")
    events_t1 = storage.list_events(t1.id)
    assert all(e.task_id == t1.id for e in events_t1)
    events_t2 = storage.list_events(t2.id)
    assert all(e.task_id == t2.id for e in events_t2)
    assert len(events_t1) == len(events_t2)
