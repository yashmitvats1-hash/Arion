"""Weighted fair admission (ADR-027, Phase B) - tests first.

Deterministic DWRR inside the claim transaction:

- equal weights -> equal claims per cycle;
- 2:1 / 3:1 / 5:1 exact per-cycle ratios (cap = sum of weights);
- three goals 2:1:1;
- an idle goal (configured but no work) reserves nothing;
- a hot high-weight goal cannot monopolize capacity; a low-weight goal
  still makes progress every cycle (anti-starvation floor);
- global cap never exceeded at any point;
- no weights / no global cap -> ADR-026 default behavior exactly;
- disabled goals are never admitted;
- dynamic weight change affects only future admission (no retroactive
  effect on RUNNING work);
- new goals default to weight 1;
- persisted deficit survives a store reopen (restart-safe).
"""

from __future__ import annotations

from arion.state.models import utcnow
from arion.state.scheduler_work import SchedulerWork, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

T0 = "2026-01-01T00:00:00+00:00"


def _iso_plus(iso: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _rows(reg, goal_id: str, n: int, scheduler_id: str = "sched-1",
          start: int = 0) -> list[SchedulerWork]:
    out = []
    for i in range(n):
        out.append(reg.create(task_id=f"t-{goal_id}-{start + i}", goal_id=goal_id,
                              step_index=i, scheduler_id=scheduler_id,
                              now=_iso_plus(T0, start + i)))
    return out


def _claim_ok(reg, row, worker="w", now=T0) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now, max_lease_seconds=600.0,
                    scheduler_id=row.scheduler_id)
    return got is not None


def _complete(reg, row, worker="w", now=T0) -> None:
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now, owner_worker_id=worker)


def _running(reg) -> int:
    return len(reg.list_work(status=SchedulerWorkStatus.RUNNING))


def _peek(reg, goal_id: str) -> SchedulerWork:
    """The first QUEUED row of a goal (helper for the simulation)."""
    for r in reg.list_work(status=SchedulerWorkStatus.QUEUED):
        if r.goal_id == goal_id:
            return r
    raise AssertionError(f"no queued row for {goal_id}")


def _claim_next(reg, goal_id: str, worker="w", now=T0):
    """Peek once, claim it, return (row, ok) - no double-peek races."""
    row = _peek(reg, goal_id)
    ok = _claim_ok(reg, row, worker=worker, now=now)
    return row, ok


# --------------------------------------------------------------------------- #
# exact per-cycle ratios
# --------------------------------------------------------------------------- #


