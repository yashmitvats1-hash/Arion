"""F10 recovery capability invariant (issue #23 second-pass review blocker).

A journaled replay-unsafe mutation must NEVER execute unless the engine has
a usable recovery mechanism (durable create + enumerate + acknowledge of
recovery obligations).  Two failure configurations are pinned here with an
ALTERNATE storage configuration (intent journal present, recovery registry
absent):

1. pre-dispatch: the mutation capability must not execute - no side effect,
   no journal row, no recovery record - the step fails closed;
2. post-dispatch crash residue: a stranded open intent plus no recovery
   mechanism must fail CLOSED on resume - the task fails terminal, the
   intent stays open, nothing is replayed and the mutation is never marked
   complete.

Also pinned: the invariant must NOT over-block - retry-safe mutations (whose
replay is the declared contract) still dispatch without a recovery
mechanism; and with a usable recovery mechanism the in-doubt mutation
reaches the explicit recovery-acknowledgement path exactly as designed.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.registry import ActionSpec
from arion.orchestration.authz import (
    AutoApproveHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.state.intents import INTENT_IN_FLIGHT, MutationIntent
from arion.state.models import PlanStep, VerificationPolicy

FS = "filesystem:path"

OFFLINE_MODEL_CONFIG = SimpleNamespace(
    enabled=False, fallback_enabled=False, reflection_enabled=False
)


class _AbsentRecovery:
    """Models 'recovery storage is absent': no recovery API at all.

    Any accidental use raises AttributeError and fails the test loudly.
    """


class _FixedPlanner:
    last_source = "deterministic"
    last_transformation = None

    def __init__(self, steps, required):
        self._steps = steps
        self._required = set(required)

    def plan(self, goal_description, task_id, registry, context=None):
        return copy.deepcopy(self._steps)

    def required_capabilities(self, goal_description):
        return set(self._required)


class _RetrySafeProbe:
    name = "probe.retry_safe"
    description = "retry-safe mutating probe"
    actions = [ActionSpec(
        name="apply",
        description="idempotent set",
        required_scope="probe:write",
        side_effects="mutating",
        reversible=True,
        idempotent=True,
        retry_safe=True,
        param_schema={"path": {"type": "string", "required": True},
                      "content": {"type": "string", "required": True}},
    )]

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        (self.sandbox / params["path"]).write_text(params["content"], encoding="utf-8")
        return {"body": "applied"}


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:write", "probe:write"},
        boundaries={FS: RelativePathBoundary()},
        risk_deny=set(),
        risk_approve={"high"},
    )


def _append_step():
    return PlanStep(
        index=0, intent="append suffix", capability="filesystem.append",
        action="append", scope="filesystem:write",
        params={"path": "journal.txt", "content": "X", "create": True},
        verification=VerificationPolicy("non_empty"),
    )


def _engine(tmp_path, planner, extra_caps=()):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    engine = build_engine(
        tmp_path / "arion.db", sandbox, planner=planner, policy=_policy(),
        approval_handler=AutoApproveHandler(), model_config=OFFLINE_MODEL_CONFIG,
    )
    engine.registry.register(FilesystemAppendCapability(sandbox))
    for cap in extra_caps:
        engine.registry.register(cap)
    return engine, sandbox


# --------------------------------------------------------------------- #
# 1. pre-dispatch: journaled replay-unsafe mutation without recovery
# --------------------------------------------------------------------- #

def test_dispatch_refused_when_recovery_storage_absent(tmp_path):
    """Storage has the intent journal, recovery storage is absent: the
    replay-unsafe mutation capability must NOT execute."""
    engine, sandbox = _engine(tmp_path, _FixedPlanner([_append_step()],
                                                     {"filesystem.append"}))
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        engine.recovery_store = _AbsentRecovery()
        goal = engine.submit_goal("append once")
        final = engine.run_goal(goal.id)

        assert final.status.value != "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status.value == "failed"
        assert "no usable recovery mechanism" in (task.error or "")
        # the capability never ran and no intent row was journaled
        assert engine.storage.list_mutation_intents() == []
    finally:
        engine.close()


def test_dispatch_refusal_message_names_the_invariant(tmp_path):
    engine, sandbox = _engine(tmp_path, _FixedPlanner([_append_step()],
                                                     {"filesystem.append"}))
    try:
        engine.recovery_store = _AbsentRecovery()
        goal = engine.submit_goal("append once")
        final = engine.run_goal(goal.id)
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status.value == "failed"
        assert "refusing to dispatch a journaled replay-unsafe mutation" in (
            task.error or ""
        )
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# 2. post-dispatch crash residue: stranded open intent, no recovery
# --------------------------------------------------------------------- #

def test_stranded_open_intent_without_recovery_fails_closed(tmp_path):
    """A crash after dispatch left an open intent; resume with NO recovery
    mechanism must fail closed - task FAILED, intent stays OPEN, nothing is
    replayed, the mutation is never marked complete."""
    engine, sandbox = _engine(tmp_path, _FixedPlanner([_append_step()],
                                                     {"filesystem.append"}))
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        goal = engine.submit_goal("append once")
        task = engine.create_task(goal)
        # Crash-after-dispatch residue: the intent was journaled before the
        # capability ran, then the process died (no outcome, no side effect
        # recorded here).
        engine.storage.record_mutation_intent(MutationIntent(
            intent_id="intent-crash-1", task_id=task.id, step_index=0,
            attempt=1, capability="filesystem.append", action="append",
            resource="journal.txt",
        ))
        engine.recovery_store = _AbsentRecovery()

        resumed = engine.run_task(task.id)

        assert resumed.status.value == "failed"
        assert "recovery required" in (resumed.error or "")
        assert "not replayed and not marked complete" in (resumed.error or "")
        # capability never ran on resume: no side effect, no completion
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        open_rows = engine.storage.list_mutation_intents(open_only=True)
        assert len(open_rows) == 1
        assert open_rows[0].status == INTENT_IN_FLIGHT
        assert open_rows[0].task_id == task.id
    finally:
        engine.close()


def test_resume_with_recovery_mechanism_fences_into_recovery(tmp_path):
    """The same crash residue WITH a usable recovery mechanism reaches the
    explicit recovery path: REQUIRED record, intent settled as recovery,
    and after acknowledgement a fresh plan finishes the work exactly once."""
    engine, sandbox = _engine(tmp_path, _FixedPlanner([_append_step()],
                                                     {"filesystem.append"}))
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        goal = engine.submit_goal("append once")
        task = engine.create_task(goal)
        engine.storage.record_mutation_intent(MutationIntent(
            intent_id="intent-crash-2", task_id=task.id, step_index=0,
            attempt=1, capability="filesystem.append", action="append",
            resource="journal.txt",
        ))
        # engine.recovery_store stays the real, usable registry

        resumed = engine.run_task(task.id)
        assert resumed.status.value == "failed"
        recoveries = engine.recovery_store.list_recoveries(task_id=task.id)
        assert len(recoveries) == 1
        assert recoveries[0].status.value == "required"

        engine.acknowledge_recovery(recoveries[0].recovery_id, actor="operator")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASEX"
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# 3. the invariant must not over-block
# --------------------------------------------------------------------- #

def test_retry_safe_mutation_still_dispatches_without_recovery(tmp_path):
    """Retry-safe mutations are NOT journaled (replay is their declared
    contract), so the recovery-capability invariant does not gate them."""
    probe_dir = tmp_path / "sandbox"
    planner = _FixedPlanner([PlanStep(
        index=0, intent="idempotent set", capability="probe.retry_safe",
        action="apply", scope="probe:write",
        params={"path": "state.txt", "content": "SET"},
        verification=VerificationPolicy("non_empty"),
    )], {"probe.retry_safe"})
    engine, sandbox = _engine(tmp_path, planner)
    probe = _RetrySafeProbe(sandbox)
    engine.registry.register(probe)
    try:
        engine.recovery_store = _AbsentRecovery()
        goal = engine.submit_goal("set state")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        assert probe.calls >= 1
        assert (sandbox / "state.txt").read_text(encoding="utf-8") == "SET"
        assert engine.storage.list_mutation_intents() == []
    finally:
        engine.close()
