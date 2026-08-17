"""Persistence and restart/resume tests.

These prove the Definition of Done: a fresh process (a new engine instance on
the same DB) can resume persisted work without losing state.
"""

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


def test_task_state_survives_process_restart(tmp_path, sandbox, db_path, fresh_engine):
    """Engine A completes nothing; a NEW engine B resumes from the DB and completes."""
    engine_a = fresh_engine(db_path, sandbox)
    task = engine_a.execute_goal("summarize this repository")
    task_id = task.id
    engine_a.storage.close()

    # ---- "process restart" ----
    engine_b = fresh_engine(db_path, sandbox)
    resumed = engine_b.run_task(task_id)
    assert resumed.id == task_id
    assert resumed.status == TaskStatus.COMPLETED
    assert resumed.steps[1].result["content"]
    engine_b.storage.close()


def test_resume_does_not_replan(db_path, sandbox, storage, fresh_engine):
    """A task planned in process A resumes from checkpoint in B without a new plan."""
    engine_a = fresh_engine(db_path, sandbox)
    goal = engine_a.submit_goal("summarize this repository")
    task = engine_a.create_task(goal)
    task = engine_a._plan(task)  # plan + checkpoint only; "process dies" before executing
    task_id = task.id
    plan_events_before = len(engine_a.storage.list_checkpoints(task_id))
    engine_a.storage.close()

    engine_b = fresh_engine(db_path, sandbox)
    resumed = engine_b.run_task(task_id)
    kinds = [e.kind for e in engine_b.storage.list_events(task_id)]
    assert "task.resumed" in kinds
    # plan was produced exactly once (no re-planning after restart)
    assert kinds.count("plan.produced") == 1
    assert resumed.status == TaskStatus.COMPLETED
    assert len(engine_b.storage.list_checkpoints(task_id)) > plan_events_before
    engine_b.storage.close()


class SimulatedProcessCrash(BaseException):
    """A BaseException (not Exception) to simulate a hard process death.

    pytest special-cases KeyboardInterrupt, so we use our own BaseException:
    it propagates through the engine untouched (the engine only handles
    Exception), exactly like a crash would.
    """


# Shared across engine instances: the crash must happen exactly once in the
# whole test, because after a crash the resumed engine re-executes the same
# step (at-least-once semantics) and must then succeed.
_crash_calls = {"n": 0}


def test_crash_mid_execution_recovers(db_path, sandbox, fresh_engine):
    """Simulate a hard crash inside a capability call.

    The last checkpoint before the crash must persist, and a fresh engine must
    be able to resume and complete the work.
    """

    class CrashOnceCapability:
        name = "crash.once"
        description = "raises a hard exception on first call, works afterwards"
        actions = [
            ActionSpec(name="read", description="read", required_scope="filesystem:read",
                       risk="low", side_effects="read_only", retry_safe=True)
        ]

        def execute(self, action, params):
            _crash_calls["n"] += 1
            if _crash_calls["n"] == 1:
                raise SimulatedProcessCrash()  # simulates process death mid-step
            return {"content": "recovered", "path": params.get("path")}

    storage_a = SQLiteStorage(db_path)
    reg = CapabilityRegistry()
    reg.register(CrashOnceCapability())
    planner = DeterministicPlanner()
    router = DeterministicRouter(planner)
    events = EventLogger(sinks=[storage_a])
    engine_a = ArionEngine(storage=storage_a, registry=reg, planner=planner, router=router, events=events)

    goal = engine_a.submit_goal("summarize this repository")
    task = engine_a.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="read", capability="crash.once", action="read",
                 scope="filesystem:read", params={"path": "README.md"},
                 verification=VerificationPolicy("schema_keys", {"keys": ["content"]}))
    ]
    storage_a.save_task(task)
    engine_a._checkpoint(task, reason="plan produced")  # state before execution

    with pytest.raises(SimulatedProcessCrash):
        engine_a.run_task(task.id)

    # the task and its pre-crash checkpoint survive
    ckpts = storage_a.list_checkpoints(task.id)
    assert ckpts, "expected at least the pre-crash checkpoint to persist"
    storage_a.close()

    # ---- fresh process ----
    storage_b = SQLiteStorage(db_path)
    reg_b = CapabilityRegistry()
    reg_b.register(CrashOnceCapability())  # fresh instance: 2nd global call succeeds
    engine_b = ArionEngine(
        storage=storage_b,
        registry=reg_b,
        planner=planner,
        router=router,
        events=EventLogger(sinks=[storage_b]),
    )
    resumed = engine_b.run_task(task.id)
    assert resumed.status == TaskStatus.COMPLETED
    assert resumed.steps[0].result["content"] == "recovered"
    kinds = [e.kind for e in storage_b.list_events(task.id)]
    assert "task.resumed" in kinds
    # AT-LEAST-ONCE (ADR-010): the interrupted step was re-executed exactly once
    # more after the crash (1st global call crashed, 2nd call produced the result)
    assert _crash_calls["n"] == 2
    storage_b.close()


def test_goals_and_checkpoints_persist(sandbox, db_path, fresh_engine):
    engine = fresh_engine(db_path, sandbox)
    task = engine.execute_goal("summarize this repository")
    engine.storage.close()

    engine2 = fresh_engine(db_path, sandbox)
    goals = engine2.storage.list_goals()
    assert len(goals) == 1
    assert goals[0].id == task.goal_id
    ckpts = engine2.storage.list_checkpoints(task.id)
    assert len(ckpts) >= 3  # plan + per-step + completed
    assert engine2.storage.load_task(task.id).status == TaskStatus.COMPLETED
    engine2.storage.close()


def test_list_tasks_by_status(sandbox, db_path, fresh_engine):
    engine = fresh_engine(db_path, sandbox)
    engine.execute_goal("summarize this repository")
    engine.storage.close()
    engine2 = fresh_engine(db_path, sandbox)
    completed = engine2.storage.list_tasks(status="completed")
    assert len(completed) == 1
    assert engine2.storage.list_tasks(status="failed") == []
    engine2.storage.close()
