"""ADR-045: one live long-horizon runner per durable goal."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.locks import GOAL_RUN_RESOURCE_KIND
from arion.state.models import GoalStatus, PlanStep, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class _AppendOnce:
    name = "goal.append"
    description = "non-retry-safe append for goal ownership tests"
    actions = [ActionSpec(
        name="append",
        description="append",
        required_scope="goal:write",
        risk="low",
        side_effects="mutating",
        reversible=False,
        idempotent=False,
        retry_safe=False,
        resource_kind=FS,
        resource_param="path",
        param_schema={"path": {"type": "string", "required": True}},
        default_verification={"policy": "non_empty"},
    )]

    def __init__(self, effects: Path) -> None:
        self.effects = effects
        self.calls = 0
        self._lock = threading.Lock()

    def execute(self, action, params):
        with self._lock:
            self.calls += 1
            with self.effects.open("a", encoding="utf-8") as handle:
                handle.write(f"effect-{self.calls}\n")
            return {"appended": True, "call": self.calls}


class _BlockingPlanner:
    def __init__(self, *, block: bool = False, forbidden: bool = False) -> None:
        self.block = block
        self.forbidden = forbidden
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def required_capabilities(self, goal_description):
        return {"goal.append"}

    def plan(self, goal_description, task_id, registry, context=None):
        self.calls += 1
        if self.forbidden:
            raise AssertionError("contended goal runner invoked the planner")
        self.started.set()
        if self.block:
            self.release.wait(timeout=5)
        return [PlanStep(
            index=0,
            intent="append exactly once",
            capability="goal.append",
            action="append",
            scope="goal:write",
            params={"path": "same.txt"},
            verification=VerificationPolicy("non_empty"),
            max_attempts=1,
        )]


def _engine(db: Path, planner, capability, *, lease: float = 0.15):
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(capability)
    events = EventLogger(sinks=[storage])
    cognition = SQLiteCognitiveStore(db)
    world = WorldStateMonitor(cognition, sink=events)
    world.observe("registered_capabilities", sorted(registry.list()), source="system")
    manager = GoalManager(
        storage=storage,
        cognitive_store=cognition,
        events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world,
    )
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=events,
        policy=ResourcePolicy(
            allowed_scopes={"goal:write"},
            risk_deny=set(),
            risk_approve=set(),
            boundaries={FS: RelativePathBoundary()},
        ),
        goal_manager=manager,
        world_monitor=world,
        mutation_lock_lease_seconds=lease,
        scheduler_lease_seconds=lease,
        scheduler_max_lease_seconds=lease * 4,
        lock_wait_max_seconds=2,
        lock_wait_backoff_base=0,
        lock_wait_backoff_max=0,
    )
    return engine, manager, storage, cognition


def _close(engine, storage, cognition) -> None:
    try:
        engine.shutdown()
    finally:
        storage.close()
        cognition.close()


@pytest.mark.parametrize("bulk", [False, True])
def test_concurrent_same_goal_has_one_plan_task_and_effect(
    tmp_path: Path,
    bulk: bool,
) -> None:
    db = tmp_path / f"same-goal-{bulk}.db"
    effects = tmp_path / f"effects-{bulk}.log"
    capability = _AppendOnce(effects)
    owner_planner = _BlockingPlanner(block=True)
    contender_planner = _BlockingPlanner(forbidden=True)
    owner, owner_manager, owner_store, owner_cognition = _engine(
        db, owner_planner, capability, lease=0.12
    )
    contender, contender_manager, contender_store, contender_cognition = _engine(
        db, contender_planner, capability, lease=0.12
    )
    goal_id = owner.submit_goal("append exactly once").id
    owner_result: dict[str, object] = {}
    owner_errors: list[BaseException] = []

    def run_owner() -> None:
        try:
            owner_result["value"] = (
                owner.run_goals([goal_id])[goal_id]
                if bulk else owner.run_goal(goal_id)
            )
        except BaseException as exc:
            owner_errors.append(exc)

    worker = threading.Thread(target=run_owner)
    worker.start()
    assert owner_planner.started.wait(timeout=3)
    time.sleep(0.3)  # beyond the original lease; heartbeat must retain ownership

    contender_result = (
        contender.run_goals([goal_id])[goal_id]
        if bulk else contender.run_goal(goal_id)
    )
    assert contender_result.status is GoalStatus.ACTIVE
    assert contender_planner.calls == 0
    assert capability.calls == 0
    assert len([task for task in contender_store.list_tasks()
                if task.goal_id == goal_id]) == 1

    owner_planner.release.set()
    worker.join(timeout=10)
    assert not owner_errors
    assert owner_result["value"].status is GoalStatus.COMPLETED
    tasks = [task for task in owner_store.list_tasks()
             if task.goal_id == goal_id]
    assert len(tasks) == 1
    assert tasks[0].status.value == "completed"
    assert [plan["plan_version"]
            for plan in owner_manager.plan_history(goal_id)] == [1]
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    assert owner_store.list(
        resource_kind=GOAL_RUN_RESOURCE_KIND,
        resource=goal_id,
    ) == []
    _close(owner, owner_store, owner_cognition)
    _close(contender, contender_store, contender_cognition)


def test_expired_goal_run_owner_is_reclaimable(tmp_path: Path) -> None:
    db = tmp_path / "expired-goal-run.db"
    effects = tmp_path / "expired-effects.log"
    capability = _AppendOnce(effects)
    planner = _BlockingPlanner()
    engine, manager, storage, cognition = _engine(
        db, planner, capability, lease=0.1
    )
    goal_id = engine.submit_goal("recover expired goal run").id
    stale_store = SQLiteStorage(db)
    stale_store.acquire(
        GOAL_RUN_RESOURCE_KIND,
        goal_id,
        "orchestration.goal",
        "run",
        "dead-owner",
        lease_seconds=0.05,
    )
    time.sleep(0.08)

    result = engine.run_goal(goal_id)

    assert result.status is GoalStatus.COMPLETED
    assert capability.calls == 1
    assert len([task for task in storage.list_tasks()
                if task.goal_id == goal_id]) == 1
    assert storage.list(
        resource_kind=GOAL_RUN_RESOURCE_KIND,
        resource=goal_id,
    ) == []
    stale_store.close()
    _close(engine, storage, cognition)
