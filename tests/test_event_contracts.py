"""ADR-033: compatible typed event details and explicit sink policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arion.bootstrap import build_engine
from arion.observability.events import (
    AuditEvent,
    AuthorizationEventDetails,
    EventContractError,
    EventLogger,
    JsonlFileSink,
)
from arion.resource_identifiers import present_resource
from arion.state.store import SQLiteStorage


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _BrokenSink:
    def __init__(self, message: str = "sink unavailable") -> None:
        self.message = message
        self.calls = 0

    def emit(self, event: AuditEvent) -> None:
        self.calls += 1
        raise OSError(self.message)


def _decision(**overrides) -> dict:
    value = {
        "outcome": "deny",
        "reason": "resource outside boundary",
        "scope": "filesystem:read",
        "resource": "private.txt",
        "resource_kind": "filesystem:path",
        "risk": "low",
        "side_effects": "read_only",
    }
    value.update(overrides)
    return value


def test_raw_dictionary_details_remain_accepted_and_are_snapshotted() -> None:
    detail = {"count": 1, "nested": {"enabled": True}}

    event = AuditEvent(kind="task.created", detail=detail)
    detail["count"] = 2
    detail["nested"]["enabled"] = False

    assert event.detail == {"count": 1, "nested": {"enabled": True}}
    assert isinstance(event.detail, dict)


def test_invalid_event_details_fail_at_construction() -> None:
    with pytest.raises(EventContractError, match="mapping or EventDetails"):
        AuditEvent(kind="task.created", detail=["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(EventContractError, match="string keys"):
        AuditEvent(kind="task.created", detail={1: "not a string"})  # type: ignore[dict-item]
    with pytest.raises(EventContractError, match="JSON serializable"):
        AuditEvent(kind="task.created", detail={"bad": object()})


def test_authorization_details_validate_and_normalize_to_stable_shape() -> None:
    details = AuthorizationEventDetails.from_mapping(
        _decision(outcome="allow", reason="allowed", resource="README.md"),
        actor="agent:arion",
        actor_chain=("user:alice", "agent:arion"),
        param_keys=("path",),
        step_declared_scope="filesystem:write",
        revalidated_after_lock_wait=True,
    )

    event = AuditEvent(kind="permission.checked", detail=details)  # type: ignore[arg-type]
    presentation = present_resource("filesystem:path", "README.md")

    assert event.detail == {
        "schema_version": 2,
        "outcome": "allow",
        "reason": "allowed",
        "scope": "filesystem:read",
        "resource": "README.md",
        "resource_kind": "filesystem:path",
        "resource_fingerprint": presentation.fingerprint,
        "resource_redacted": False,
        "risk": "low",
        "side_effects": "read_only",
        "actor": "agent:arion",
        "actor_chain": ["user:alice", "agent:arion"],
        "param_keys": ["path"],
        "step_declared_scope": "filesystem:write",
        "revalidated_after_lock_wait": True,
    }
    assert "params" not in event.detail


@pytest.mark.parametrize("field,value", [
    ("outcome", "invented"),
    ("reason", ""),
    ("scope", ""),
    ("resource", 123),
])
def test_authorization_details_reject_invalid_stable_fields(field: str, value) -> None:
    with pytest.raises(EventContractError):
        AuthorizationEventDetails.from_mapping(_decision(**{field: value}))


def test_typed_details_round_trip_through_existing_sqlite_schema(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "events.db")
    event = AuditEvent(
        kind="permission.denied",
        task_id="task-1",
        step_id="step_0",
        success=False,
        detail=AuthorizationEventDetails.from_mapping(_decision()),  # type: ignore[arg-type]
    )

    storage.append_event(event)
    loaded = storage.list_events("task-1")

    assert len(loaded) == 1
    assert loaded[0].to_dict() == event.to_dict()
    assert loaded[0].detail["schema_version"] == 2
    storage.close()


def test_legacy_sqlite_dictionary_payload_remains_readable(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "legacy.db")
    legacy_detail = {
        "scope": "filesystem:read",
        "resource": "README.md",
        "reason": "legacy decision without outcome or schema version",
        "legacy_extension": {"kept": True},
    }
    storage._conn.execute(
        "INSERT INTO audit_events "
        "(id, ts, task_id, step_id, kind, actor, success, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "evt_legacy",
            "2026-01-01T00:00:00+00:00",
            "task-legacy",
            "step_0",
            "permission.denied",
            "system",
            0,
            json.dumps(legacy_detail),
        ),
    )
    storage._conn.commit()

    loaded = storage.list_events("task-legacy")[0]

    assert loaded.detail == legacy_detail
    assert "schema_version" not in loaded.detail
    storage.close()


def test_jsonl_format_and_legacy_dictionary_adapter_remain_compatible(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    event = AuditEvent(
        kind="approval.requested",
        task_id="task-1",
        detail=AuthorizationEventDetails.from_mapping(
            _decision(outcome="require_approval", reason="medium risk")
        ),  # type: ignore[arg-type]
    )
    JsonlFileSink(path).emit(event)

    raw = json.loads(path.read_text(encoding="utf-8").strip())
    restored = AuditEvent.from_dict(raw)
    legacy = AuditEvent.from_dict({
        "kind": "task.created",
        "task_id": "task-old",
        "detail": {"goal_id": "goal-old"},
    })

    assert raw["detail"] == event.detail
    assert restored.to_dict() == event.to_dict()
    assert legacy.kind == "task.created"
    assert legacy.task_id == "task-old"
    assert legacy.detail == {"goal_id": "goal-old"}


def test_best_effort_sink_failure_is_isolated_and_delivery_continues() -> None:
    broken = _BrokenSink()
    capture = _CaptureSink()
    logger = EventLogger()
    logger.add_sink(broken, required=False)
    logger.add_sink(capture)
    event = AuditEvent(kind="task.created")

    logger.emit(event)

    assert capture.events == [event]
    assert broken.calls == 1
    assert len(logger.last_failures) == 1
    failure = logger.last_failures[0]
    assert failure.required is False
    assert failure.sink == "_BrokenSink"
    assert failure.error_type == "OSError"
    assert failure.event_id == event.id


def test_required_sink_failure_retains_fail_fast_behavior() -> None:
    broken = _BrokenSink("required audit unavailable")
    capture = _CaptureSink()
    logger = EventLogger(sinks=[broken, capture])

    with pytest.raises(OSError, match="required audit unavailable"):
        logger.emit(AuditEvent(kind="task.created"))

    assert broken.calls == 1
    assert capture.events == []
    assert logger.last_failures[0].required is True


def test_broken_jsonl_mirror_does_not_break_durable_engine_audit(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# Test repository\n", encoding="utf-8")
    # Opening a directory as an append-only file fails on every mirror write,
    # while construction can still register the sink.
    broken_jsonl = tmp_path / "mirror.jsonl"
    broken_jsonl.mkdir()

    engine = build_engine(
        tmp_path / "arion.db",
        sandbox,
        jsonl_log=broken_jsonl,
    )
    task = engine.execute_goal("summarize this repository")

    assert task.status.value == "completed"
    assert "task.completed" in [
        event.kind for event in engine.storage.list_events(task.id)
    ]
    assert engine.events.last_failures
    assert engine.events.last_failures[0].sink == "JsonlFileSink"
    assert engine.events.last_failures[0].required is False
    engine.shutdown()


def test_core_permission_event_uses_contract_and_never_persists_param_values(
    engine, storage,
) -> None:
    task = engine.execute_goal("summarize this repository")

    checked = next(
        event for event in storage.list_events(task.id)
        if event.kind == "permission.checked"
    )

    assert checked.detail["schema_version"] == 2
    assert checked.detail["outcome"] == "allow"
    assert checked.detail["scope"] == "filesystem:read"
    assert checked.detail["param_keys"] == ["path"]
    assert "params" not in checked.detail
