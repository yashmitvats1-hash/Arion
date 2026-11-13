"""Recovery acknowledgement vs. fresh authorization (issue #23, pass 3).

``acknowledge_recovery`` ONLY records "the previous failed mutation has
been handled" and unblocks the goal (ADR-020 / ADR-043).  It is NOT
authorization:

- it cannot execute a capability and cannot grant authorization;
- it cannot reuse or resurrect an approval - every later execution attempt
  is a NEW task that solicits a FRESH approval decision through the live
  authorization pipeline;
- a denied fresh approval is honored: the post-ack mutation does not run.

These tests pin that separation end-to-end through the approval seam.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.append import FilesystemAppendCapability
from arion.orchestration.authz import (
    ApprovalOutcome,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.state.models import PlanStep, VerificationPolicy

FS = "filesystem:path"

OFFLINE_MODEL_CONFIG = SimpleNamespace(
    enabled=False, fallback_enabled=False, reflection_enabled=False
)


class _SimulatedCrash(BaseException):
    """Hard-death model: bypasses all ``except Exception`` handlers."""


class _CrashAfterAppend(FilesystemAppendCapability):
    """Applies the side effect, then dies - the F10 crash window."""

    def __init__(self, sandbox, crash_on: int = 1):
        super().__init__(sandbox)
        self.calls = 0
        self.crash_on = crash_on

    def execute(self, action, params):
        self.calls += 1
        result = super().execute(action, params)
        if self.calls == self.crash_on:
            raise _SimulatedCrash()
        return result


class _CountingApproval:
    """Records every approval decision solicited; approves or denies on
    demand.  ``requests`` is the audit trail for fresh-authorization
    assertions."""

    def __init__(self, approve: bool = True, approve_first: int = 1):
        self.requests: list[tuple[str, int]] = []
        self.approve = approve
        self.approve_first = approve_first

    def request(self, request, decision):
        self.requests.append((request.task_id, request.step_index))
        if len(self.requests) <= self.approve_first:
            return ApprovalOutcome.APPROVED
        return ApprovalOutcome.APPROVED if self.approve else ApprovalOutcome.DENIED


class _FixedPlanner:
    last_source = "deterministic"
    last_transformation = None

    def __init__(self, steps, required):
        self._steps = steps
        self._required = set(required)

    def plan(self, goal_description, task_id, registry, context=None):
        return copy.deepcopy(self._steps)

    def required_capabilities(self, goal_description):
        return set(self._required)


def _append_step():
    return PlanStep(
        index=0, intent="append suffix", capability="filesystem.append",
        action="append", scope="filesystem:write",
        params={"path": "journal.txt", "content": "X", "create": True},
        verification=VerificationPolicy("non_empty"),
    )


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:write"},
        boundaries={FS: RelativePathBoundary()},
        risk_deny=set(),
        risk_approve={"high"},  # filesystem.append is risk=high -> approval
    )


def _build(tmp_path, capability, approval, db_name="arion.db"):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    planner = _FixedPlanner([_append_step()], {"filesystem.append"})
    engine = build_engine(
        tmp_path / db_name, sandbox, planner=planner, policy=_policy(),
        approval_handler=approval, model_config=OFFLINE_MODEL_CONFIG,
    )
    engine.registry.register(capability)
    return engine, sandbox


def _crash_and_fence(tmp_path, approval):
    """Run a crash-window mutation to completion of its FIRST attempt
    (approval solicited), die after the side effect, then resume into the
    in-doubt fence (recovery record created).  Returns
    (engine, sandbox, goal, crashed_task, recovery)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    capability = _CrashAfterAppend(sandbox, crash_on=1)
    engine, sandbox = _build(tmp_path, capability, approval)
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    goal = engine.submit_goal("append once")
    with pytest.raises(_SimulatedCrash):
        engine.run_goal(goal.id)
    task = engine.goal_manager.task_history(goal.id)[-1]
    engine.close()

    resumed_capability = _CrashAfterAppend(sandbox, crash_on=-1)
    resumed, sandbox = _build(tmp_path, resumed_capability, approval)
    fenced = resumed.run_task(task.id)
    assert fenced.status.value == "failed"
    recoveries = resumed.recovery_store.list_recoveries(task_id=task.id)
    assert len(recoveries) == 1
    return resumed, sandbox, goal, task, recoveries[0]


# --------------------------------------------------------------------- #
# 1. fresh approval is solicited for the post-ack execution
# --------------------------------------------------------------------- #

def test_fresh_approval_solicited_per_execution_attempt(tmp_path):
    approval = _CountingApproval(approve=True)
    engine, sandbox, goal, crashed_task, recovery = _crash_and_fence(
        tmp_path, approval,
    )
    try:
        engine.acknowledge_recovery(recovery.recovery_id, actor="operator")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"

        task_ids = [t for t, _ in approval.requests]
        # one approval decision for the crashed attempt, one for the
        # post-ack re-execution: the approval was NOT carried over.
        assert len(task_ids) == 2
        assert task_ids[0] == crashed_task.id
        assert task_ids[1] != crashed_task.id
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# 2. acknowledge_recovery grants no authorization of its own
# --------------------------------------------------------------------- #

def test_acknowledge_recovery_grants_no_authorization(tmp_path):
    approval = _CountingApproval(approve=True)
    engine, sandbox, goal, crashed_task, recovery = _crash_and_fence(
        tmp_path, approval,
    )
    try:
        requests_before = list(approval.requests)
        content_before = (sandbox / "journal.txt").read_text(encoding="utf-8")

        acked = engine.acknowledge_recovery(recovery.recovery_id, actor="operator")
        assert acked.status.value == "acknowledged"

        # the acknowledgement solicited no approval, executed nothing and
        # changed no file: it only records handling and unblocks the goal.
        assert approval.requests == requests_before
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == content_before
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# 3. a denied fresh approval is honored (no approval resurrection)
# --------------------------------------------------------------------- #

def test_denied_fresh_approval_is_honored_post_ack(tmp_path):
    approval = _CountingApproval(approve=False, approve_first=1)
    engine, sandbox, goal, crashed_task, recovery = _crash_and_fence(
        tmp_path, approval,
    )
    try:
        content_after_crash = (sandbox / "journal.txt").read_text(encoding="utf-8")
        engine.acknowledge_recovery(recovery.recovery_id, actor="operator")
        final = engine.run_goal(goal.id)

        assert final.status.value != "completed"
        # a fresh approval decision WAS solicited (2nd request) and its
        # denial was honored: the post-ack mutation never executed.
        assert len(approval.requests) == 2
        assert approval.requests[1][0] != crashed_task.id
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == content_after_crash
    finally:
        engine.close()
