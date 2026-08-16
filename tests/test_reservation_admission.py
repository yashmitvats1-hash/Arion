"""Reservation admission (ADR-029, Phase B) - tests first.

Deterministic, exact durable observations (no wall-clock timing):

- a reserved goal reaches its floor under sustained contention even when
  its DWRR weight is tiny (floor path bypasses DWRR credit);
- competitors are denied the slot the floor needs (protection path);
- high-weight goals cannot consume reserved capacity;
- equal reservations + unequal weights / unequal reservations + equal
  weights;
- reservation 0 / reservation == cap / multiple reserved goals;
- idle reserved goals release capacity; runnable-again re-engages;
- remaining capacity (after floors) still follows ADR-027 weighted
  fairness;
- floor claims respect the global cap, the scheduler fair share and the
  weight-disabled hard gate;
- reservation.satisfied telemetry when the floor is reached;
- reservation config changes never cancel RUNNING work.
"""

from __future__ import annotations

from arion.state.scheduler_work import SchedulerRegistryError, SchedulerWork, SchedulerWorkStatus
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
        out.append(reg.create(task_id=f"t-{goal_id}-{start + i}",
                              goal_id=goal_id, step_index=i,
                              scheduler_id=scheduler_id,
                              now=_iso_plus(T0, start + i)))
    return out


def _claim(reg, row, worker="w", now=T0) -> bool:
    got = reg.claim(row.work_id, worker_id=worker, lease_seconds=60.0,
                    now=now, max_lease_seconds=600.0,
                    scheduler_id=row.scheduler_id)
    return got is not None


def _complete(reg, row, worker="w", now=T0) -> None:
    reg.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                      now=now, owner_worker_id=worker)


def _running(reg) -> int:
    return len(reg.list_work(status=SchedulerWorkStatus.RUNNING))


def _running_for(reg, goal_id: str) -> int:
    return len([r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
                if r.goal_id == goal_id])


def _peek(reg, goal_id: str) -> SchedulerWork | None:
    for r in reg.list_work(status=SchedulerWorkStatus.QUEUED):
        if r.goal_id == goal_id:
            return r
    return None


def _fill(reg, goal_id: str) -> int:
    """Claim as many queued rows of `goal_id` as the gates allow.
    Returns the number of NEW claims."""
    claimed = 0
    while True:
        row = _peek(reg, goal_id)
        if row is None:
            break
        if not _claim(reg, row):
            break
        claimed += 1
        assert _running(reg) <= reg.get_scheduler_global_max()
    return claimed


# --------------------------------------------------------------------------- #
# core invariant: the floor is reached under sustained contention
# --------------------------------------------------------------------------- #


def test_high_weight_goal_cannot_consume_reserved_capacity(db_path: str):
    """Mission example: cap 8, A weight 8 res 0, B weight 1 res 2. B must
    occupy >= 2 slots; A gets the remaining capacity (6) via weights."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 40)
    _rows(reg, "goal-b", 20)
    # the hot goal fills first, then the reserved goal claims
    a_claimed = _fill(reg, "goal-a")
    b_claimed = _fill(reg, "goal-b")
    assert b_claimed >= 2, "reserved goal must reach its floor"
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 6
    assert _running(reg) == 8  # cap exactly
    # the floor holds: complete one B task; B's next claim is admitted via
    # the floor even though A still holds DWRR credit
    b_rows = [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-b"]
    _complete(reg, b_rows[0])
    assert _fill(reg, "goal-b") == 1
    assert _running_for(reg, "goal-b") == 2
    # A cannot claim while B is below its floor and slots are scarce
    before = _running_for(reg, "goal-a")
    assert _fill(reg, "goal-a") == 0
    assert _running_for(reg, "goal-a") == before
    reg.close()


def test_reservation_denied_event_and_row_stays_queued(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 10)
    a_claimed = _fill(reg, "goal-a")
    assert a_claimed == 4  # slots 5 and 6 are protected for B
    denied = [e for e in reg.scheduler_events(event_type="reservation.denied")]
    assert len(denied) == 1
    assert denied[0].detail["reason"] == "reservation"
    assert denied[0].detail["goal_id"] == "goal-a"
    assert denied[0].detail["pressure"] == 2
    # the denied rows are still QUEUED, nothing was partially claimed
    queued_a = [r for r in reg.list_work(status=SchedulerWorkStatus.QUEUED)
                if r.goal_id == "goal-a"]
    assert len(queued_a) == 6
    reg.close()


def test_floor_claims_bypass_dwrr_credit(db_path: str):
    """B (weight 1, res 2) is admitted to its floor while A holds DWRR
    credit and B's own credit is 0 - the floor is a guarantee, not an
    opportunity. No goal_weight.refill is emitted for floor claims."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 10)
    _fill(reg, "goal-a")          # A spends its credit (4 claims, cap 6)
    refills_before = len(reg.scheduler_events(event_type="goal_weight.refill"))
    assert _fill(reg, "goal-b") == 2  # floor claims succeed without refill
    refills_after = len(reg.scheduler_events(event_type="goal_weight.refill"))
    assert refills_after == refills_before
    assert _running_for(reg, "goal-b") == 2
    reg.close()


