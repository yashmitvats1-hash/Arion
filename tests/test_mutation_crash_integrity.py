"""F10 regression: mutation crash integrity (issue #23 / issue #18 F10).

A non-retry-safe mutation must dispatch at most once per step lifetime.
The durable mutation-intent journal records the dispatch BEFORE the
capability runs; after a hard crash in the side-effect/persistence window,
an unsettled intent means the outcome is IN DOUBT and the work fences into
the recovery registry instead of being replayed blindly.

Covers:
- intent persistence ordering (journaled BEFORE the side effect);
- fail-closed dispatch when the journal cannot be written (capability never
  runs, no recovery record - nothing uncertain happened);
- hard-crash window: no duplicate append on resume; REQUIRED recovery
  record; task fails closed; goal blocked; recovery acknowledgement flow
  intact;
- retry-safe mutations still replay after a crash (declared contract,
  preserved);
- settled intents are not fenced on ordinary resume (no behaviour change
  for normal completions);
- journal lifecycle API (single row per task/step, idempotent settle).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.state.intents import INTENT_IN_FLIGHT, INTENT_SETTLED, MutationIntent, MutationIntentError
from arion.state.recovery import RecoveryStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
OFFLINE_MODEL_CONFIG = SimpleNamespace(
    enabled=False, fallback_enabled=False, reflection_enabled=False
)


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


class FixedPlanner:
    last_source = "deterministic"
    last_transformation = None

    def __init__(self, steps, required):
        self._steps = steps
        self._required = set(required)

    def plan(self, goal_description, task_id, registry, context=None):
        return copy.deepcopy(self._steps)

    def required_capabilities(self, goal_description):
        return set(self._required)


def _append_step(path="journal.txt", content="A"):
    from arion.state.models import PlanStep, VerificationPolicy
    return PlanStep(
        index=0, intent="append journal", capability="filesystem.append",
        action="append", scope="filesystem:write",
        params={"path": path, "content": content, "create": False},
        verification=VerificationPolicy("non_empty"),
    )


def _policy():
    from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        boundaries={FS: RelativePathBoundary()},
        risk_deny=set(),
        risk_approve={"high"},
    )


def _build(root: Path, *, planner=None):
    from arion.orchestration.authz import AutoApproveHandler
    return build_engine(
        root / "arion.db", root / "sandbox", planner=planner or FixedPlanner(
            [_append_step()], {"filesystem.append"}),
        policy=_policy(), approval_handler=AutoApproveHandler(),
        model_config=OFFLINE_MODEL_CONFIG,
    )


class CrashAfterSideEffect(FilesystemAppendCapability):
    """Test-only append that applies the side effect and kills the process
    before any outcome persistence (the F10 crash window)."""

    name = "filesystem.append"

    def __init__(self, sandbox, signal):
        super().__init__(sandbox)
        self.signal = signal

    def execute(self, action, params):
        result = super().execute(action, params)
        self.signal.write_text("side effect durable; exiting", encoding="utf-8")
        import os
        os._exit(86)


class RetrySafeCrashCapability:
    """Test-only RETRY-SAFE mutating capability that crashes once."""

    name = "probe.retry_safe_crash"
    description = "test-only retry-safe mutation with a crash window"
    actions = [
        ActionSpec(
            name="apply", description="idempotent set", required_scope="filesystem:write",
            risk="low", side_effects="mutating", reversible=True, idempotent=True,
            retry_safe=True, resource_kind=FS, resource_param="path",
            param_schema={"path": {"type": "string", "required": True},
                          "content": {"type": "string", "required": True}},
            default_verification={"policy": "non_empty"},
        )
    ]

    def __init__(self, sandbox, signal):
        self.sandbox = sandbox
        self.signal = signal

    def execute(self, action, params):
        (self.sandbox / params["path"]).write_text(params["content"], encoding="utf-8")
        if not self.signal.exists():
            self.signal.write_text("crash now", encoding="utf-8")
            import os
            os._exit(87)
        return {"ok": True, "path": params["path"]}


WORKER_TEMPLATE = '''
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
import importlib
mod = importlib.import_module({module!r})
mod.{entry}(Path({root!r}))
'''


def _run_worker(module: str, entry: str, root: Path, repo: Path):
    code = WORKER_TEMPLATE.format(repo=str(repo), module=module, entry=entry, root=str(root))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        cwd=str(repo),
    )


def _expire_leases(engine, goal_id):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    engine.storage.reclaim_expired(now=future)
    engine.storage.reclaim_expired(
        now=future, resource_kind="arion:goal-run", resource=goal_id)
    engine.scheduler_registry.reclaim_stale(now=future)


# --------------------------------------------------------------------- #
# crash-window workers (executed in a child process via _run_worker)
# --------------------------------------------------------------------- #


def _crash_append_child(root: Path):
    """Runs the approved append and dies inside the capability (exit 86)."""
    engine = _build(root)
    engine.registry.register(
        CrashAfterSideEffect(root / "sandbox", root / "crash-signal.txt"))
    goal_id = (root / "goal-id.txt").read_text(encoding="utf-8")
    engine.run_goal(goal_id)
    raise AssertionError("child should have exited inside capability")


def _crash_retry_safe_child(root: Path):
    """Applies a retry-safe mutation and dies inside the capability (exit 87)."""
    from arion.state.models import PlanStep, VerificationPolicy
    step = PlanStep(
        index=0, intent="idempotent set", capability="probe.retry_safe_crash",
        action="apply", scope="filesystem:write",
        params={"path": "state.txt", "content": "SET"},
        verification=VerificationPolicy("non_empty"),
    )
    engine = _build(root, planner=FixedPlanner([step], {"probe.retry_safe_crash"}))
    engine.registry.register(
        RetrySafeCrashCapability(root / "sandbox", root / "retry-signal.txt"))
    goal_id = (root / "goal-id.txt").read_text(encoding="utf-8")
    engine.run_goal(goal_id)
    raise AssertionError("child should have exited inside capability")


# --------------------------------------------------------------------- #
# intent persistence ordering + fail-closed dispatch
# --------------------------------------------------------------------- #


class JournalCheckAppend(FilesystemAppendCapability):
    """Asserts its own intent row exists BEFORE applying the side effect."""

    name = "filesystem.append"

    def __init__(self, sandbox, journal):
        super().__init__(sandbox)
        self.journal = journal
        self.saw_intent_before_side_effect = False

    def execute(self, action, params):
        rows = self.journal.list_mutation_intents(open_only=True)
        self.saw_intent_before_side_effect = any(
            r.capability == "filesystem.append" and r.status == INTENT_IN_FLIGHT
            for r in rows
        )
        return super().execute(action, params)


def test_intent_persisted_before_dispatch_and_settled_after(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    engine = _build(tmp_path)
    try:
        probe = JournalCheckAppend(sandbox, engine.storage)
        engine.registry.register(probe)
        goal = engine.submit_goal("append A once")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEA"
        assert probe.saw_intent_before_side_effect
        rows = engine.storage.list_mutation_intents()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == INTENT_SETTLED and row.outcome == "succeeded"
        assert row.capability == "filesystem.append" and row.action == "append"
        assert row.resource == "journal.txt"
    finally:
        engine.close()


def test_journal_write_failure_fails_closed_without_dispatch(tmp_path):
    """If the intent cannot be persisted, the capability NEVER runs: no side
    effect exists, so the step fails cleanly WITHOUT a recovery record."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    engine = _build(tmp_path)
    try:
        def boom(intent):
            raise RuntimeError("journal unavailable (test)")
        engine.storage.record_mutation_intent = boom
        goal = engine.submit_goal("append A once")
        final = engine.run_goal(goal.id)
        assert final.status.value != "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status.value == "failed"
        assert "mutation intent persistence failed closed" in (task.error or "")
        assert engine.recovery_store.list_recoveries(task_id=task.id) == []
    finally:
        engine.close()


