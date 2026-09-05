"""ADR-061 C3: AuthorizationRequest carries the complete resource set.

Covers D1, D10 and invariants 1, 16, 22 (carriage), plus R-A: the durable
projection must survive REAL storage rehydration with role ORDER and role
IDENTITY intact - not merely the entry count.

C3 CARRIES the set; it does not enforce it. Boundary checking over the
complete set is C4 and is deliberately NOT asserted here.
"""

import tempfile
from pathlib import Path

import pytest

from arion.capabilities.registry import ActionSpec, ResourceRole
from arion.orchestration.authz import Actor, AuthorizationRequest
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, Task
from arion.state.store import SQLiteStorage


def _move_spec():
    return ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        risk="high", side_effects="mutating", retry_safe=False,
        param_schema={"source": {"type": "string", "required": True},
                      "dest": {"type": "string", "required": True}},
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )


def _read_spec():
    return ActionSpec(
        name="read", description="d", required_scope="filesystem:read",
        resource_kind="filesystem:path", resource_param="path",
    )


def _request(spec, params):
    from arion.orchestration.resource_set import resolve_resources
    return AuthorizationRequest(
        actor=Actor(kind="user", name="tester"),
        task_id="task-1", step_index=0,
        capability="filesystem.move", action="move",
        scope=spec.required_scope, params=dict(params),
        resource=ArionEngine._extract_resource(spec, params),
        resource_kind=spec.resource_kind,
        risk=spec.risk, side_effects=spec.side_effects,
        resources=resolve_resources(spec, params),
    )


# --------------------------------------------------------------------------
# D1 - the set is carried, and agrees with the singular fields
# --------------------------------------------------------------------------

