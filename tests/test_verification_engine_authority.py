"""M7-A end-to-end: the ENGINE is the authoritative normalization point.

ADR-060 D4. `PlanValidator` is instantiated at exactly one site
(`model_planner.py`), so `DeterministicPlanner` and stored-plan rehydration
never traverse it. Enforcing verification authority only in the validator
would leave unprotected the very path that produced the audit's false-success
defect. These tests drive the real engine with a planner that proposes WEAK
verification for a mutation and assert the engine corrects it anyway.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve=set(),  # auto-approve: we are testing verification, not approval
        boundaries={FS: RelativePathBoundary()},
    )


class _WeakVerificationPlanner:
    """Proposes a MUTATION verified only by `non_empty` - the audit's bug shape."""

    def __init__(self, policy=VerificationPolicy("non_empty")):
        self.policy = policy
        self.last_transformation = None
        self.last_source = "deterministic"

    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(
                index=0, intent="write file", capability="filesystem.write",
                action="write", scope="filesystem:write",
                params={"path": "out.txt", "content": "hello", "overwrite": True},
                verification=self.policy,
            )
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


class _UndeclaredMutation:
    """A mutating capability whose ActionSpec declares NO default_verification."""

    name = "custom.mutate"
    description = "mutating capability with no declared verification"
    actions = [
        ActionSpec(
            name="mutate", description="mutate something",
            required_scope="filesystem:write", risk="low",
            side_effects="mutating", retry_safe=True,
            default_verification=None,
        )
    ]

    def execute(self, action, params):
        return {"did_something": True}


class _UndeclaredPlanner:
    def __init__(self, policy):
        self.policy = policy
        self.last_transformation = None
        self.last_source = "deterministic"

    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(
                index=0, intent="mutate", capability="custom.mutate",
                action="mutate", scope="filesystem:write", params={},
                verification=self.policy,
            )
        ]

    def required_capabilities(self, goal_description):
        return {"custom.mutate"}


def _engine(db_path, sandbox, planner, extra=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    if extra is not None:
        registry.register(extra)
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db_path)
    world = WorldStateMonitor(cognitive, sink=events)
    world.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=_policy(), goal_manager=gm, world_monitor=world,
    )
    return engine, storage, gm


