"""M9-B.3 P0 — `irreversible` IS a mutation: one classification, every path.

Before P0 the engine compared ``side_effects == "mutating"`` **literally** in four
places (`engine.py` 365, 3039, 3422, 4725) while `registry.is_mutating()` — the
authority ADR-060 D5 uses to *demand a verification policy* — also counts
`"irreversible"`. The two definitions disagreed, and the disagreement was
reachable. Measured at baseline, an action declaring `side_effects="irreversible"`:

    capability invocations : 1
    step status            : SUCCEEDED
    dest exists / source gone : True / True
    durable lock rows      : []
    lock events            : NONE

i.e. it mutated the world with **zero mutation locks**, skipping the durable lock
(ADR-021), bounded contention waiting (ADR-022), the FIFO waiter queue (ADR-023),
same-resource dispatch gating (ADR-024/025), post-wait re-authorization and
recovery mirroring (ADR-020) — while ADR-060 D5 *still* refused to let it run
without a verification policy. Two authorities, one word, different meanings.

P0 collapses all four sites onto `registry.is_mutating()`. These tests pin the
behaviour in BOTH directions: every mutating classification now coordinates, and
read-only classifications still do not — the fix must not widen.

Note what is under test: the engine classifies from **declared metadata alone**.
It never inspects what a capability actually did, so the stub capability below
writes a marker file regardless of the `side_effects` value it declares. That is
deliberate — a declaration-driven engine must be driven by the declaration.
"""

from __future__ import annotations

import inspect
import re

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import (
    ActionSpec,
    CapabilityRegistry,
    is_mutating,
)
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import canonical_resource
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
CAPABILITY = "filesystem.touch"

TAXONOMY = ["none", "read_only", "mutating", "irreversible"]


def _spec(side_effects: str, retry_safe: bool = True) -> ActionSpec:
    return ActionSpec(
        name="touch",
        description="Write a marker file (classification under test is declared, not observed).",
        required_scope="filesystem:write",
        risk="low",                      # keep the approval seam out of these tests
        side_effects=side_effects,
        reversible=False,
        idempotent=False,
        retry_safe=retry_safe,
        resource_kind=FS,
        resource_param="path",
        param_schema={"path": {"type": "string", "required": True}},
        default_verification={"policy": "schema_keys", "args": {"keys": ["touched"]}},
    )


class TouchCapability:
    """One action whose `side_effects` declaration is injected per test."""

    name = CAPABILITY
    description = "Marker-writing capability used to test mutation classification."

    def __init__(self, sandbox_root, side_effects="mutating", retry_safe=True,
                 fail_after_mutating=False):
        from pathlib import Path

        self.sandbox_root = Path(sandbox_root).resolve()
        self.actions = [_spec(side_effects, retry_safe)]
        self.fail_after_mutating = fail_after_mutating
        self.calls = 0
        self.lock_store = None            # wired after engine construction
        self.locks_seen_during_execute = []

    def execute(self, action, params):
        if action != "touch":
            raise ValueError(action)
        self.calls += 1
        if self.lock_store is not None:
            # What the coordination layer holds AT THE MOMENT OF MUTATION.
            self.locks_seen_during_execute.append(
                [(l.resource_kind, l.resource) for l in self.lock_store.list()]
            )
        rel = params["path"]
        target = (self.sandbox_root / rel).resolve()
        target.relative_to(self.sandbox_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("touched\n", encoding="utf-8")
        if self.fail_after_mutating:
            from arion.capabilities.registry import CapabilityError

            raise CapabilityError("failed after the side effect")
        return {"touched": True, "path": str(rel)}


class TouchPlanner:
    """Emits one or more `touch` steps (the deterministic planner cannot reach
    this capability at all — see ADR-062 D2)."""

    def __init__(self, paths):
        self.paths = list(paths)

    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=i, intent=f"touch {p}", capability=CAPABILITY,
                     action="touch", scope="filesystem:write", params={"path": p},
                     verification=VerificationPolicy("schema_keys", {"keys": ["touched"]}))
            for i, p in enumerate(self.paths)
        ]

    def required_capabilities(self, goal_description):
        return {CAPABILITY}


def _engine(db_path, sandbox, paths=("marker.txt",), side_effects="mutating",
            retry_safe=True, fail_after_mutating=False, lock_wait_max_seconds=5.0):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    cap = TouchCapability(sandbox, side_effects=side_effects, retry_safe=retry_safe,
                          fail_after_mutating=fail_after_mutating)
    registry.register(cap)
    events = EventLogger(sinks=[storage])
    planner = TouchPlanner(paths)
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(storage=storage, cognitive_store=cognitive, events=events,
                     strategy_selector=StrategySelector(),
                     progress_evaluator=DeterministicProgressEvaluator(),
                     world_monitor=wm)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(
            allowed_scopes={"filesystem:read", "filesystem:write"},
            risk_deny=set(), risk_approve=set(),
            boundaries={FS: RelativePathBoundary()},
        ),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        lock_wait_max_seconds=lock_wait_max_seconds,
    )
    cap.lock_store = engine.mutation_lock_store
    return engine, gm, storage, cap


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    return sb


