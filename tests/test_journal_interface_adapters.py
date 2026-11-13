"""Fourth-pass F10: journal-interface completeness at dispatch and resume
(issue #23 fourth-pass work order, item 1).

A replay-unsafe mutation may only be dispatched when the storage exposes the
COMPLETE journal contract - durable recording AND reliable open-intent
enumeration AND point-read AND settlement.  Four adversarial adapters are
pinned here:

(a) record exists but list is missing  -> dispatch refused, zero capability
    invocations, nothing journaled;
(b) the enumerator raises              -> dispatch refused / resume fails
    closed, zero capability invocations;
(c) the enumerator returns malformed
    rows                               -> untrusted, fail closed;
(d) an open intent exists but
    enumeration becomes unavailable    -> resume fails closed with ZERO
    capability invocations and NO silent clearing/settlement of the intent.

Every case preserves the recovery-registry checks, the in-doubt fence,
fresh authorization and approval denial as they stand; the tests assert the
journal is never interpreted as "no open intents" when it cannot be read.
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


class _JournalAdapter:
    """Storage proxy that blocks or breaks selected journal methods and
    delegates everything else to the real storage underneath."""

    def __init__(self, inner, blocked=(), broken=None):
        self._inner = inner
        self._blocked = frozenset(blocked)
        self._broken = dict(broken or {})

    def __getattr__(self, name):
        if name in self._blocked:
            raise AttributeError(name)
        if name in self._broken:
            return self._broken[name]
        return getattr(self._inner, name)


def _raising_lister(**_kwargs):
    raise RuntimeError("enumeration unavailable (test)")


def _malformed_lister(**_kwargs):
    return [SimpleNamespace(step_index="not-an-int")]


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


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:write"},
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


def _engine(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    engine = build_engine(
        tmp_path / "arion.db", sandbox,
        planner=_FixedPlanner([_append_step()], {"filesystem.append"}),
        policy=_policy(), approval_handler=AutoApproveHandler(),
        model_config=OFFLINE_MODEL_CONFIG,
    )
    engine.registry.register(FilesystemAppendCapability(sandbox))
    return engine, sandbox


# --------------------------------------------------------------------- #
# (a) record without list
# --------------------------------------------------------------------- #

def test_record_without_enumerator_refuses_dispatch_zero_capability_calls(
    tmp_path,
):
    engine, sandbox = _engine(tmp_path)
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        inner = engine.storage
        engine.storage = _JournalAdapter(
            inner, blocked={"list_mutation_intents"},
        )
        goal = engine.submit_goal("append once")
        final = engine.run_goal(goal.id)
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status.value == "failed"
        assert "journal" in (task.error or "")
        # zero capability invocations: the append never ran
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        # nothing journaled, nothing settled, no recovery fabricated
        assert inner.list_mutation_intents() == []
        assert inner.list_recoveries() == []
    finally:
        engine.close()


def test_dispatch_guard_requires_complete_journal_interface(tmp_path):
    """The pre-dispatch guard validates the COMPLETE interface: a recorder
    without an enumerator refuses the dispatch before the capability runs."""
    from arion.orchestration.engine import MutationIntentError

    engine, sandbox = _engine(tmp_path)
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        goal = engine.submit_goal("append once")
        task = engine.create_task(goal)
        step = _append_step()
        spec = engine.registry.action_spec("filesystem.append", "append")
        inner = engine.storage
        engine.storage = _JournalAdapter(
            inner, blocked={"list_mutation_intents"},
        )
        with pytest.raises(MutationIntentError) as excinfo:
            engine._persist_mutation_intent(task, step, spec)
        assert "incomplete mutation-intent journal interface" in str(
            excinfo.value
        )
        assert "missing list_mutation_intents" in str(excinfo.value)
        # the capability must not have run and no row must exist
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        assert inner.list_mutation_intents() == []
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# (b) enumerator raises  /  (c) malformed enumeration
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "lister", [_raising_lister, _malformed_lister],
    ids=["enumerator-raises", "malformed-rows"],
)
def test_untrusted_enumeration_refuses_dispatch_and_fails_closed(
    tmp_path, lister,
):
    engine, sandbox = _engine(tmp_path)
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        inner = engine.storage
        engine.storage = _JournalAdapter(
            inner, broken={"list_mutation_intents": lister},
        )
        goal = engine.submit_goal("append once")
        final = engine.run_goal(goal.id)
        task = engine.goal_manager.task_history(goal.id)[-1]
        # fail closed before any capability runs
        assert task.status.value == "failed"
        assert "journal" in (task.error or "")
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        # the enumerator lied / raised: nothing journaled or settled
        assert inner.list_mutation_intents() == []
        assert inner.list_recoveries() == []
    finally:
        engine.close()


@pytest.mark.parametrize(
    "lister", [_raising_lister, _malformed_lister, lambda **_: None],
    ids=["enumerator-raises", "malformed-rows", "enumerator-returns-none"],
)
def test_dispatch_guard_probes_enumeration_before_recording(
    tmp_path, lister,
):
    """Dispatch probes the enumerator and refuses when it is absent, raises,
    or returns an invalid result - before the journal row and before any
    capability runs."""
    from arion.orchestration.engine import MutationIntentError

    engine, sandbox = _engine(tmp_path)
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        goal = engine.submit_goal("append once")
        task = engine.create_task(goal)
        step = _append_step()
        spec = engine.registry.action_spec("filesystem.append", "append")
        inner = engine.storage
        engine.storage = _JournalAdapter(
            inner, broken={"list_mutation_intents": lister},
        )
        with pytest.raises(MutationIntentError) as excinfo:
            engine._persist_mutation_intent(task, step, spec)
        assert "enumeration is unusable" in str(excinfo.value)
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        assert inner.list_mutation_intents() == []
    finally:
        engine.close()


# --------------------------------------------------------------------- #
# (d) open intent exists, enumeration becomes unavailable
# --------------------------------------------------------------------- #

def test_open_intent_with_enumeration_gone_fails_closed_no_settlement(
    tmp_path,
):
    """A previously recorded open intent exists but enumeration becomes
    unavailable.  Resume must fail closed with ZERO capability invocations
    and NO silent clearing or settlement of the intent."""
    engine, sandbox = _engine(tmp_path)
    try:
        (sandbox / "journal.txt").write_text("BASE", encoding="utf-8")
        goal = engine.submit_goal("append once")
        task = engine.create_task(goal)
        # Crash-after-dispatch residue: the intent was journaled before the
        # capability ran, then enumeration became unavailable.
        engine.storage.record_mutation_intent(MutationIntent(
            intent_id="intent-crash-4th", task_id=task.id, step_index=0,
            attempt=1, capability="filesystem.append", action="append",
            resource="journal.txt",
        ))
        inner = engine.storage
        engine.storage = _JournalAdapter(
            inner, broken={"list_mutation_intents": _raising_lister},
        )

        resumed = engine.run_task(task.id)

        # fail closed; zero capability invocations on resume
        assert resumed.status.value == "failed"
        assert "mutation journal unreadable" in (resumed.error or "")
        assert (sandbox / "journal.txt").read_text(encoding="utf-8") == "BASE"
        # the intent was NOT cleared or settled and no recovery fabricated
        rows = inner.list_mutation_intents(open_only=True)
        assert len(rows) == 1
        assert rows[0].status == INTENT_IN_FLIGHT
        assert rows[0].recovery_id is None
        assert inner.list_recoveries() == []
    finally:
        engine.close()
