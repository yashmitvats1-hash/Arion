"""Context injection into planning (ADR-012).

Proves the pipeline: goal -> memory retrieval -> context object -> planner,
WITHOUT a real external model. The model (mock router) receives relevant,
bounded memory context and the plan still flows through validation,
authorization, execution, verification, completion.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"

PLAN = {
    "version": PLAN_SCHEMA_VERSION,
    "intent": "Inspect this repository",
    "steps": [
        {"intent": "list root", "capability": "filesystem.read", "action": "list",
         "params": {"path": "."}, "verification": {"policy": "non_empty"}},
        {"intent": "read readme", "capability": "filesystem.read", "action": "read",
         "params": {"path": "README.md"},
         "verification": {"policy": "schema_keys", "args": {"keys": ["content"]}}},
    ],
}


class RecordingRouter:
    """Mock model that records the context it received (memory digest)."""

    def __init__(self):
        self.seen_contexts = []

    def generate(self, prompt, **kwargs):
        return "mock"

    def plan_structured(self, goal, capabilities, context):
        self.seen_contexts.append(context)
        return PlanSchema.from_dict(PLAN)


def _engine(db_path, sandbox, router):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    memory = SQLiteMemoryStore(db_path)
    return ArionEngine(
        storage=storage, registry=registry,
        planner=RealModelPlanner(router, events=events), router=router, events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
    ), memory


def test_goal_retrieve_context_planner_without_external_model(tmp_path, sandbox):
    db = tmp_path / "ctx.db"

    # ---- Task A completes and records memory (process 1) ----
    router_a = RecordingRouter()
    engine_a, memory_a = _engine(db, sandbox, router_a)
    task_a = engine_a.execute_goal("Inspect this repository")
    assert task_a.status == TaskStatus.COMPLETED
    assert len(memory_a.list_recent(limit=5)) == 1
    memory_a.close()

    # ---- Task B (process 2): planning receives relevant historical context ----
    router_b = RecordingRouter()
    engine_b, memory_b = _engine(db, sandbox, router_b)
    task_b = engine_b.execute_goal("Inspect this repository")
    assert task_b.status == TaskStatus.COMPLETED

    # the model router received a memory digest built from retrieved episodes
    assert router_b.seen_contexts, "model was never consulted"
    mem = router_b.seen_contexts[0].get("memory")
    assert mem is not None, "planner must receive memory context"
    assert mem["counts"]["episodes"] >= 1
    assert mem["episodes"][0]["goal"] == "Inspect this repository"
    # task ids differ (two separate tasks), but memory carried the first forward
    assert task_b.id != task_a.id

    # observability: context + retrieval events recorded
    kinds = [e.kind for e in engine_b.storage.list_events(task_b.id)]
    assert "memory.retrieval.completed" in kinds
    assert "planning.context.created" in kinds
    engine_b.storage.close()
    memory_b.close()


def test_retrieval_returns_only_relevant_bounded_memory(tmp_path, sandbox):
    db = tmp_path / "rel.db"
    router = RecordingRouter()
    engine, memory = _engine(db, sandbox, router)

    # seed prior experience: one RELEVANT episode + unrelated ones (rank low)
    from arion.memory.models import Episode
    from arion.state.models import utcnow

    memory.record_episode(Episode(
        episode_id="ep_inspect_prior", task_id="t_prior", goal_id="g",
        goal="Inspect this repository", plan_summary=[], actions=[],
        outcome="completed", verification={}, failures=[], authorization={}, recovery={},
        tags=["filesystem.read"], importance=0.5, created_at=utcnow(), updated_at=utcnow(),
    ))
    for i in range(3):
        memory.record_episode(Episode(
            episode_id=f"ep_other_{i}", task_id=f"t_other_{i}", goal_id="g",
            goal=f"order a pizza with extra cheese {i}", plan_summary=[], actions=[],
            outcome="completed", verification={}, failures=[], authorization={}, recovery={},
            tags=["delivery"], importance=0.5, created_at=utcnow(), updated_at=utcnow(),
        ))

    task = engine.execute_goal("Inspect this repository")
    assert task.status == TaskStatus.COMPLETED
    mem = router.seen_contexts[-1]["memory"]
    assert len(mem["episodes"]) >= 1
    assert mem["episodes"][0]["goal"] == "Inspect this repository"  # relevant memory ranked first
    assert not any("pizza" in e["goal"] for e in mem["episodes"])   # irrelevant memory excluded
    engine.storage.close()
    memory.close()


def test_context_digest_shape_is_stable(tmp_path, sandbox):
    db = tmp_path / "shape.db"
    router = RecordingRouter()
    engine, memory = _engine(db, sandbox, router)

    # seed prior relevant experience so planning receives non-empty context
    from arion.memory.models import Episode
    from arion.state.models import utcnow

    memory.record_episode(Episode(
        episode_id="ep_prior", task_id="t_prior", goal_id="g",
        goal="Inspect this repository", plan_summary=[], actions=[],
        outcome="completed", verification={}, failures=[], authorization={}, recovery={},
        tags=["filesystem.read"], importance=0.5, created_at=utcnow(), updated_at=utcnow(),
    ))

    engine.execute_goal("Inspect this repository")
    mem = router.seen_contexts[-1]["memory"]
    # structured, separated context: historical_facts + reflections +
    # recommendations (guidance) + strategy + environment + plan_history +
    # provenance + counts
    assert set(mem.keys()) == {"episodes", "reflections", "guidance", "strategy",
                               "environment", "plan_history", "provenance", "counts"}
    assert isinstance(mem["plan_history"], list)  # bounded, immutable plan versions
    assert set(mem["counts"].keys()) == {"episodes", "reflections", "guidance"}
    # provenance answers "which memory influenced this plan?"
    assert "episode_ids" in mem["provenance"]
    assert "reflection_ids" in mem["provenance"]
    assert "guidance_ids" in mem["provenance"]
    # strategy + environment are structured, informational fields
    assert mem["strategy"] is None or isinstance(mem["strategy"], dict)
    assert isinstance(mem["environment"], list)
    ep0 = mem["episodes"][0]
    for key in ("episode_id", "goal", "outcome", "tags", "importance", "plan", "failures", "created_at"):
        assert key in ep0, f"missing {key} in episode digest"
    engine.storage.close()