def _kinds(storage):
    return [e.kind for e in storage.list_events()]


# --------------------------------------------------------------------------
# The core P0 regression: irreversible must coordinate like any mutation
# --------------------------------------------------------------------------


def test_irreversible_action_holds_a_mutation_lock_while_it_mutates(tmp_path):
    """The baseline measurement this reverses: `irreversible` mutated the world
    with zero lock rows and zero lock events."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, cap = _engine(tmp_path / "p0.db", sb,
                                       side_effects="irreversible")
    try:
        task = engine.execute_goal("touch the marker")
        assert task.status == TaskStatus.COMPLETED, task.error
        assert cap.calls == 1
        assert (sb / "marker.txt").exists()

        kinds = _kinds(storage)
        assert "mutation.lock.requested" in kinds
        assert "mutation.lock.acquired" in kinds
        assert "mutation.lock.released" in kinds

        # and the lock was actually held AT THE MOMENT OF MUTATION, not merely
        # requested somewhere in the task's lifetime
        expected = (FS, canonical_resource(FS, "marker.txt"))
        assert cap.locks_seen_during_execute == [[expected]], (
            f"no lock held during the irreversible mutation: "
            f"{cap.locks_seen_during_execute}"
        )
    finally:
        engine.storage.close()


def test_irreversible_emits_the_mutation_event_vocabulary(tmp_path):
    sb = _sandbox(tmp_path)
    engine, _, storage, _ = _engine(tmp_path / "p0b.db", sb,
                                    side_effects="irreversible")
    try:
        engine.execute_goal("touch the marker")
        kinds = _kinds(storage)
        assert "mutation.attempted" in kinds
        assert "mutation.succeeded" in kinds
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# One classification across the whole taxonomy — and it must not widen
# --------------------------------------------------------------------------


@pytest.mark.parametrize("side_effects", TAXONOMY)
def test_lock_acquired_exactly_when_is_mutating_says_so(tmp_path, side_effects):
    """The engine's coordination decision and `registry.is_mutating()` are now
    the SAME decision. Read-only classifications must still take no lock."""
    sb = _sandbox(tmp_path)
    engine, _, storage, cap = _engine(tmp_path / f"tax-{side_effects}.db", sb,
                                      side_effects=side_effects)
    try:
        spec = engine.registry.action_spec(CAPABILITY, "touch")
        expected_mutating = is_mutating(spec)
        assert expected_mutating == (side_effects in ("mutating", "irreversible"))

        task = engine.execute_goal("touch the marker")
        assert task.status == TaskStatus.COMPLETED, task.error
        kinds = _kinds(storage)
        acquired = "mutation.lock.acquired" in kinds
        assert acquired is expected_mutating, (
            f"side_effects={side_effects!r}: lock acquired={acquired} but "
            f"is_mutating={expected_mutating}"
        )
        held = bool(cap.locks_seen_during_execute) and all(
            cap.locks_seen_during_execute)
        assert held is expected_mutating
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# Irreversible shares the lock namespace with mutating
# --------------------------------------------------------------------------


def test_irreversible_contends_with_a_held_lock_on_the_same_resource(tmp_path):
    """Same canonical resource => same lock, regardless of which of the two
    mutating classifications each side declares. Waiting disabled so contention
    is immediate and durable (ADR-021 semantics)."""
    sb = _sandbox(tmp_path)
    engine, _, storage, cap = _engine(tmp_path / "contend.db", sb,
                                      side_effects="irreversible",
                                      lock_wait_max_seconds=0.0)
    try:
        engine.mutation_lock_store.acquire(
            FS, canonical_resource(FS, "marker.txt"),
            "filesystem.write", "write", "owner:other",
            lease_seconds=300.0, now=engine._lock_now())

        task = engine.execute_goal("touch the marker")
        assert task.status == TaskStatus.FAILED
        assert cap.calls == 0, "the capability ran despite a held lock"
        assert not (sb / "marker.txt").exists()
        assert "mutation.lock.contended" in _kinds(storage)
    finally:
        engine.storage.close()


def test_irreversible_waits_then_times_out_durably_when_a_lock_is_held(tmp_path):
    """With bounded waiting configured (ADR-022), an irreversible action joins
    the wait path instead of bypassing it: it must NOT execute while the lock is
    held, and deadline expiry must be a durable, explainable failure."""
    sb = _sandbox(tmp_path)
    engine, _, storage, cap = _engine(tmp_path / "wait.db", sb,
                                      side_effects="irreversible",
                                      lock_wait_max_seconds=0.2)
    engine.lock_sleeper = lambda seconds: None   # instant, deterministic backoff
    try:
        engine.mutation_lock_store.acquire(
            FS, canonical_resource(FS, "marker.txt"),
            "filesystem.write", "write", "owner:other",
            lease_seconds=300.0, now=engine._lock_now())

        task = engine.execute_goal("touch the marker")
        assert task.status == TaskStatus.FAILED
        assert cap.calls == 0, "the capability ran while another owner held the lock"
        assert not (sb / "marker.txt").exists()
        assert "mutation.lock.queued" in _kinds(storage)
    finally:
        engine.storage.close()


def test_dispatch_gate_treats_two_irreversible_steps_as_same_resource(tmp_path):
    """`engine.py:3422` — the ADR-024/025 same-resource collision gate. Two
    irreversible steps on one canonical resource must not both be dispatched in
    a round when waiting is disabled."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, _ = _engine(tmp_path / "gate.db", sb,
                                     paths=["marker.txt", "marker.txt"],
                                     side_effects="irreversible",
                                     lock_wait_max_seconds=0.0)
    try:
        spec = engine.registry.action_spec(CAPABILITY, "touch")
        assert is_mutating(spec)
        task = engine.create_task(engine.submit_goal("touch twice"))
        task.steps = TouchPlanner(["marker.txt", "marker.txt"]).plan("", task.id, None)
        task.current_step = 0

        chosen: set[tuple[str, str]] = set()
        ok0, reason0 = engine._step_dispatchable(task, 0, chosen)
        assert ok0 and reason0 == ""
        assert chosen == {(FS, canonical_resource(FS, "marker.txt"))}

        ok1, reason1 = engine._step_dispatchable(task, 1, chosen)
        assert ok1 is False
        assert reason1 == "same-resource"
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# Recovery: engine.py:365 and the failure path at 4725
# --------------------------------------------------------------------------