def test_reservation_satisfied_event(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 4)
    _rows(reg, "goal-b", 4)
    _fill(reg, "goal-b")
    satisfied = [e for e in reg.scheduler_events(
        event_type="reservation.satisfied")]
    assert len(satisfied) == 1
    assert satisfied[0].detail["goal_id"] == "goal-b"
    assert satisfied[0].detail["reservation"] == 2
    assert satisfied[0].detail["running"] == 2
    reg.close()


# --------------------------------------------------------------------------- #
# weight / reservation combinations
# --------------------------------------------------------------------------- #


def test_equal_reservation_unequal_weights(db_path: str):
    """A (w8, r2) vs B (w1, r2), cap 6: both floors hold; A still gets
    more of the remaining capacity."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 20)
    _rows(reg, "goal-b", 20)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    assert _running(reg) == 6
    reg.close()


def test_unequal_reservation_equal_weights(db_path: str):
    """A (w1, r1) vs B (w1, r3), cap 6, equal weights: B reaches its
    floor 3 (floor claims spend/refill credit normally); A's fill is
    DWRR-limited (protection only denies - it never grants)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-a", 1)
    reg.set_goal_reservation("goal-b", 3)
    _rows(reg, "goal-a", 20)
    _rows(reg, "goal-b", 20)
    a_filled = _fill(reg, "goal-a")
    b_filled = _fill(reg, "goal-b")
    assert b_filled == 3, "B's floor is guaranteed"
    assert _running_for(reg, "goal-b") == 3
    assert _running_for(reg, "goal-a") == a_filled  # unchanged by B's floor
    assert _running(reg) == 4
    reg.close()


