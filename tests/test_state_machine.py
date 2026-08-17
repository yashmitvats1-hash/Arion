"""State machine tests: lifecycle transitions, success and failure paths."""

from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy


def test_goal_to_completed_lifecycle(engine):
    task = engine.execute_goal("summarize this repository")

    assert task.status == TaskStatus.COMPLETED
    assert task.completed_at is not None
    assert len(task.steps) == 2  # list root + read key file
    assert all(s.status == StepStatus.SUCCEEDED for s in task.steps)
    # steps produced content
    assert task.steps[0].result["entries"]  # list root found entries
    assert "content" in task.steps[1].result

    # audit trail shows the full lifecycle; the goal event is global (pre-task)
    all_kinds = [k.kind for k in engine.storage.list_events()]
    assert all_kinds[0] == "goal.submitted"
    task_kinds = [k.kind for k in engine.storage.list_events(task.id)]
    assert task_kinds[0] == "task.created"


def test_status_progression_was_created_planned_running(engine, storage):
    # verify intermediate transitions are observable via persisted snapshots
    task = engine.execute_goal("summarize this repository")
    snapshots = storage.list_checkpoints(task.id)
    statuses = [c.status for c in snapshots]
    # plan checkpoint (planned), per-step checkpoints, final (completed)
    assert "planned" in statuses
    assert statuses[-1] == "completed"
    assert statuses[0] == "planned"


def test_task_fails_when_capability_missing(engine, storage):
    goal = engine.submit_goal("summarize this repository")
    task = engine.create_task(goal)
    # force a plan referencing a nonexistent capability
    task.steps = [
        PlanStep(index=0, intent="use missing", capability="nope.missing", action="run",
                 scope="filesystem:read", verification=VerificationPolicy("non_empty"))
    ]
    storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert "capability not found" in (result.error or "")
    events = [e.kind for e in storage.list_events(task.id)]
    assert "error" in events
    assert "task.failed" in events


def test_scope_spoofing_does_not_escalate(engine, storage):
    """Adversarial: a plan claims scope shell:exec while calling filesystem.read.

    Authorization uses the capability's ActionSpec scope (filesystem:read) as
    the source of truth, so spoofing the claimed scope cannot escalate to shell
    privileges - the action still runs under read-only, sandboxed permissions.
    """
    goal = engine.submit_goal("read the file")
    task = engine.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="spoof scope", capability="filesystem.read", action="read",
                 scope="shell:exec", params={"path": "README.md"}, verification=VerificationPolicy("non_empty"))
    ]
    storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert "content" in result.steps[0].result
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "permission.denied" not in kinds
    checked = [e for e in storage.list_events(task.id) if e.kind == "permission.checked"]
    assert checked[0].detail["scope"] == "filesystem:read"  # resolved from ActionSpec
    assert checked[0].detail["step_declared_scope"] == "shell:exec"  # the spoof is visible but ignored


def test_verification_failure_fails_task(engine, storage):
    goal = engine.submit_goal("read the file")
    task = engine.create_task(goal)
    # verification requires a key the capability will never produce
    task.steps = [
        PlanStep(
            index=0,
            intent="read file",
            capability="filesystem.read",
            action="read",
            scope="filesystem:read",
            params={"path": "README.md"},
            verification=VerificationPolicy("schema_keys", {"keys": ["nonexistent_key"]}),
            max_attempts=1,
        )
    ]
    storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert result.error == "verification failed"
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "verification.failed" in kinds
    # only one attempt configured: no retry event
    assert "step.retrying" not in kinds


def test_retry_then_success(engine, storage):
    from arion.capabilities.registry import ActionSpec, CapabilityError

    class FlakyCapability:
        name = "flaky.read"
        description = "fails the first attempt, then succeeds"
        actions = [
            ActionSpec(name="read", description="read", required_scope="filesystem:read",
                       risk="low", side_effects="read_only", retry_safe=True)
        ]

        def __init__(self):
            self.calls = 0

        def execute(self, action, params):
            self.calls += 1
            if self.calls == 1:
                raise CapabilityError("temporary failure")
            return {"content": "ok", "path": params.get("path")}

    engine.registry.register(FlakyCapability())
    goal = engine.submit_goal("read the file")
    task = engine.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="read", capability="flaky.read", action="read",
                 scope="filesystem:read", params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys", {"keys": ["content"]}))
    ]
    storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    step = result.steps[0]
    assert step.status == StepStatus.SUCCEEDED
    assert step.attempts == 2  # one failure + one success
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "step.retrying" in kinds
    assert "verification.passed" in kinds


def test_no_steps_planned_when_goal_undecomposable(engine):
    """A planner that cannot decompose the goal fails the task gracefully
    (never crashes the loop, never leaves the task in 'planning')."""
    goal = engine.submit_goal("do something utterly unplannable here")
    task = engine.create_task(goal)
    task.status = TaskStatus.PLANNING
    engine.storage.save_task(task)
    result = engine.run_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert "planning failed" in (result.error or "")
    assert "not decomposable" in (result.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "error" in kinds
    assert "task.failed" in kinds