def test_failed_irreversible_mutation_requires_recovery(tmp_path):
    """A non-retry-safe `irreversible` failure may have partially applied, so it
    must enter the durable recovery path (ADR-020) exactly as `mutating` does."""
    sb = _sandbox(tmp_path)
    engine, gm, storage, cap = _engine(tmp_path / "recovery.db", sb,
                                       side_effects="irreversible",
                                       retry_safe=False,
                                       fail_after_mutating=True)
    try:
        task = engine.execute_goal("touch then fail")
        assert task.status == TaskStatus.FAILED
        assert cap.calls == 1, "a non-retry-safe mutation was retried"
        assert (sb / "marker.txt").exists(), "the side effect happened"

        kinds = _kinds(storage)
        assert "mutation.failed" in kinds
        assert "mutation.requires_recovery" in kinds

        recoveries = engine.recovery_store.list_recoveries()
        assert len(recoveries) == 1
        assert recoveries[0].capability == CAPABILITY
        assert recoveries[0].resource == "marker.txt"
    finally:
        engine.storage.close()


def test_read_only_failure_does_not_create_a_recovery_record(tmp_path):
    """Negative control: the recovery path must not widen to read-only actions."""
    sb = _sandbox(tmp_path)
    engine, _, storage, cap = _engine(tmp_path / "norecovery.db", sb,
                                      side_effects="read_only",
                                      retry_safe=False,
                                      fail_after_mutating=True)
    try:
        task = engine.execute_goal("touch then fail")
        assert task.status == TaskStatus.FAILED
        assert "mutation.requires_recovery" not in _kinds(storage)
        assert engine.recovery_store.list_recoveries() == []
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# The classification is single — a source-level regression gate
# --------------------------------------------------------------------------


def test_engine_has_no_literal_side_effects_comparison_left():
    """P0's whole point is that ONE predicate classifies mutation. A re-introduced
    literal comparison would silently re-open the `irreversible` bypass, and no
    behavioural test above would notice if it landed on a path these tests do not
    drive (e.g. the round-building loop at `engine.py:3039`).

    Deliberately broader than P0: ANY literal comparison of `side_effects` against
    a taxonomy value is an offence, including a future `== "irreversible"`
    special case, because each one is a second classification authority.
    """
    from arion.orchestration import engine as engine_module

    source = inspect.getsource(engine_module)
    taxonomy = ("mutating", "irreversible", "read_only", "none")
    offenders = []
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.split("#")[0]
        if "side_effects" not in line:
            continue
        if re.search(r"[=!]=\s*[\"'](?:%s)[\"']" % "|".join(taxonomy), line):
            offenders.append((lineno, line.strip()))
    assert not offenders, (
        "engine.py compares side_effects against a literal taxonomy value "
        "instead of deferring to registry.is_mutating() (ADR-062 P0): "
        f"{offenders}"
    )


def test_is_mutating_is_the_single_authority_for_the_taxonomy():
    """The predicate every engine path now defers to."""
    for side_effects in TAXONOMY:
        spec = _spec(side_effects)
        expected = side_effects in ("mutating", "irreversible")
        assert is_mutating(spec) is expected, side_effects