def test_request_carries_complete_ordered_set():
    req = _request(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    assert [r.role for r in req.resources] == ["source", "dest"]
    assert [r.value for r in req.resources] == ["README.md", "ARCHIVE.md"]


def test_singular_fields_carry_the_primary_role():
    """invariant 16 - existing single-resource readers keep working."""
    req = _request(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    assert req.resource == "README.md"
    assert req.resource_kind == "filesystem:path"


def test_singular_and_set_cannot_disagree():
    """Both derive from the same resolve_resources call (D1)."""
    req = _request(_move_spec(), {"source": "a.txt", "dest": "b.txt"})
    assert req.resource == req.resources[0].value


def test_single_resource_request_has_one_element_set():
    req = _request(_read_spec(), {"path": "notes.md"})
    assert len(req.resources) == 1
    assert req.resources[0].role == "path"
    assert req.resource == "notes.md"


def test_resourceless_request_has_empty_set():
    spec = ActionSpec(name="now", description="d", required_scope="clock:read")
    req = _request(spec, {})
    assert req.resources == []
    assert req.resource is None


def test_default_is_empty_list_not_shared():
    a = AuthorizationRequest(actor=Actor(kind="user", name="u"), task_id="t",
                             step_index=0, capability="c", action="a",
                             scope="s", params={})
    b = AuthorizationRequest(actor=Actor(kind="user", name="u"), task_id="t",
                             step_index=1, capability="c", action="a",
                             scope="s", params={})
    assert a.resources == [] and b.resources == []
    assert a.resources is not b.resources


# --------------------------------------------------------------------------
# R-A - REAL storage round-trip, not obj -> to_dict -> from_dict
# --------------------------------------------------------------------------

def _roundtrip_task_with_record(request) -> dict:
    """Persist a task carrying the approval record, then rehydrate from disk."""
    engine = ArionEngine.__new__(ArionEngine)  # projection helper only
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "c3.db")
        store = SQLiteStorage(db)
        task = Task(id="task-1", goal_id="goal-1", description="move test")
        step = PlanStep(index=0, intent="move a file", scope="filesystem:write",
                        capability="filesystem.move", action="move",
                        params=dict(request.params))
        ArionEngine._append_approval_record(
            engine, task, step, request,
            _decision(), outcome="approved", actor="user:tester",
        )
        store.save_task(task)

        # rehydrate through a SEPARATE store instance on the same file, so the
        # in-memory object cannot mask a serialization defect
        reloaded = SQLiteStorage(db).load_task("task-1")
        assert reloaded is not None
        return reloaded.approvals[-1]


def _decision():
    from arion.orchestration.authz import PolicyDecision, PolicyOutcome
    return PolicyDecision(outcome=PolicyOutcome.ALLOW, reason="allowed",
                          scope="filesystem:write", resource="README.md",
                          resource_kind="filesystem:path")


def test_persisted_resource_set_survives_rehydration():
    req = _request(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    record = _roundtrip_task_with_record(req)
    persisted = record["request"]["resources"]
    assert len(persisted) == 2


def test_rehydrated_role_ORDER_survives():
    """A swap preserving the count would be semantic corruption."""
    req = _request(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    record = _roundtrip_task_with_record(req)
    persisted = record["request"]["resources"]
    assert [r["role"] for r in persisted] == ["source", "dest"]
    assert persisted[0]["resource"] == "README.md"
    assert persisted[1]["resource"] == "ARCHIVE.md"


def test_rehydrated_role_IDENTITY_survives():
    """Role names must bind to their own values after a round-trip."""
    req = _request(_move_spec(), {"source": "one.md", "dest": "two.md"})
    record = _roundtrip_task_with_record(req)
    by_role = {r["role"]: r["resource"] for r in record["request"]["resources"]}
    assert by_role == {"source": "one.md", "dest": "two.md"}


def test_source_dest_swap_is_detectable_after_rehydration():
    """Guard the guard: the assertion above would catch an actual swap."""
    a = _roundtrip_task_with_record(
        _request(_move_spec(), {"source": "one.md", "dest": "two.md"}))
    b = _roundtrip_task_with_record(
        _request(_move_spec(), {"source": "two.md", "dest": "one.md"}))
    ra = [r["resource"] for r in a["request"]["resources"]]
    rb = [r["resource"] for r in b["request"]["resources"]]
    assert ra == ["one.md", "two.md"]
    assert rb == ["two.md", "one.md"]
    assert ra != rb


def test_persisted_values_go_through_present_resource():
    """invariant 7 - presentation metadata is present per role."""
    req = _request(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    record = _roundtrip_task_with_record(req)
    for entry in record["request"]["resources"]:
        assert "resource_fingerprint" in entry
        assert "resource_redacted" in entry
        assert entry["resource_kind"] == "filesystem:path"


def test_persisted_value_is_as_declared_not_canonical():
    """invariant 20 - canonicalization never rewrites what a human reads."""
    req = _request(_move_spec(), {"source": "./README.md", "dest": "ARCHIVE.md"})
    record = _roundtrip_task_with_record(req)
    assert record["request"]["resources"][0]["resource"] == "./README.md"


def test_duplicate_values_persist_as_two_roles():
    """invariant 4 - move a -> a keeps both roles durably."""
    req = _request(_move_spec(), {"source": "a.txt", "dest": "a.txt"})
    record = _roundtrip_task_with_record(req)
    assert [r["role"] for r in record["request"]["resources"]] == ["source", "dest"]


def test_single_resource_record_keeps_historical_shape():
    """invariant 16 - existing keys unchanged; new key is additive."""
    req = _request(_read_spec(), {"path": "notes.md"})
    record = _roundtrip_task_with_record(req)
    r = record["request"]
    assert r["resource"] == "notes.md"          # historical singular key
    assert r["resource_kind"] == "filesystem:path"
    assert "params_keys" in r
    assert len(r["resources"]) == 1             # additive


def test_unresolved_role_persists_as_null_not_dropped():
    """A missing dest must remain VISIBLE, never silently disappear."""
    req = _request(_move_spec(), {"source": "README.md"})
    record = _roundtrip_task_with_record(req)
    persisted = record["request"]["resources"]
    assert [r["role"] for r in persisted] == ["source", "dest"]
    assert persisted[1]["resource"] is None
