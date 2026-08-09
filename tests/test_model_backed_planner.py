"""Model-backed planner swap test (ADR-005, ADR-008).

Proves the loop is provider-agnostic: a planner that consults a mock model
produces steps and the engine executes them identically. The model is only an
intelligence component - the loop (permission -> execute -> verify) is owned by
the engine and never changes.
"""

from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.router import ModelRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy


class MockModelPlanner:
    """A 'model-backed' planner: decomposes goals by asking a mock model."""

    def __init__(self, model: ModelRouter | None = None):
        self.model = model

    def plan(self, goal_description: str, task_id: str, registry: CapabilityRegistry, context=None) -> list[PlanStep]:
        if "file" in goal_description.lower():
            return [
                PlanStep(
                    index=0,
                    intent=self.model.generate(f"plan step for: {goal_description}") if self.model else "read readme",
                    capability="filesystem.read",
                    action="read",
                    scope="filesystem:read",
                    params={"path": "README.md"},
                    verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
                )
            ]
        raise ValueError(f"mock model cannot plan: {goal_description!r}")


class RecordingModelRouter:
    """Deterministic mock model that records what the planner asked it."""

    def __init__(self, planner: MockModelPlanner):
        self.prompts: list[str] = []
        self._planner = planner

    def generate(self, prompt: str, **kwargs) -> str:
        self.prompts.append(prompt)
        return "read the readme"

    def planner(self):
        return self._planner


def test_model_backed_planner_drives_same_loop(storage, sandbox):
    registry = CapabilityRegistry()
    from arion.capabilities.filesystem import FilesystemReadCapability

    registry.register(FilesystemReadCapability(sandbox))

    planner = MockModelPlanner()
    router = RecordingModelRouter(planner)
    planner.model = router  # wire the mock model into the planner

    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=router,
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
    )

    task = engine.execute_goal("read me the readme file")
    assert task.status == TaskStatus.COMPLETED
    assert len(task.steps) == 1
    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert task.steps[0].result["content"].startswith("# Test Repo")
    # the model was consulted for the plan intent
    assert router.prompts and "read me the readme" in router.prompts[0]

    # the loop events are identical to the deterministic path
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert "permission.checked" in kinds
    assert "capability.executed" in kinds
    assert "verification.passed" in kinds
    assert "task.completed" in kinds