def test_duplicate_intent_refused_fail_closed(tmp_path):
    """Replay-unsafe work journals at most once per (task, step)."""
    storage = SQLiteStorage(tmp_path / "d.db")
    first = MutationIntent(
        intent_id="intent-1", task_id="t1", step_index=0, attempt=1,
        capability="filesystem.append", action="append", resource="journal.txt",
    )
    storage.record_mutation_intent(first)
    with pytest.raises(MutationIntentError):
        storage.record_mutation_intent(MutationIntent(
            intent_id="intent-2", task_id="t1", step_index=0, attempt=2,
            capability="filesystem.append", action="append", resource="journal.txt",
        ))
    assert storage.list_mutation_intents(open_only=True)[0].intent_id == "intent-1"


def test_settle_is_idempotent_and_filters(tmp_path):
    storage = SQLiteStorage(tmp_path / "d.db")
    storage.record_mutation_intent(MutationIntent(
        intent_id="intent-1", task_id="t1", step_index=0, attempt=1,
        capability="filesystem.append", action="append", resource="journal.txt",
    ))
    assert storage.settle_mutation_intent("t1", 0, "succeeded") == 1
    assert storage.settle_mutation_intent("t1", 0, "succeeded") == 0
    assert storage.list_mutation_intents(open_only=True) == []
    rows = storage.list_mutation_intents(task_id="t1")
    assert rows[0].status == INTENT_SETTLED and rows[0].outcome == "succeeded"


# --------------------------------------------------------------------- #
# hard-crash window: no blind replay
# --------------------------------------------------------------------- #