def test_reservation_zero_behaves_like_adr027(db_path: str):
    """reservation 0: no floor, no protection - exact ADR-027 5:1 ratio."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-a", 0)
    reg.set_goal_reservation("goal-b", 0)
    _rows(reg, "goal-a", 30)
    _rows(reg, "goal-b", 30)
    claimed = {"goal-a": 0, "goal-b": 0}
    for _ in range(3):
        for g in ("goal-a", "goal-b"):
            while True:
                row = _peek(reg, g)
                if row is None or not _claim(reg, row):
                    break
                claimed[g] += 1
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed["goal-a"] == 5 * claimed["goal-b"], claimed
    assert claimed["goal-b"] >= 3, claimed
    reg.close()


def test_reservation_equal_to_cap(db_path: str):
    """B reserves the ENTIRE cap (6 of 6): while B is runnable and below
    its floor, A cannot claim a single slot."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 6)
    _rows(reg, "goal-a", 10)
    _rows(reg, "goal-b", 10)
    assert _fill(reg, "goal-a") == 0
    assert _fill(reg, "goal-b") == 6
    assert _running_for(reg, "goal-b") == 6
    # release one B slot: B re-claims it before A can take it
    b_rows = [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-b"]
    _complete(reg, b_rows[0])
    assert _fill(reg, "goal-a") == 0
    assert _fill(reg, "goal-b") == 1
    reg.close()


def test_multiple_reserved_goals(db_path: str):
    """A r2, B r2, C r1 (total 5 <= cap 6): all floors met; A (weight 8,
    first claimer) still cannot eat B/C's protected slots."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_reservation("goal-a", 2)
    reg.set_goal_reservation("goal-b", 2)
    reg.set_goal_reservation("goal-c", 1)
    for g in ("goal-a", "goal-b", "goal-c"):
        _rows(reg, g, 12)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    _fill(reg, "goal-c")
    assert _running_for(reg, "goal-a") == 3
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-c") == 1
    assert _running(reg) == 6
    reg.close()


# --------------------------------------------------------------------------- #
# idle / runnable-again
# --------------------------------------------------------------------------- #


def test_idle_reserved_goal_releases_capacity(db_path: str):
    """B has reservation 2 but NO work: A may use the full cap. When B
    becomes runnable, A's freed slots go to B (floor)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 1)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 10)
    assert _fill(reg, "goal-a") == 6  # idle B reserves nothing
    # B becomes runnable: complete two A tasks, B claims its floor
    a_rows = reg.list_work(status=SchedulerWorkStatus.RUNNING)
    _complete(reg, a_rows[0])
    _complete(reg, a_rows[1])
    _rows(reg, "goal-b", 10)
    assert _fill(reg, "goal-b") == 2
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    reg.close()


def test_reserved_goal_runnable_again_reengages_floor(db_path: str):
    """A reserved goal whose queued work drains loses its floor; when new
    work arrives the floor re-engages and competitors are pushed back."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-a", 8)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 10)
    b_rows = _rows(reg, "goal-b", 2)
    _fill(reg, "goal-a")
    _fill(reg, "goal-b")
    assert _running_for(reg, "goal-b") == 2
    # B's queue drains and its running work completes: B becomes idle
    for r in b_rows:
        _complete(reg, r)
    assert _fill(reg, "goal-a") == 2  # A reclaims the freed capacity
    assert _running_for(reg, "goal-b") == 0
    # new B work arrives: the floor re-engages (after A frees two slots)
    _rows(reg, "goal-b", 4, start=10)
    a_rows = [r for r in reg.list_work(status=SchedulerWorkStatus.RUNNING)
              if r.goal_id == "goal-a"]
    _complete(reg, a_rows[0])
    _complete(reg, a_rows[1])
    b_new = _fill(reg, "goal-b")
    assert b_new == 2
    assert _running_for(reg, "goal-b") == 2
    assert _running_for(reg, "goal-a") == 4
    reg.close()


# --------------------------------------------------------------------------- #
# DWRR on the remaining capacity
# --------------------------------------------------------------------------- #


def test_remaining_capacity_follows_dwrr(db_path: str):
    """Cap 8, A w5 r0, B w1 r2, C w1 r0, all runnable: B's floor (2)
    holds every round; the remaining capacity (6 = 5+1) is shared A:C
    exactly 5:1 per DWRR (B's floor claims spend/refill DWRR credit like
    any claim, so no credit strands and rounds keep firing)."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(8)
    reg.set_goal_weight("goal-a", 5)
    reg.set_goal_weight("goal-b", 1)
    reg.set_goal_weight("goal-c", 1)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-a", 40)
    _rows(reg, "goal-b", 20)
    _rows(reg, "goal-c", 40)
    claimed = {"goal-a": 0, "goal-b": 0, "goal-c": 0}
    for _ in range(3):
        for g in ("goal-a", "goal-b", "goal-c"):
            while True:
                row = _peek(reg, g)
                if row is None or not _claim(reg, row):
                    break
                claimed[g] += 1
        # the floor held throughout this round's claiming phase
        assert _running_for(reg, "goal-b") == 2
        assert _running(reg) <= 8
        for row in reg.list_work(status=SchedulerWorkStatus.RUNNING):
            _complete(reg, row)
    assert claimed["goal-a"] == 5 * claimed["goal-c"], claimed
    assert claimed["goal-c"] == 3, claimed  # exactly one C slot per round
    assert claimed["goal-b"] == 6, claimed  # exactly the floor every round
    reg.close()


# --------------------------------------------------------------------------- #
# gates the floor must NOT bypass
# --------------------------------------------------------------------------- #


def test_floor_respects_global_cap(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(2)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-b") == 2
    assert _fill(reg, "goal-b") == 0  # cap full: no floor beyond the cap
    assert _running(reg) == 2
    reg.close()


def test_floor_respects_scheduler_fair_share(db_path: str):
    """Two schedulers, cap 4 (share = 2 each). The floor may NOT bypass
    the scheduler fair share: sched-a's goal-b (res 3) reaches only its
    share (2) despite the 3-floor."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(4)
    reg.set_goal_reservation("goal-b", 3)
    _rows(reg, "goal-b", 6, scheduler_id="sched-a")
    _rows(reg, "goal-x", 1, scheduler_id="sched-b")  # active=2 => share 2
    a_rows = [r for r in reg.list_work(status=SchedulerWorkStatus.QUEUED)
              if r.scheduler_id == "sched-a"]
    assert _claim(reg, a_rows[0], worker="w-a", now=_iso_plus(T0, 2))
    assert _claim(reg, a_rows[1], worker="w-a", now=_iso_plus(T0, 2))
    assert _running_for(reg, "goal-b") == 2  # sched-a at its share
    # sched-a's third claim (would satisfy a 3-floor) is denied by the
    # FAIR SHARE gate, not admitted via the floor
    assert _claim(reg, a_rows[2], worker="w-a", now=_iso_plus(T0, 3)) is False
    share_denied = [e for e in reg.scheduler_events(
        event_type="scheduler_share.denied")]
    assert len(share_denied) >= 1
    assert _running_for(reg, "goal-b") == 2
    reg.close()


def test_floor_respects_weight_disabled_goal(db_path: str):
    """A weight-disabled goal (ADR-027 hard gate) is NEVER admitted, even
    with a reservation floor."""
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_weight("goal-b", 1, enabled=False)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 4)
    assert _fill(reg, "goal-b") == 0
    assert _running_for(reg, "goal-b") == 0
    denied = [e for e in reg.scheduler_events(event_type="goal_weight.denied")]
    assert len(denied) >= 1
    reg.close()


# --------------------------------------------------------------------------- #
# dynamic reservation changes
# --------------------------------------------------------------------------- #


def test_reservation_change_never_cancels_running_work(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 2)
    _fill(reg, "goal-b")
    before = reg.list_work(status=SchedulerWorkStatus.RUNNING)
    assert len(before) == 2
    # raise / remove / disable the reservation while work is RUNNING
    reg.set_goal_reservation("goal-b", 4)
    reg.set_goal_reservation_enabled("goal-b", False)
    reg.remove_goal_reservation("goal-b")
    after = reg.list_work(status=SchedulerWorkStatus.RUNNING)
    assert [(r.work_id, r.worker_id, r.lease_expires_at)
            for r in after] == [(r.work_id, r.worker_id, r.lease_expires_at)
                                for r in before]
    reg.close()


def test_cap_change_while_reservations_configured(db_path: str):
    reg = SQLiteStorage(db_path)
    reg.set_scheduler_global_max(6)
    reg.set_goal_reservation("goal-b", 2)
    _rows(reg, "goal-b", 2)
    _fill(reg, "goal-b")
    reg.set_scheduler_global_max(4)  # still >= total 2: legal
    assert reg.get_scheduler_global_max() == 4
    try:
        reg.set_scheduler_global_max(1)  # below total: rejected
    except SchedulerRegistryError:
        pass
    else:
        raise AssertionError("cap lowered below reservation total")
    assert reg.get_scheduler_global_max() == 4
    reg.close()
