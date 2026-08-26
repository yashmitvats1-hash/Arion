"""Durable goal-lifecycle compare-and-swap invariant.

A previous defect let two independent writers corrupt authoritative
goal state:

    Worker A: load_goal() -> version N
    Worker B: load_goal() -> version N
    Worker A: mutate (pause)      -> save_goal()   INSERT OR REPLACE
    Worker B: mutate (set_blocked)-> save_goal()   INSERT OR REPLACE

The second writer silently replaced the first. Distinct blockers were
lost, a progress/strategy patch could roll a committed COMPLETED /
CANCELLED row back to ACTIVE, and ``goal.state.changed`` could be
emitted for a write that never became canonical.

This module proves the DURABLE guarantee:

- stale full-row writes never overwrite a newer committed lifecycle;
- a successful lifecycle transition increments ``goal.version`` exactly
  once and emits ``goal.state.changed`` only after the CAS commits;
- a CAS miss reloads the canonical row and revalidates;
- an illegal transition after a race fails closed;
- concurrent additions of distinct blocker keys merge;
- progress / strategy patches cannot clobber a committed lifecycle
  transition;
- retries are bounded and fail closed under persistent contention.

Concurrency is proven with genuinely independent SQLite connections
and a real-subprocess race on a shared database.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.observability.events import EventLogger
from arion.state.models import Goal, GoalStateError, GoalStatus
from arion.state.store import SQLiteStorage

from conftest import MemorySink

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _gm(db_path, events=None):
    storage = SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def _blocker_keys(goal: Goal) -> set[str]:
    return {(b.get("key") or b.get("type")) for b in (goal.blockers or [])}


def _install_first_load_barrier(stores, barrier):
    """Synchronize two (or more) stores on their FIRST load_goal.

    Both workers observe the same version N, then proceed to mutate.
    Subsequent loads (CAS-miss reload / post-commit refresh) do not
    wait, so the retry path can observe the winner's committed row.
    """
    for store in stores:
        orig = store.load_goal
        seen = {"done": False}

        def _load(goal_id, orig=orig, seen=seen):
            goal = orig(goal_id)
            if not seen["done"]:
                seen["done"] = True
                barrier.wait(timeout=30)
            return goal

        store.load_goal = _load


# --------------------------------------------------------------------------- #
# 1. Independent-connection pause vs blocked
# --------------------------------------------------------------------------- #


def test_pause_vs_blocked_independent_connections(tmp_path):
    """pause() and set_blocked() race on the same ACTIVE goal.

    Legal committed outcomes (reload + revalidate):

    - pause commits first: PAUSED with the blocker merged (PAUSED ->
      BLOCKED is illegal, so the blocker attaches without flipping
      status);
    - set_blocked commits first: BLOCKED with the blocker; pause then
      fails closed (BLOCKED -> PAUSED is illegal).

    Lost-update is illegal: PAUSED with no blocker, or ACTIVE.
    """
    db = tmp_path / "pvb.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: dict[str, BaseException] = {}
    results: dict[str, Goal] = {}

    def pause_w():
        try:
            results["pause"] = gm_a.pause(gid)
        except BaseException as exc:
            errors["pause"] = exc

    def block_w():
        try:
            results["block"] = gm_b.set_blocked(
                gid, {"key": "network", "type": "network"})
        except BaseException as exc:
            errors["block"] = exc

    t_a = threading.Thread(target=pause_w)
    t_b = threading.Thread(target=block_w)
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    keys = _blocker_keys(final)
    assert "network" in keys, (
        f"blocker lost under pause vs blocked: status={final.status_value} "
        f"blockers={final.blockers} errors={errors}"
    )
    assert final.status_value in ("paused", "blocked")
    assert final.status_value != "active"
    if final.status_value == "blocked":
        assert "pause" in errors
        assert isinstance(errors["pause"], GoalStateError)
    else:
        assert "block" not in errors
        assert final.status_value == "paused"
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 2. Independent-connection distinct blocker merge
# --------------------------------------------------------------------------- #


def test_distinct_blockers_merge_independent_connections(tmp_path):
    """Two independent actors add different blocker keys. Both survive."""
    db = tmp_path / "blk.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: list[BaseException] = []

    def worker(gm, key):
        try:
            gm.set_blocked(gid, {"key": key, "type": key})
        except BaseException as exc:
            errors.append(exc)

    t_a = threading.Thread(target=worker, args=(gm_a, "network"))
    t_b = threading.Thread(target=worker, args=(gm_b, "approval"))
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value == "blocked"
    assert _blocker_keys(final) == {"network", "approval"}
    # create=1, two committed blocker writes
    assert final.version == 3
    added = [b.get("added_at") for b in final.blockers]
    assert all(added)
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 3. Stale lifecycle transition cannot overwrite canonical state
# --------------------------------------------------------------------------- #


def test_stale_lifecycle_transition_cannot_overwrite(tmp_path):
    """A CAS writer holding version N cannot replace a newer committed row."""
    db = tmp_path / "stale.db"
    seed = SQLiteStorage(db)
    seed.save_goal(Goal(id="g1", description="inspect"))
    seed.close()

    store_a = SQLiteStorage(db)
    store_b = SQLiteStorage(db)
    snap_a = store_a.load_goal("g1")
    snap_b = store_b.load_goal("g1")
    assert snap_a is not None and snap_b is not None
    expected = snap_a.version

    snap_b.status = GoalStatus.COMPLETED
    snap_b.version = expected + 1
    assert store_b.cas_goal(snap_b, expected) is True

    snap_a.status = GoalStatus.PAUSED
    snap_a.version = expected + 1
    assert store_a.cas_goal(snap_a, expected) is False

    witness = SQLiteStorage(db)
    final = witness.load_goal("g1")
    assert final is not None
    assert final.status_value == "completed"
    assert final.version == expected + 1
    witness.close()
    store_a.close()
    store_b.close()


# --------------------------------------------------------------------------- #
# 4. CAS miss reloads and revalidates
# --------------------------------------------------------------------------- #


def test_cas_miss_reloads_and_revalidates(tmp_path):
    """A stale pause snapshot must reload, see COMPLETED, and fail closed
    rather than writing PAUSED over the canonical row."""
    db = tmp_path / "reload.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id

    orig = st_a.load_goal
    first = {"done": False}

    def _load(goal_id):
        goal = orig(goal_id)
        if not first["done"]:
            first["done"] = True
            gm_b.complete_goal(goal_id)
        return goal

    st_a.load_goal = _load
    with pytest.raises(GoalStateError, match="invalid goal transition"):
        gm_a.pause(gid)

    final = st_b.load_goal(gid)
    assert final is not None
    assert final.status_value == "completed"
    # only the successful complete transition bumped the version
    assert final.version == 2
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 5. Illegal transition after a race fails closed
# --------------------------------------------------------------------------- #


def test_illegal_transition_after_race_fails_closed(tmp_path):
    """complete vs cancel: both destinations are terminal. Exactly one
    commits; the loser revalidates and fails closed."""
    db = tmp_path / "term.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: dict[str, BaseException] = {}
    results: dict[str, Goal] = {}

    def complete_w():
        try:
            results["complete"] = gm_a.complete_goal(gid)
        except BaseException as exc:
            errors["complete"] = exc

    def cancel_w():
        try:
            results["cancel"] = gm_b.cancel(gid)
        except BaseException as exc:
            errors["cancel"] = exc

    t_a = threading.Thread(target=complete_w)
    t_b = threading.Thread(target=cancel_w)
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value in ("completed", "cancelled")
    assert final.version == 2
    assert len(errors) == 1
    assert len(results) == 1
    loser = next(iter(errors.values()))
    assert isinstance(loser, GoalStateError)
    assert "invalid goal transition" in str(loser)
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 6. Version increments exactly once per successful lifecycle transition
# --------------------------------------------------------------------------- #


def test_version_increments_once_per_lifecycle_transition(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "ver.db")
    gid = gm.create_goal("inspect").id
    assert gm.get_goal(gid).version == 1

    gm.pause(gid)
    assert gm.get_goal(gid).version == 2
    assert gm.get_goal(gid).status_value == "paused"

    gm.resume(gid)
    assert gm.get_goal(gid).version == 3
    assert gm.get_goal(gid).status_value == "active"

    # fail_goal used to write twice (transition + last_replan_reason).
    # The CAS path must increment exactly once and still persist the reason.
    failed = gm.fail_goal(gid, reason="boom")
    assert failed.version == 4
    assert failed.status_value == "failed"
    assert failed.last_replan_reason == "boom"
    reloaded = gm.get_goal(gid)
    assert reloaded.version == 4
    assert reloaded.last_replan_reason == "boom"
    storage.close()
    cognitive.close()


# --------------------------------------------------------------------------- #
# 7. Metadata / progress patch cannot clobber lifecycle state
# --------------------------------------------------------------------------- #


def test_progress_patch_cannot_clobber_lifecycle(tmp_path):
    """evaluate() racing complete_goal() must leave the goal COMPLETED.
    The progress snapshot is merged onto the latest row, never used to
    resurrect ACTIVE."""
    db = tmp_path / "prog.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: list[BaseException] = []

    def eval_w():
        try:
            gm_a.evaluate(gid)
        except BaseException as exc:
            errors.append(exc)

    def complete_w():
        try:
            gm_b.complete_goal(gid)
        except BaseException as exc:
            errors.append(exc)

    t_a = threading.Thread(target=eval_w)
    t_b = threading.Thread(target=complete_w)
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value == "completed"
    assert isinstance(final.progress_metadata, dict)
    assert final.progress_metadata.get("goal_id") == gid
    # complete increments the CAS token; evaluate is column-scoped
    assert final.version >= 2
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 8. Strategy patch cannot clobber lifecycle state
# --------------------------------------------------------------------------- #


def test_strategy_patch_cannot_clobber_lifecycle(tmp_path):
    db = tmp_path / "strat.db"
    gm_a, st_a, c_a = _gm(db)
    gm_b, st_b, c_b = _gm(db)
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: list[BaseException] = []

    def strat_w():
        try:
            gm_a.strategy_for(gid, "inspect", [], {}, [])
        except BaseException as exc:
            errors.append(exc)

    def cancel_w():
        try:
            gm_b.cancel(gid)
        except BaseException as exc:
            errors.append(exc)

    t_a = threading.Thread(target=strat_w)
    t_b = threading.Thread(target=cancel_w)
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value == "cancelled"
    assert final.strategy == "direct"
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 9. Exactly one event for one successful competing transition
# --------------------------------------------------------------------------- #


def test_one_event_for_one_successful_competing_transition(tmp_path):
    """Two workers both pause the same ACTIVE goal. Exactly one
    ``goal.state.changed`` is emitted (the loser fails closed)."""
    db = tmp_path / "evt.db"
    sink = MemorySink()
    st_a = SQLiteStorage(db)
    st_b = SQLiteStorage(db)
    c_a = SQLiteCognitiveStore(db)
    c_b = SQLiteCognitiveStore(db)
    gm_a = GoalManager(
        storage=st_a, cognitive_store=c_a,
        events=EventLogger(sinks=[st_a, sink]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    gm_b = GoalManager(
        storage=st_b, cognitive_store=c_b,
        events=EventLogger(sinks=[st_b, sink]),
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    gid = gm_a.create_goal("inspect").id
    barrier = threading.Barrier(2)
    _install_first_load_barrier([st_a, st_b], barrier)
    errors: list[BaseException] = []

    def worker(gm):
        try:
            gm.pause(gid)
        except GoalStateError:
            pass
        except BaseException as exc:
            errors.append(exc)

    t_a = threading.Thread(target=worker, args=(gm_a,))
    t_b = threading.Thread(target=worker, args=(gm_b,))
    t_a.start(); t_b.start(); t_a.join(timeout=60); t_b.join(timeout=60)
    assert not errors, errors

    assert sink.count("goal.state.changed") == 1
    changed = sink.by_kind("goal.state.changed")[0]
    assert changed.detail["to"] == "paused"
    assert changed.detail["from"] == "active"
    assert changed.detail["goal_version"] == 2

    witness = SQLiteStorage(db)
    persisted = [e for e in witness.list_events() if e.kind == "goal.state.changed"]
    assert len(persisted) == 1
    assert persisted[0].detail["to"] == "paused"
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value == "paused"
    assert final.version == 2
    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


# --------------------------------------------------------------------------- #
# 10. Repeated contention remains durable
# --------------------------------------------------------------------------- #


def test_repeated_contention_remains_durable(tmp_path):
    """N concurrent distinct blockers all survive, and a writer that can
    never CAS fails closed without mutating the canonical row."""
    db = tmp_path / "many.db"
    seed_gm, seed_st, seed_c = _gm(db)
    gid = seed_gm.create_goal("inspect").id
    seed_st.close(); seed_c.close()

    n = 6
    keys = [f"blocker_{i}" for i in range(n)]
    gms = []
    stores = []
    cognitives = []
    for _ in range(n):
        gm, st, cog = _gm(db)
        gms.append(gm)
        stores.append(st)
        cognitives.append(cog)
    barrier = threading.Barrier(n)
    _install_first_load_barrier(stores, barrier)
    errors: list[BaseException] = []

    def worker(gm, key):
        try:
            gm.set_blocked(gid, {"key": key, "type": key})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(gms[i], keys[i]))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert final.status_value == "blocked"
    assert _blocker_keys(final) == set(keys)
    assert final.version == 1 + n
    witness.close()

    # Persistent CAS miss on a still-legal transition: the row is
    # untouched and no event is emitted.
    sink = MemorySink()
    gm_fail, st_fail, c_fail = _gm(db, events=EventLogger(sinks=[sink]))
    fresh = gm_fail.create_goal("still active")
    st_fail.cas_goal_fields = lambda *a, **k: False
    st_fail.cas_goal = lambda *a, **k: False
    before = gm_fail.get_goal(fresh.id)
    with pytest.raises(GoalStateError, match="persistent contention"):
        gm_fail.pause(fresh.id)
    after = SQLiteStorage(db).load_goal(fresh.id)
    assert after is not None and before is not None
    assert after.status_value == "active"
    assert after.version == before.version == 1
    assert sink.count("goal.state.changed") == 0
    st_fail.close(); c_fail.close()
    for st, cog in zip(stores, cognitives):
        st.close(); cog.close()


# --------------------------------------------------------------------------- #
# 11. Real subprocess / shared-database race
# --------------------------------------------------------------------------- #


_HELPER = textwrap.dedent(r"""
    import json, os, sys, time
    sys.path.insert(0, %r)
    from arion.cognition.goals import GoalManager
    from arion.cognition.progress import DeterministicProgressEvaluator
    from arion.cognition.store import SQLiteCognitiveStore
    from arion.cognition.strategy import StrategySelector
    from arion.state.store import SQLiteStorage
    db, op, barrier_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    storage = SQLiteStorage(db)
    cognitive = SQLiteCognitiveStore(db)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    ready = os.path.join(barrier_dir, "ready-" + op)
    open(ready, "w").close()
    for _ in range(200):
        if (os.path.exists(os.path.join(barrier_dir, "ready-pause"))
                and os.path.exists(os.path.join(barrier_dir, "ready-block"))):
            break
        time.sleep(0.01)
    gid = open(os.path.join(barrier_dir, "goal_id")).read().strip()
    try:
        if op == "pause":
            g = gm.pause(gid)
        else:
            g = gm.set_blocked(gid, {"key": "network", "type": "network"})
        print(json.dumps({
            "ok": True,
            "status": g.status_value,
            "version": g.version,
            "blockers": [b.get("key") for b in (g.blockers or [])],
        }))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "msg": str(exc),
        }))
    storage.close()
    cognitive.close()
