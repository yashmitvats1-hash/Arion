"""Approval seam tests (ADR-009): ALLOW / DENY / REQUIRE_APPROVAL.

The engine pauses a task at REQUIRE_APPROVAL with status awaiting_approval,
checkpoints it, and resumes from the exact same step once approved - without
any GUI. The ApprovalHandler protocol is the seam a future approval interface
implements.
"""

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    AutoApproveHandler,
    AutoDenyHandler,
    PendingApprovalHandler,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


class MediumRiskCapability:
    """An action that requires approval under the default policy (risk=medium)."""

    name = "medium.tool"
    description = "a medium-risk action"
    actions = [
        ActionSpec(name="run", description="run", required_scope="medium:run",
                   risk="medium", side_effects="mutating", reversible=True,
                   idempotent=False, retry_safe=False)
    ]

    def execute(self, action, params):
        return {"content": "medium done", "path": params.get("path")}


def _build_engine(db_path, approval_handler=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(MediumRiskCapability())
    policy = ResourcePolicy(allowed_scopes={"medium:run"})  # default: medium -> REQUIRE_APPROVAL
    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=DeterministicPlanner(),
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=policy,
        approval_handler=approval_handler,
    )


def _step():
    return PlanStep(index=0, intent="run medium", capability="medium.tool", action="run",
                    scope="medium:run", params={"path": "x"},
                    verification=VerificationPolicy("non_empty"))


def test_require_approval_pauses_task(db_path):
    """Pending handler: the task pauses as awaiting_approval with a checkpoint."""
    engine = _build_engine(db_path)  # default PendingApprovalHandler
    goal = engine.submit_goal("do medium thing")
    task = engine.create_task(goal)
    task.steps = [_step()]
    engine.storage.save_task(task)

    result = engine.run_task(task.id)

    assert result.status == TaskStatus.AWAITING_APPROVAL
    assert result.steps[0].status == StepStatus.PENDING
    # checkpointed so the pause survives restarts
    assert engine.storage.latest_checkpoint(task.id) is not None
    assert engine.storage.latest_checkpoint(task.id).reason == "awaiting approval"
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "approval.requested" in kinds
    assert "approval.granted" not in kinds
    assert "approval.denied" not in kinds
    # the capability was never executed
    assert result.steps[0].result is None


def test_approval_then_resume_completes(db_path):
    """Approve the pause, then resume: same step runs to completion.

    Simulates a human approval interface answering between two run_task calls
    (the approval seam).
    """
    engine = _build_engine(db_path, PendingApprovalHandler())
    goal = engine.submit_goal("do medium thing")
    task = engine.create_task(goal)
    task.steps = [_step()]
    engine.storage.save_task(task)
    task_id = task.id

    engine.run_task(task_id)  # pauses awaiting approval

    # approval interface answers; a fresh handler grants approval on resume
    engine2 = _build_engine(db_path, AutoApproveHandler())
    resumed = engine2.run_task(task_id)

    assert resumed.status == TaskStatus.COMPLETED
    assert resumed.steps[0].status == StepStatus.SUCCEEDED
    assert resumed.steps[0].result["content"] == "medium done"
    kinds = [e.kind for e in engine2.storage.list_events(task_id)]
    assert "approval.requested" in kinds
    assert "approval.granted" in kinds
    # paused task resumed from the same step, not replanned
    assert kinds.count("plan.produced") == 0


def test_approval_denied_fails_task(db_path):
    engine = _build_engine(db_path, AutoDenyHandler())
    goal = engine.submit_goal("do medium thing")
    task = engine.create_task(goal)
    task.steps = [_step()]
    engine.storage.save_task(task)

    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert result.error == "approval denied"
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "approval.requested" in kinds
    assert "approval.denied" in kinds


def test_approval_events_persist_across_restart(db_path):
    engine = _build_engine(db_path)
    goal = engine.submit_goal("do medium thing")
    task = engine.create_task(goal)
    task.steps = [_step()]
    engine.storage.save_task(task)
    task_id = task.id
    engine.run_task(task_id)  # pauses
    engine.storage.close()

    # fresh process: events preserved, task resumable to completion
    engine2 = _build_engine(db_path, AutoApproveHandler())
    events = engine2.storage.list_events(task_id)
    assert "approval.requested" in [e.kind for e in events]
    assert engine2.storage.load_task(task_id).status == TaskStatus.AWAITING_APPROVAL
    resumed = engine2.run_task(task_id)
    assert resumed.status == TaskStatus.COMPLETED
