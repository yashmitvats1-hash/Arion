"""Deterministic ProgressEvaluator tests (ADR-016).

Evaluates completed/failed/skipped work, blockers, outstanding plan steps,
and world-state changes; returns progress/status/blockers/next_action/evidence.
"""

from arion.cognition.progress import DeterministicProgressEvaluator, ProgressResult
from arion.state.models import Goal, GoalStatus, PlanStep, StepStatus, Task, TaskStatus


def _goal(status="active", blockers=None):
    return Goal(id="goal_1", description="g", status=status, blockers=blockers or [])


def _task(status="completed", steps=None):
    return Task(id="task_x", goal_id="goal_1", description="t", status=TaskStatus(status),
                steps=steps or [])


def _step(index, status="succeeded"):
    return PlanStep(index=index, intent=f"s{index}", capability="filesystem.read",
                    action="read", scope="filesystem:read", params={"path": "README.md"},
                    status=StepStatus(status))


def _plan(step_count=2):
    return {"plan_version": 1, "strategy": "direct", "reason": "initial_plan",
            "plan_summary": [{"index": i} for i in range(step_count)]}


def test_terminal_goal_no_action():
    evaluator = DeterministicProgressEvaluator()
    for status in ("completed", "failed", "cancelled"):
        r = evaluator.evaluate(_goal(status=status), [], None)
        assert isinstance(r, ProgressResult)
        assert r.next_action == "none"


def test_paused_goal():
    r = DeterministicProgressEvaluator().evaluate(_goal(status="paused"), [], None)
    assert r.next_action == "paused"


def test_blocker_yields_blocked():
    goal = _goal(blockers=[{"key": "missing_capability", "detail": "x"}])
    r = DeterministicProgressEvaluator().evaluate(goal, [], None)
    assert r.status == "blocked"
    assert r.next_action == "resolve_blocker"
    assert r.blockers


def test_world_change_triggers_replan():
    from arion.cognition.world_state import WorldStateChange

    change = WorldStateChange(key="registered_capabilities", old_value=["filesystem.read"],
                              new_value=["filesystem.read", "shell.exec"], version=2, observed_at="now")
    r = DeterministicProgressEvaluator().evaluate(_goal(), [_task()], _plan(), [change])
    assert r.next_action == "replan"
    assert r.evidence["reason"] == "world_changed"
    assert r.evidence["world_change_keys"] == ["registered_capabilities"]


def test_failed_task_triggers_replan():
    r = DeterministicProgressEvaluator().evaluate(_goal(), [_task(status="failed")], _plan())
    assert r.next_action == "replan"
    assert r.evidence["reason"] == "task_failed"


def test_no_plan_yet_continue():
    r = DeterministicProgressEvaluator().evaluate(_goal(), [], None)
    assert r.next_action == "continue"
    assert r.evidence["reason"] == "initial_plan"


def test_all_work_complete():
    r = DeterministicProgressEvaluator().evaluate(
        _goal(), [_task(status="completed", steps=[_step(0), _step(1)])], _plan(2))
    assert r.next_action == "complete"
    assert r.progress == 1.0


def test_outstanding_work_continue():
    r = DeterministicProgressEvaluator().evaluate(
        _goal(), [_task(status="completed", steps=[_step(0)])], _plan(2))
    assert r.next_action == "continue"
    assert 0.0 < r.progress < 1.0


def test_skipped_steps_counted():
    r = DeterministicProgressEvaluator().evaluate(
        _goal(),
        [_task(status="completed", steps=[_step(0, status="skipped"), _step(1)])],
        _plan(2),
    )
    assert r.evidence["skipped"] == 1


def test_single_successful_task_does_not_complete_goal():
    """'Never infer goal completion from a single successful task': a plan
    with more steps than a single completed task leaves the goal incomplete."""
    r = DeterministicProgressEvaluator().evaluate(
        _goal(), [_task(status="completed", steps=[_step(0)])], _plan(3))
    assert r.next_action == "continue"
    assert r.progress < 1.0