""" % str(REPO))


# --------------------------------------------------------------------------- #
# 12. ADR-056 fenced terminal race: cancel vs fenced complete_goal
# --------------------------------------------------------------------------- #


def test_adr056_fenced_complete_vs_concurrent_cancel_fails_closed(tmp_path):
    """concurrent cancel vs complete_goal(..., expect_plan_version=v1).

    The production engine always supplies ``expect_plan_version`` when
    completing a goal, which routes through ``cas_goal_terminal_fenced``
    (ADR-056 atomic path) instead of the legacy ``_commit_goal`` path.
    This test proves that the same goal-version CAS invariant holds on
    the fenced path:

    - exactly one terminal state commits;
    - the loser receives a ``GoalStateError`` from the retry loop (it
      reloads the now-CANCELLED row and cannot transition to COMPLETED);
    - goal.version is incremented exactly once;
    - ``cas_goal_terminal_fenced`` is provably exercised (not the legacy
      path), verified by asserting on the monkeypatched call count.

    Race injection
    --------------
    We monkeypatch ``storage.cas_goal_terminal_fenced`` so that on the
    FIRST call the concurrent cancel is committed by a second GoalManager,
    and then ``("cas_miss", None)`` is returned.  This deterministically
    forces the transition() retry loop to reload the goal, observe
    CANCELLED, and raise ``GoalStateError("invalid goal transition
    'cancelled' -> 'completed'")`` — all without any timing dependence
    or sleep.
    """
    db = tmp_path / "adr056-race.db"
    gm_a, st_a, c_a = _gm(db)          # will call complete_goal
    gm_b, st_b, c_b = _gm(db)          # will commit the concurrent cancel

    # Establish a plan version so complete_goal can supply expect_plan_version.
    gid = gm_a.create_goal("inspect").id
    plan = gm_a.record_plan_version(
        gid, "direct",
        [{"index": 0, "intent": "x", "capability": "filesystem.read",
          "action": "read", "scope": "filesystem:read", "params": {},
          "verification": {"policy": "non_empty", "args": {}},
          "depends_on": [], "guidance": [], "skipped_reason": None,
          "max_attempts": 1}],
        reason="initial_plan",
    )
    v1 = plan["plan_version"]

    # ── Injection ─────────────────────────────────────────────────────────────
    # Intercept the first call to cas_goal_terminal_fenced:
    #   1. the real method has NOT been called yet (goal still ACTIVE);
    #   2. gm_b commits a cancel (bumps goal.version, status -> CANCELLED);
    #   3. return ("cas_miss", None) — as if the goal version changed;
    #   4. transition() retries, reloads: status=CANCELLED,
    #      "completed" not in GOAL_TRANSITIONS["cancelled"] -> GoalStateError.
    real_fenced = st_a.cas_goal_terminal_fenced
    injection_calls: list[tuple] = []          # record every call for verification

    def _injecting_fenced(goal_id, expected_goal_version, expect_plan_version, fields):
        injection_calls.append((goal_id, expected_goal_version,
                                expect_plan_version, fields.get("status")))
        if len(injection_calls) == 1:
            # First call: commit the concurrent cancel before returning.
            gm_b.cancel(goal_id)
            return ("cas_miss", None)
        # Subsequent calls (should not happen after GoalStateError, but guard):
        return real_fenced(goal_id, expected_goal_version, expect_plan_version, fields)

    st_a.cas_goal_terminal_fenced = _injecting_fenced

    # ── Exercise ──────────────────────────────────────────────────────────────
    with pytest.raises(GoalStateError, match="invalid goal transition") as exc_info:
        gm_a.complete_goal(gid, reason="all_work_complete",
                           expect_plan_version=v1)

    # ── Verify the ADR-056 path was taken ─────────────────────────────────────
    assert len(injection_calls) >= 1, (
        "cas_goal_terminal_fenced was never called; "
        "complete_goal may have used the legacy path"
    )
    # The first call must have carried the fenced status and plan version.
    first_goal_id, first_version, first_plan_v, first_status = injection_calls[0]
    assert first_goal_id == gid
    assert first_plan_v == v1, (
        f"expect_plan_version mismatch: want {v1}, got {first_plan_v}"
    )
    assert first_status == "completed"

    # ── Verify authoritative goal state ───────────────────────────────────────
    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None

    # The cancel must be the sole committed terminal state.
    assert final.status_value == "cancelled", (
        f"expected cancelled (cancel committed first), got {final.status_value}"
    )

    # goal.version must have advanced exactly once: ACTIVE(1) -> CANCELLED(2).
    # The failed complete_goal must NOT have incremented it further.
    assert final.version == 2, (
        f"goal.version should be 2 (one cancel commit); got {final.version}"
    )

    # The exception must name the CANCELLED -> COMPLETED illegal transition.
    assert "cancelled" in str(exc_info.value).lower() or "invalid goal transition" in str(exc_info.value)

    witness.close()
    st_a.close(); st_b.close(); c_a.close(); c_b.close()


def test_subprocess_pause_vs_blocked(tmp_path):
    """Two real processes share one database: pause vs set_blocked.

    The durable topology is the authority. Same legal outcomes as the
    in-process race; last-writer-wins is illegal.
    """
    db = str(tmp_path / "xproc.db")
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    seed, storage, cognitive = _gm(db)
    gid = seed.create_goal("inspect").id
    (barrier_dir / "goal_id").write_text(gid, encoding="utf-8")
    storage.close()
    cognitive.close()

    p_a = subprocess.Popen(
        [sys.executable, "-c", _HELPER, db, "pause", str(barrier_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p_b = subprocess.Popen(
        [sys.executable, "-c", _HELPER, db, "block", str(barrier_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out_a, err_a = p_a.communicate(timeout=60)
    out_b, err_b = p_b.communicate(timeout=60)
    assert p_a.returncode == 0, err_a
    assert p_b.returncode == 0, err_b

    witness = SQLiteStorage(db)
    final = witness.load_goal(gid)
    assert final is not None
    assert "network" in _blocker_keys(final), (
        f"subprocess race lost the blocker: status={final.status_value} "
        f"blockers={final.blockers} pause={out_a!r} block={out_b!r} "
        f"err_a={err_a!r} err_b={err_b!r}"
    )
    assert final.status_value in ("paused", "blocked")
    witness.close()
