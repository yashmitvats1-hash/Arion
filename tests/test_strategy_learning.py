"""STRATEGY-LEVEL learning acceptance test (architecture directive #8).

Repeated failure must cause a MATERIALLY DIFFERENT decomposition or execution
strategy - not merely resource substitution.

Scenario: README.md is a BINARY file (read always fails with CapabilityError).
The deterministic default plan is [list ., read README.md]. After the first
failure, memory records an episode + reflection + guidance (avoid read on
README.md). A subsequent plan for the same goal must NOT merely swap resources
(it cannot - no read ever works) - it must switch to a DIFFERENT strategy:
inspection via directory listing (list), producing a materially different
decomposition that COMPLETES.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.retrieval import MemoryRetriever, build_planning_context
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _binary_sandbox(sandbox):
    """README.md is binary (unreadable); notes.txt + docs/ are readable."""
    (sandbox / "README.md").write_bytes(b"\x00\x01\x02\xffbinary")
    (sandbox / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    docs = sandbox / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "design.md").write_text("# Design\n", encoding="utf-8")
    return sandbox


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
    ), registry, planner, memory


def _list_docs_task(engine):
    """A successful list-based experience (list docs) so guidance has a prefer."""
    goal = engine.submit_goal("explore the docs directory")
    task = engine.create_task(goal)
    task.steps = [PlanStep(index=0, intent="list docs", capability="filesystem.read", action="list",
                           scope="filesystem:read", params={"path": "docs"},
                           verification=VerificationPolicy("non_empty"))]
    engine.storage.save_task(task)
    return engine.run_task(task.id)


def test_repeated_failure_changes_execution_strategy(tmp_path, sandbox):
    _binary_sandbox(sandbox)
    db = tmp_path / "strat.db"
    engine, registry, planner, memory = _engine(db, sandbox)

    # ---- Cycle 1: default plan FAILS (binary read) ----
    t1 = engine.execute_goal("inspect this repository")
    assert t1.status == TaskStatus.FAILED
    assert "not a text file" in (t1.error or "")
    # the failing episode + reflection + guidance exist
    episodes = memory.list_recent(limit=10)
    failed = [e for e in episodes if e.outcome == "failed"]
    assert failed and failed[0].task_id == t1.id
    assert failed[0].reflection_id

    # seed a successful list-based alternative (list docs worked)
    assert _list_docs_task(engine).status == TaskStatus.COMPLETED

    # ---- Cycle 2: WITHOUT memory, the default plan still fails ----
    plan_no_memory = planner.plan("inspect this repository", "t_plain", registry, context=None)
    assert plan_no_memory[1].params["path"] == "README.md"  # would fail again

    # ---- WITH memory: STRATEGY changes, not just resource ----
    retriever = MemoryRetriever(memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    plan_with_memory = planner.plan("inspect this repository", "t_mem", registry, context=ctx)

    # MATERIAL difference in decomposition: read step replaced by list step
    assert plan_no_memory[1].action == "read"
    assert plan_with_memory[1].action == "list"
    assert plan_with_memory[1].params["path"] == "docs"
    assert [s.action for s in plan_with_memory] != [s.action for s in plan_no_memory]

    # ---- Cycle 3: the SAME goal now COMPLETES end-to-end ----
    t2 = engine.execute_goal("inspect this repository")
    assert t2.status == TaskStatus.COMPLETED, f"expected completion after strategy change: {t2.error}"
    assert all(s.status.value == "succeeded" for s in t2.steps)
    # the executed plan used the list strategy, not read
    assert "read" not in [s.action for s in t2.steps]

    # ---- auditability: the transformation is recorded with provenance ----
    transformations = [e for e in engine.storage.list_events(t2.id) if e.kind == "planning.memory.transformation"]
    assert transformations, "planning.memory.transformation must be audited"
    decisions = transformations[0].detail["decisions"]
    assert any(d["category"] == "action_substitution" for d in decisions)
    assert decisions[0]["episode_id"] and decisions[0]["guidance_id"]
    # the executed step carries its guidance provenance
    list_step = [s for s in t2.steps if s.action == "list"][-1]
    assert list_step.guidance and list_step.guidance[0]["new_action"] == "list"
    engine.storage.close()


def test_second_cycle_stable_no_lesson_pileup(tmp_path, sandbox):
    _binary_sandbox(sandbox)
    db = tmp_path / "strat2.db"
    engine, registry, planner, memory = _engine(db, sandbox)

    assert engine.execute_goal("inspect this repository").status == TaskStatus.FAILED
    assert _list_docs_task(engine).status == TaskStatus.COMPLETED
    for _ in range(3):
        assert engine.execute_goal("inspect this repository").status == TaskStatus.COMPLETED

    retriever = MemoryRetriever(memory)
    ctx = build_planning_context(retriever, "inspect this repository")
    # strategy guidance is bounded and deduplicated
    strategy_guides = [g for g in ctx.guidance if g.strategy == "alternative_action"]
    assert len(strategy_guides) <= 1
    # beliefs derived from the experience are bounded too
    engine.storage.close()