def test_crash_window_no_blind_replay_and_recovery_fences(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    engine = _build(tmp_path)
    try:
        goal = engine.submit_goal("append A once")
        (tmp_path / "goal-id.txt").write_text(goal.id, encoding="utf-8")
    finally:
        engine.close()

    child = _run_worker("tests.test_mutation_crash_integrity", "_crash_append_child",
                        tmp_path, Path(__file__).resolve().parent.parent)
    assert child.returncode == 86, child.stdout[-2000:] + child.stderr[-2000:]
    assert (tmp_path / "crash-signal.txt").exists()
    assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEA"

    engine = _build(tmp_path)
    try:
        _expire_leases(engine, goal.id)
        from arion.state.models import TaskStatus
        task = engine.goal_manager.task_history(goal.id)[-1]
        # PUBLIC resume paths (arion resume <task> / run_goal) must fence.
        resumed = engine.run_task(task.id)
        assert resumed.status == TaskStatus.FAILED
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEA", \
            "non-retry-safe append must not be replayed after the crash"
        final = engine.run_goal(goal.id)
        assert final.status.value != "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEA"

        recoveries = engine.recovery_store.list_recoveries(goal_id=goal.id)
        assert len(recoveries) == 1
        rec = recoveries[0]
        assert rec.status == RecoveryStatus.REQUIRED
        assert rec.step_index == 0 and rec.action == "append"
        attempts = sum(
            e.kind == "mutation.attempted"
            for e in engine.storage.list_events(task.id)
        )
        assert attempts == 1, "no second dispatch attempt may exist"

        # Recovery acknowledgement flow is preserved (ADR-020).
        acked = engine.acknowledge_recovery(rec.recovery_id, actor="operator")
        assert acked.status == RecoveryStatus.ACKNOWLEDGED
        assert not engine._has_open_recovery(goal.id)
    finally:
        engine.close()


def test_choke_point_fences_unsettled_intent_without_crash(tmp_path):
    """The dispatch choke point (_execute_step) refuses an unsettled
    replay-unsafe step even when the resume scans are bypassed entirely
    (defence in depth: no path may replay the step blindly)."""
    from arion.state.models import StepStatus, Task, TaskStatus, new_id
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    engine = _build(tmp_path)
    try:
        goal = engine.submit_goal("append A once")
        task = Task(
            id=new_id("task"), goal_id=goal.id, description="append A once",
            steps=[_append_step()], status=TaskStatus.RUNNING,
        )
        engine.storage.save_task(task)
        # Simulate a crash-leftover: journaled intent, step never finished.
        engine.storage.record_mutation_intent(MutationIntent(
            intent_id="intent-crash", task_id=task.id, step_index=0, attempt=1,
            capability="filesystem.append", action="append", resource="journal.txt",
        ))
        # Bypass every resume scan: call the per-step dispatch pipeline.
        engine._execute_step(task, task.steps[0])
        assert task.steps[0].status == StepStatus.FAILED
        assert task.status == TaskStatus.FAILED
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE", \
            "the choke point must not replay the journaled mutation"
        assert engine.recovery_store.list_recoveries(task_id=task.id)
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# retry-safe replay contract preserved + ordinary resume unchanged
# --------------------------------------------------------------------- #


def test_retry_safe_mutation_replays_after_crash(tmp_path):
    """Retry-safe mutating work MUST still replay after a crash - the fence
    applies only to replay-unsafe mutations (declared contract preserved)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    from arion.state.models import PlanStep, VerificationPolicy
    step = PlanStep(
        index=0, intent="idempotent set", capability="probe.retry_safe_crash",
        action="apply", scope="filesystem:write",
        params={"path": "state.txt", "content": "SET"},
        verification=VerificationPolicy("non_empty"),
    )
    engine = _build(tmp_path, planner=FixedPlanner([step], {"probe.retry_safe_crash"}))
    try:
        engine.registry.register(
            RetrySafeCrashCapability(sandbox, tmp_path / "retry-signal.txt"))
        goal = engine.submit_goal("idempotent set")
        (tmp_path / "goal-id.txt").write_text(goal.id, encoding="utf-8")
    finally:
        engine.close()

    child = _run_worker("tests.test_mutation_crash_integrity", "_crash_retry_safe_child",
                        tmp_path, Path(__file__).resolve().parent.parent)
    assert child.returncode == 87, child.stdout[-2000:] + child.stderr[-2000:]
    assert (sandbox / "state.txt").read_text(encoding="utf-8") == "SET"

    engine = _build(tmp_path, planner=FixedPlanner([step], {"probe.retry_safe_crash"}))
    try:
        engine.registry.register(
            RetrySafeCrashCapability(sandbox, tmp_path / "retry-signal.txt"))
        _expire_leases(engine, goal.id)
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        assert (sandbox / "state.txt").read_text(encoding="utf-8") == "SET"
        # The journal deliberately does not cover retry-safe dispatches.
        assert engine.storage.list_mutation_intents() == []
        assert engine.recovery_store.list_recoveries(goal_id=goal.id) == []
    finally:
        engine.close()


def test_settled_mutation_not_fenced_on_ordinary_resume(tmp_path):
    """A normally completed mutation leaves no open intent: re-running the
    task is a no-op and never fences (no behaviour change for success)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
    engine = _build(tmp_path)
    try:
        goal = engine.submit_goal("append A once")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        task = engine.goal_manager.task_history(goal.id)[-1]
        again = engine.run_task(task.id)
        assert again.status.value == "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEA"
        assert engine.storage.list_mutation_intents(open_only=True) == []
        assert engine.recovery_store.list_recoveries(goal_id=goal.id) == []
    finally:
        engine.close()