@pytest.fixture
def sandbox(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _events(storage, task_id):
    return [e.kind for e in storage.list_events(task_id=task_id)]


# ---------------------------------------------------------------------------
# D4: engine upgrades a weak mutation policy, whatever the planner proposed
# ---------------------------------------------------------------------------


def test_engine_upgrades_weak_mutation_verification(tmp_path, sandbox):
    """DeterministicPlanner bypasses PlanValidator - the engine must still fix it."""
    engine, storage, gm = _engine(
        tmp_path / "a.db", sandbox, _WeakVerificationPlanner()
    )
    gid = engine.submit_goal("write a file").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    step = task.steps[0]
    assert step.verification.policy == "write_verified"  # upgraded from non_empty
    assert step.status == StepStatus.SUCCEEDED
    assert task.status == TaskStatus.COMPLETED
    assert "verification.normalized" in _events(storage, task.id)
    storage.close()


def test_normalization_preserves_what_the_planner_requested(tmp_path, sandbox):
    """The executed policy is authoritative, but the request is not erased."""
    engine, storage, gm = _engine(
        tmp_path / "b.db", sandbox, _WeakVerificationPlanner()
    )
    gid = engine.submit_goal("write a file").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    prov = [g for g in task.steps[0].guidance
            if g.get("kind") == "verification_normalized"]
    assert len(prov) == 1
    assert prov[0]["requested"] == "non_empty"
    assert prov[0]["applied"] == "write_verified"
    assert prov[0]["authority"] == "registry"
    assert prov[0]["mutating"] is True
    storage.close()


def test_normalization_provenance_survives_persistence(tmp_path, sandbox):
    engine, storage, gm = _engine(
        tmp_path / "c.db", sandbox, _WeakVerificationPlanner()
    )
    gid = engine.submit_goal("write a file").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    reloaded = storage.load_task(task.id)
    prov = [g for g in reloaded.steps[0].guidance
            if g.get("kind") == "verification_normalized"]
    assert prov and prov[0]["applied"] == "write_verified"
    storage.close()


def test_read_only_step_is_not_renormalized(tmp_path, sandbox):
    """Reads keep the historical model: no upgrade, no provenance noise."""
    class ReadPlanner(_WeakVerificationPlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(
                index=0, intent="read", capability="filesystem.read",
                action="read", scope="filesystem:read", params={"path": "f.txt"},
                verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
            )]

        def required_capabilities(self, goal_description):
            return {"filesystem.read"}

    (sandbox / "f.txt").write_text("data")
    engine, storage, gm = _engine(tmp_path / "d.db", sandbox, ReadPlanner())
    gid = engine.submit_goal("read a file").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    assert task.steps[0].verification.policy == "schema_keys"
    assert not [g for g in task.steps[0].guidance
                if g.get("kind") == "verification_normalized"]
    assert "verification.normalized" not in _events(storage, task.id)
    storage.close()


# ---------------------------------------------------------------------------
# D5: fail closed BEFORE the mutation happens
# ---------------------------------------------------------------------------


def test_mutation_with_no_usable_policy_is_refused_before_executing(tmp_path, sandbox):
    """The refusal must precede execution - the mutation never runs."""
    cap = _UndeclaredMutation()
    engine, storage, gm = _engine(
        tmp_path / "e.db", sandbox,
        _UndeclaredPlanner(VerificationPolicy("made_up_policy")),
        extra=cap,
    )
    gid = engine.submit_goal("do the thing").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    assert task.steps[0].status == StepStatus.FAILED
    assert task.status == TaskStatus.FAILED
    kinds = _events(storage, task.id)
    assert "verification.refused" in kinds
    # Never executed: no capability.executed, no mutation events at all.
    assert "capability.executed" not in kinds
    assert not any(k.startswith("mutation.") for k in kinds)
    storage.close()


def test_refusal_diagnostic_names_the_action_and_the_missing_declaration(tmp_path, sandbox):
    engine, storage, gm = _engine(
        tmp_path / "f.db", sandbox,
        _UndeclaredPlanner(VerificationPolicy("made_up_policy")),
        extra=_UndeclaredMutation(),
    )
    gid = engine.submit_goal("do the thing").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    error = task.steps[0].error or ""
    assert "mutate" in error
    assert "default_verification" in error
    assert "made_up_policy" in error
    storage.close()


def test_custom_mutation_with_explicit_known_policy_still_executes(tmp_path, sandbox):
    """Fail-closed must not brick capabilities that declare no default."""
    engine, storage, gm = _engine(
        tmp_path / "g.db", sandbox,
        _UndeclaredPlanner(VerificationPolicy("non_empty")),
        extra=_UndeclaredMutation(),
    )
    gid = engine.submit_goal("do the thing").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert task.steps[0].verification.policy == "non_empty"
    storage.close()


# ---------------------------------------------------------------------------
# D9: verified-outcome provenance reaches the episode
# ---------------------------------------------------------------------------


def test_episode_records_which_policy_established_each_step(tmp_path, sandbox):
    engine, storage, gm = _engine(
        tmp_path / "h.db", sandbox, _WeakVerificationPlanner()
    )
    gid = engine.submit_goal("write a file").id
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]

    from arion.memory.lifecycle import build_episode_from_task

    episode = build_episode_from_task(task, storage.list_events(task_id=task.id))
    assert episode.verification["policies"]["0"] == "write_verified"
    assert episode.verification["unverifiable"] == []
    assert 0 in episode.verification["passed"]
    storage.close()