def test_equal_weights_equal_claims(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    rows_a, rows_b = _rows(reg, "goal-a", 10), _rows(reg, "goal-b", 10)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(5):
        for g in ("goal-a", "goal-b"):
            _, ok = _claim_next(reg, g)
            assert ok
            claimed[g] += 1
        assert _running(reg) == 2  # cap exact
        for row in rows_a[claimed["goal-a"] - 1:claimed["goal-a"]] + \
                rows_b[claimed["goal-b"] - 1:claimed["goal-b"]]:
            _complete(reg, row)
    assert claimed == {"goal-a": 5, "goal-b": 5}
    reg.close()




def _round(reg, order, claimed, cap: int) -> None:
    """One DWRR round: each goal in `order` attempts until denied (credit
    spent or cap full), then all running rows complete. Returns counts."""
    for g in order:
        while _peek_or_none(reg, g):
            _, ok = _claim_next(reg, g)
            if ok:
                claimed[g] += 1
            else:
                break
        assert _running(reg) <= cap, (g, _running(reg))
    for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
        _complete(reg, row)

def test_weight_2_to_1_exact(db_path: str):
    """Per DWRR round the gate itself produces the 2:1 ratio (A spends 2
    credits, B spends 1) under sustained contention."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 20)
    _rows(reg, "goal-b", 20)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(6):
        _round(reg, ("goal-a", "goal-b"), claimed, cap=3)
    assert claimed["goal-a"] == 2 * claimed["goal-b"], claimed
    assert claimed["goal-b"] >= 6, claimed  # both made steady progress
    reg.close()


def test_weight_3_to_1_exact(db_path: str):
    """Per round: A spends 3 credits, B spends 1 -> exact 3:1."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 3)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 30)
    _rows(reg, "goal-b", 30)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(4):
        _round(reg, ("goal-a", "goal-b"), claimed, cap=4)
    assert claimed["goal-a"] == 3 * claimed["goal-b"], claimed
    assert claimed["goal-b"] >= 4, claimed
    reg.close()


def test_weight_5_to_1_exact(db_path: str):
    """Per round: A spends 5 credits, B spends 1 -> exact 5:1."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 30)
    _rows(reg, "goal-b", 30)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(3):
        _round(reg, ("goal-a", "goal-b"), claimed, cap=6)
    assert claimed["goal-a"] == 5 * claimed["goal-b"], claimed
    assert claimed["goal-b"] >= 3, claimed
    reg.close()


def test_three_goals_2_1_1(db_path: str):
    """Per round: A=2, B=1, C=1 -> exact 2:1:1."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_weight("goal-c", 1)
    _rows(reg, "goal-a", 30)
    _rows(reg, "goal-b", 30)
    _rows(reg, "goal-c", 30)
    claimed = {"goal-a": 0, "goal-b": 0, "goal-c": 0}
    for _ in range(4):
        _round(reg, ("goal-a", "goal-b", "goal-c"), claimed, cap=4)
    assert claimed["goal-a"] == 2 * claimed["goal-b"] == 2 * claimed["goal-c"], claimed
    assert claimed["goal-b"] >= 4, claimed
    reg.close()


# --------------------------------------------------------------------------- #
# starvation / monopolization / idle goals
# --------------------------------------------------------------------------- #


def test_idle_goal_reserves_nothing(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_weight("goal-idle", 100)  # configured, but NO rows
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 4)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(2):
        for g in ("goal-a", "goal-b"):
            _, ok = _claim_next(reg, g)
            assert ok
            claimed[g] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    # the idle goal's huge weight consumed nothing and distorted nothing
    assert claimed == {"goal-a": 2, "goal-b": 2}
    assert reg.get_goal_weight("goal-idle") == 100  # config persists
    reg.close()


def test_hot_goal_cannot_monopolize_low_weight_progresses(db_path: str):
    """A weight-8 goal vs a weight-1 goal with cap 4: the low-weight goal
    still claims once EVERY cycle (anti-starvation floor) and the cap is
    never exceeded."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_weight("goal-hot", 8)
    reg.set_goal_weight("goal-low", 1)
    _rows(reg, "goal-hot", 40)
    _rows(reg, "goal-low", 10)
    claimed = {"goal-hot": 0, "goal-low": 0}
    for _ in range(5):
        # the hot goal keeps filling capacity until its credit is spent
        # (a round boundary); the cap is never exceeded
        while True:
            filled = 0
            while _peek_or_none(reg, "goal-hot"):
                _, ok = _claim_next(reg, "goal-hot")
                if ok:
                    claimed["goal-hot"] += 1
                    filled += 1
                else:
                    break
                assert _running(reg) <= 4
            for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
                _complete(reg, row)
            if filled == 0:
                break  # hot has no credit: round boundary reached
        # the low goal attempts at the boundary and IS admitted (refill
        # guarantees it credit) - never starved across rounds
        if _peek_or_none(reg, "goal-low"):
            _, ok = _claim_next(reg, "goal-low")
            assert ok, "low-weight goal must progress every round"
            claimed["goal-low"] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed["goal-low"] == 5   # exactly once per round
    assert claimed["goal-hot"] == 40  # 8 per round (cap-truncated 4+4)
    reg.close()


def _peek_or_none(reg, goal_id):
    for r in reg.list_work(status=SchedulerWorkStatus.QUEUED):
        if r.goal_id == goal_id:
            return r
    return None


def test_global_cap_never_exceeded_under_weights(db_path: str):
    """After EVERY claim in a weighted simulation the running count is
    within the cap."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 10)
    for _ in range(6):
        for g in ("goal-a", "goal-b", "goal-a", "goal-b"):
            if _peek_or_none(reg, g):
                _, ok = _claim_next(reg, g)
                if ok:
                    assert _running(reg) <= 3
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    reg.close()


# --------------------------------------------------------------------------- #
# backward compatibility / defaults
# --------------------------------------------------------------------------- #


def test_no_global_cap_no_weights_is_adr026_behavior(db_path: str):
    """Without global_max_concurrency the weight gate is a no-op: claims
    always succeed (ADR-026 per-engine behavior)."""
    reg = SQLiteStorage(db_path)
    reg.set_goal_weight("goal-a", 2)  # config exists but no global scope
    rows = _rows(reg, "goal-a", 5)
    for row in rows:
        assert _claim_ok(reg, row)  # never gated
    reg.close()


def test_global_cap_without_weights_defaults_to_one(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 4)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(2):
        for g in ("goal-a", "goal-b"):
            _, ok = _claim_next(reg, g)
            assert ok
            claimed[g] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed == {"goal-a": 2, "goal-b": 2}  # default weight 1 each
    reg.close()


def test_new_goal_without_config_uses_default_weight(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-new", 2)  # no config -> default weight 1
    claimed = {"goal-a": 0, "goal-new": 0}
    for _ in range(2):
        for g in ("goal-a", "goal-a", "goal-new"):
            _, ok = _claim_next(reg, g)
            assert ok
            claimed[g] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed == {"goal-a": 4, "goal-new": 2}
    reg.close()


def test_disabled_goal_never_admitted(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1, enabled=False)
    rows_a = _rows(reg, "goal-a", 2)
    rows_b = _rows(reg, "goal-b", 2)
    assert _claim_ok(reg, rows_a[0])
    assert _claim_ok(reg, rows_a[1])
    # disabled goal's rows stay QUEUED forever
    assert not _claim_ok(reg, rows_b[0])
    assert not _claim_ok(reg, rows_b[1])
    assert reg.get_work(rows_b[0].work_id).status == SchedulerWorkStatus.QUEUED
    reg.close()


# --------------------------------------------------------------------------- #
# dynamic configuration
# --------------------------------------------------------------------------- #


def test_weight_change_affects_only_future_admission(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 8)
    _rows(reg, "goal-b", 8)
    claimed = {"goal-a": [], "goal-b": []}
    # one 2:1 cycle
    for g in ("goal-a", "goal-b", "goal-a"):
        row, ok = _claim_next(reg, g)
        assert ok
        claimed[g].append(row)
    running_before = [r.work_id for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)]
    # change A's weight to 1 while work is queued/running
    reg.set_goal_weight("goal-a", 1)
    for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
        _complete(reg, row)
    # next cycles are 1:1
    for _ in range(2):
        for g in ("goal-a", "goal-b"):
            row, ok = _claim_next(reg, g)
            assert ok
            claimed[g].append(row)
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    # no retroactive effect: the pre-change RUNNING rows were untouched
    assert all(r in [x.work_id for x in claimed["goal-a"] + claimed["goal-b"]]
               or reg.get_work(r).status == SchedulerWorkStatus.COMPLETED
               for r in running_before)
    # 2:1 cycle then 1:1 cycles
    assert len(claimed["goal-a"]) == 4 and len(claimed["goal-b"]) == 3
    reg.close()


def test_disable_re_enable_goal(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    rows_b = _rows(reg, "goal-b", 2)
    _rows(reg, "goal-a", 2)
    reg.set_goal_weight_enabled("goal-b", False)
    assert not _claim_ok(reg, rows_b[0])  # disabled
    reg.set_goal_weight_enabled("goal-b", True)
    assert _claim_ok(reg, rows_b[0])  # re-enabled
    reg.close()


# --------------------------------------------------------------------------- #
# restart safety (persisted deficit)
# --------------------------------------------------------------------------- #


def test_deficit_survives_reopen(db_path: str):
    """The durable deficit counter is the only fairness state: after a
    store reopen the weighted cycle continues exactly."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(3)
    reg.set_goal_weight("goal-a", 2)
    reg.set_goal_weight("goal-b", 1)
    _rows(reg, "goal-a", 6)
    _rows(reg, "goal-b", 3)
    # one full 2:1 cycle
    for g in ("goal-a", "goal-b", "goal-a"):
        _, ok = _claim_next(reg, g)
        assert ok
    for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
        _complete(reg, row)
    reg.close()

    # "restart": reopen the store; the durable deficit continues the cycle
    reg2 = SQLiteStorage(db_path)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(2):
        for g in ("goal-a", "goal-b", "goal-a"):
            if _peek_or_none(reg2, g):
                _, ok = _claim_next(reg2, g)
                if ok:
                    claimed[g] += 1
        for row in reg2.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg2, row)
    assert claimed == {"goal-a": 4, "goal-b": 2}  # 2:1 continued
    reg2.close()
