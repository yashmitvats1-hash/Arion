#!/usr/bin/env python3
"""ADR-026 DoD demo: cross-process shared scheduler with lease-based ownership.

Multiple Arion engine processes share ONE scheduler/work registry database.
Ownership is real:

- unique durable scheduler registration per process (lease + heartbeat);
- every dispatched work item is atomically CLAIMED (BEGIN IMMEDIATE) with
  a bounded worker lease - two processes racing for one item -> exactly one
  owner;
- heartbeats are ownership-checked, monotonic, bounded; a worker that stops
  heartbeating becomes reclaimable; a stale owner can never complete work
  after its lease expired or was reassigned;
- optional durable global_max_concurrency is enforced at claim time across
  ALL processes (N engines cannot become N x max_concurrency);
- release_and_claim_next provides an atomic handoff;
- crash recovery: dead registrations -> abandoned queues; expired leases ->
  reclaimed; completed mutations never replay.

  A  registration + atomic claim + heartbeat primitives (bounded/monotonic).
  B  two processes race claim_next on one queued item -> exactly one owner.
  C  global capacity: two engines x local mc=2, durable cap=2 -> never 4.
  D  heartbeats keep ownership; a stopped heartbeat -> expiry -> reclaim.
  E  stale owner rejected: no heartbeat, no completion after reclaim.
  F  release_and_claim_next atomic handoff (owner-checked).
  G  crash recovery: process death while RUNNING + while QUEUED.
  H  restart with multiple goals: durable state, no duplicate mutation.

Deterministic and offline: no LLM, no network, no shell. Cross-process
atomicity is proven with real SQLite transactions (two store handles) and
REAL subprocesses in tests/test_multi_process_scheduler.py.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import PendingApprovalHandler, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, VerificationPolicy
from arion.state.scheduler_work import SchedulerStateError, SchedulerWorkStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
CHECKS = 0
T0 = "2026-01-01T00:00:00+00:00"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def _iso_plus(iso: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


class SlowRead(FilesystemReadCapability):
    def __init__(self, sandbox, sleep=0.15):
        super().__init__(sandbox)
        self.sleep = sleep
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.started = []

    def execute(self, action, params):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(params.get("path"))
        try:
            time.sleep(self.sleep)
            return super().execute(action, params)
        finally:
            with self.lock:
                self.active -= 1


class SlowWrite(FilesystemWriteCapability):
    def __init__(self, sandbox, sleep=0.05):
        super().__init__(sandbox)
        self.sleep = sleep
        self.calls = []

    def execute(self, action, params):
        self.calls.append(params.get("path"))
        time.sleep(self.sleep)
        return super().execute(action, dict(params))


class StepPlanner:
    def __init__(self, factory):
        self._factory = factory

    def plan(self, goal_description, task_id, registry, context=None):
        steps = self._factory(goal_description)
        return [PlanStep(index=i, intent=s[0], capability=s[1], action=s[2],
                         scope=s[3], params=dict(s[4]), verification=s[5],
                         depends_on=[])
                for i, s in enumerate(steps)]

    def required_capabilities(self, goal_description):
        return {s[1] for s in self._factory(goal_description)}


def _read(path):
    return (f"read {path}", "filesystem.read", "read", "filesystem:read", {"path": path},
            VerificationPolicy("schema_keys", {"keys": ["content"]}))


def _write(path, content="x"):
    return (f"write {path}", "filesystem.write", "write", "filesystem:write",
            {"path": path, "content": content, "overwrite": True},
            VerificationPolicy("write_verified"))


def _engine(db, sb, planner, read_cap=None, write_cap=None, max_concurrency=2,
            global_max=None, lease=300.0, max_lease=None):
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(read_cap or FilesystemReadCapability(sb))
    registry.register(write_cap or FilesystemWriteCapability(sb))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(), risk_approve=set(),
        boundaries={FS: RelativePathBoundary()},
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy, approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=max_concurrency, lock_wait_max_seconds=0.0,
        scheduler_lease_seconds=lease, scheduler_max_lease_seconds=max_lease,
        scheduler_global_max_concurrency=global_max,
    )
    return engine, gm, storage


def _submit(engine, description):
    gid = engine.submit_goal(description).id
    engine._plan_for_goal(gid)
    return gid


def main() -> int:
    print("ADR-026 demo: cross-process shared scheduler (lease ownership)\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr026-"))
    sb = tmp / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    (sb / "b.txt").write_text("b", encoding="utf-8")
    db = tmp / "adr026.db"

    # ---------------------------------------------------------------- A -----
    print("A. registration + atomic claim + heartbeats (bounded/monotonic)")
    store = SQLiteStorage(db)
    store.register_scheduler("sched-A", pid=1001, lease_seconds=60.0, now=T0)
    check(store.scheduler_registration_live("sched-A", now=T0),
          "A: scheduler registration is live after registration")
    check(store.heartbeat_scheduler("sched-A", lease_seconds=60.0,
                                    now=_iso_plus(T0, 30), max_lease_seconds=300.0),
          "A: registration heartbeat extends the lease")
    check(store.scheduler_registration_live("sched-A", now=_iso_plus(T0, 89)),
          "A: extended registration stays live")
    row = store.create(task_id="t1", goal_id=None, step_index=0,
                       scheduler_id="sched-A", now=T0)
    claimed = store.claim(row.work_id, worker_id="worker:1", lease_seconds=60.0,
                          now=T0, max_lease_seconds=120.0)
    check(claimed.status == SchedulerWorkStatus.RUNNING
          and claimed.worker_id == "worker:1",
          "A: atomic claim -> RUNNING with owner + lease")
    hb = store.heartbeat(row.work_id, "worker:1", lease_seconds=60.0,
                         now=_iso_plus(T0, 10), max_lease_seconds=120.0)
    check(hb.lease_expires_at == _iso_plus(T0, 70),
          "A: in-window heartbeat extends the lease (monotonic)")
    try:
        store.heartbeat(row.work_id, "worker:1", lease_seconds=60.0,
                        now="2099-01-01T00:00:00+00:00", max_lease_seconds=120.0)
        check(False, "A: forged future heartbeat must be rejected")
    except SchedulerStateError:
        check(True, "A: forged future heartbeat rejected (cannot extend)")
    try:
        store.heartbeat(row.work_id, "worker-evil", lease_seconds=60.0,
                        now=_iso_plus(T0, 11), max_lease_seconds=120.0)
        check(False, "A: forged worker heartbeat must be rejected")
    except SchedulerStateError:
        check(True, "A: forged worker heartbeat rejected (ownership checked)")
    store.mark_terminal(row.work_id, SchedulerWorkStatus.COMPLETED,
                        now=T0, owner_worker_id="worker:1")
    check(store.get_work(row.work_id).status == SchedulerWorkStatus.COMPLETED,
          "A: owner completes the work normally")
    store.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. two processes race one queued item -> exactly one owner")
    store = SQLiteStorage(db)
    race = store.create(task_id="t-race", goal_id=None, step_index=0,
                        scheduler_id="sched-shared", now=T0)
    store.close()
    outcomes = []

    def race_claim(worker):
        s = SQLiteStorage(db)
        try:
            got = s.claim_next("sched-shared", worker_id=worker, lease_seconds=60.0,
                               now=_iso_plus(T0, 1), max_lease_seconds=600.0)
            outcomes.append((worker, got.work_id if got else None))
        except Exception as exc:  # pragma: no cover
            outcomes.append((worker, f"ERR:{exc}"))
        finally:
            s.close()

    threads = [threading.Thread(target=race_claim, args=("worker-p1",)),
               threading.Thread(target=race_claim, args=("worker-p2",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    winners = [w for w, wid in outcomes if wid == race.work_id]
    check(len(winners) == 1, f"B: exactly one owner won the race ({outcomes})")
    store = SQLiteStorage(db)
    final = store.get_work(race.work_id)
    check(final.status == SchedulerWorkStatus.RUNNING
          and final.worker_id == winners[0],
          "B: the row is RUNNING under exactly the winning worker")
    store.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. global capacity across two engines: never 2+2")
    db_c = tmp / "adr026c.db"
    shared_cap = SlowRead(sb, sleep=0.12)
    engine_a, gm_a, st_a = _engine(db_c, sb, StepPlanner(
        lambda d: [_read("a.txt") if "a" in d else _read("b.txt")]),
        read_cap=shared_cap, max_concurrency=2, global_max=2, lease=2.0)
    engine_b, gm_b, st_b = _engine(db_c, sb, StepPlanner(
        lambda d: [_read("a.txt") if "a" in d else _read("b.txt")]),
        read_cap=shared_cap, max_concurrency=2, global_max=2, lease=2.0)
    check(engine_a.scheduler_id != engine_b.scheduler_id,
          "C: two processes have distinct durable scheduler ids")
    check(engine_a.scheduler_registry.get_scheduler_global_max() == 2,
          "C: durable global_max_concurrency configured once, shared")
    ga1, ga2 = _submit(engine_a, "a1"), _submit(engine_a, "a2")
    gb1, gb2 = _submit(engine_b, "b1"), _submit(engine_b, "b2")
    results_c: dict[str, GoalStatus] = {}
    errors: list[str] = []

    def run_c(engine, gids):
        try:
            for _ in range(200):
                out = engine.run_goals(gids)
                for g, goal in out.items():
                    results_c[g] = goal.status
                if all(results_c.get(g) == GoalStatus.COMPLETED for g in gids):
                    return
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    t1 = threading.Thread(target=run_c, args=(engine_a, [ga1, ga2]))
    t2 = threading.Thread(target=run_c, args=(engine_b, [gb1, gb2]))
    t1.start(); t2.start(); t1.join(timeout=90); t2.join(timeout=90)
    check(not errors, "C: both engines ran without errors")
    check(shared_cap.max_active <= 2,
          f"C: global cap held - never more than 2 concurrent (was "
          f"{shared_cap.max_active})")
    check(all(results_c.get(g) == GoalStatus.COMPLETED for g in
              (ga1, ga2, gb1, gb2)),
          "C: all four goals completed (no permanent capacity loss)")
    engine_a.shutdown(); engine_b.shutdown()
    st_a.close(); st_b.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. stopped heartbeat -> lease expiry -> reclaim")
    db_d = tmp / "adr026d.db"
    store = SQLiteStorage(db_d)
    d_row = store.create(task_id="t-d", goal_id=None, step_index=0,
                         scheduler_id="sched-D", now=T0)
    store.claim(d_row.work_id, worker_id="worker-D", lease_seconds=0.4, now=T0,
                max_lease_seconds=600.0)
    store.heartbeat(d_row.work_id, "worker-D", lease_seconds=0.4,
                    now=_iso_plus(T0, 0.1), max_lease_seconds=600.0)
    check(store.get_work(d_row.work_id).lease_expires_at > _iso_plus(T0, 0.2),
          "D: heartbeat kept the lease alive")
    store.close()
    time.sleep(0.6)  # the worker stops heartbeating -> lease lapses
    store = SQLiteStorage(db_d)
    check(store.get_work(d_row.work_id).status == SchedulerWorkStatus.RUNNING,
          "D: the row is still RUNNING (lazy reclaim)")
    reclaimed = store.reclaim_stale()
    check(reclaimed == [d_row.work_id]
          and store.get_work(d_row.work_id).status == SchedulerWorkStatus.ABANDONED,
          "D: expired lease reclaimed -> ABANDONED (no immortal RUNNING)")
    store.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. stale owner rejected after expiry/reclaim")
    store = SQLiteStorage(db)
    e_row = store.create(task_id="t-e", goal_id=None, step_index=0,
                         scheduler_id="sched-E", now=T0)
    store.claim(e_row.work_id, worker_id="worker-E", lease_seconds=1.0, now=T0,
                max_lease_seconds=600.0)
    store.reclaim_stale(now=_iso_plus(T0, 2))
    try:
        store.heartbeat(e_row.work_id, "worker-E", lease_seconds=60.0,
                        now=_iso_plus(T0, 3), max_lease_seconds=600.0)
        check(False, "E: stale owner heartbeat must be rejected")
    except SchedulerStateError:
        check(True, "E: stale owner cannot heartbeat a reclaimed row")
    try:
        store.mark_terminal(e_row.work_id, SchedulerWorkStatus.COMPLETED,
                            now=_iso_plus(T0, 3), owner_worker_id="worker-E")
        check(False, "E: stale owner completion must be rejected")
    except SchedulerStateError:
        check(True, "E: stale owner cannot complete a reclaimed row")
    store.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. release_and_claim_next atomic handoff")
    store = SQLiteStorage(db)
    fa = store.create(task_id="t-f1", goal_id=None, step_index=0,
                      scheduler_id="sched-F", now=T0)
    fb = store.create(task_id="t-f2", goal_id=None, step_index=0,
                      scheduler_id="sched-F", now=_iso_plus(T0, 1))
    store.claim(fa.work_id, worker_id="worker-F", lease_seconds=60.0, now=T0,
                max_lease_seconds=600.0)
    terminal, nxt = store.release_and_claim_next(
        fa.work_id, owner_worker_id="worker-F", status=SchedulerWorkStatus.COMPLETED,
        error=None, scheduler_id="sched-F", worker_id="worker-F",
        lease_seconds=60.0, now=_iso_plus(T0, 2), max_lease_seconds=600.0)
    check(terminal.status == SchedulerWorkStatus.COMPLETED,
          "F: handoff completed the finished row")
    check(nxt is not None and nxt.work_id == fb.work_id
          and nxt.status == SchedulerWorkStatus.RUNNING,
          "F: handoff atomically claimed the next queued row")
    try:
        store.release_and_claim_next(
            fb.work_id, owner_worker_id="worker-stale", status=SchedulerWorkStatus.COMPLETED,
            error=None, scheduler_id="sched-F", worker_id="worker-stale",
            lease_seconds=60.0, now=_iso_plus(T0, 3), max_lease_seconds=600.0)
        check(False, "F: stale handoff must be rejected")
    except SchedulerStateError:
        check(True, "F: stale owner handoff rejected (ownership checked)")
    store.mark_terminal(fb.work_id, SchedulerWorkStatus.COMPLETED,
                        now=_iso_plus(T0, 3), owner_worker_id="worker-F")
    store.close()

    # ---------------------------------------------------------------- G -----
    print("\nG. crash recovery: death while QUEUED and while RUNNING")
    db_g = tmp / "adr026g.db"
    store = SQLiteStorage(db_g)
    store.register_scheduler("sched-crashed", pid=777, lease_seconds=0.3)
    q_row = store.create(task_id="t-q", goal_id=None, step_index=0,
                         scheduler_id="sched-crashed", now=T0)
    store.close()
    time.sleep(0.5)  # the crashed process never heartbeats again
    store = SQLiteStorage(db_g)
    check(not store.scheduler_registration_live("sched-crashed"),
          "G: crashed process's registration lapsed")
    check(store.abandon_foreign_queued("sched-alive") == 1
          and store.get_work(q_row.work_id).status == SchedulerWorkStatus.ABANDONED,
          "G: dead process's QUEUED row abandoned (live peers untouched)")
    store.close()

    db_g2 = tmp / "adr026g2.db"
    store = SQLiteStorage(db_g2)
    r_row = store.create(task_id="t-r", goal_id=None, step_index=0,
                         scheduler_id="sched-shared", now=T0)
    store.claim(r_row.work_id, worker_id="worker-crashed", lease_seconds=0.4,
                now=T0, max_lease_seconds=600.0)
    store.close()
    time.sleep(0.6)  # process died while RUNNING; lease lapses
    store = SQLiteStorage(db_g2)
    check(store.reclaim_stale() == [r_row.work_id]
          and store.get_work(r_row.work_id).status == SchedulerWorkStatus.ABANDONED,
          "G: expired RUNNING lease reclaimed (no immortal RUNNING)")
    store.close()

    # ---------------------------------------------------------------- H -----
    print("\nH. restart with multiple goals: durable state, no duplicate mutation")
    db_h = tmp / "adr026h.db"
    wc = SlowWrite(sb, sleep=0.01)
    engine_h, gm_h, st_h = _engine(db_h, sb, StepPlanner(
        lambda d: ([_write("a.txt")] if "A" in d else [_write("b.txt")])),
        write_cap=wc, max_concurrency=2, global_max=2)
    g_a = _submit(engine_h, "goal A")
    g_b = _submit(engine_h, "goal B")
    ra = engine_h.run_goals([g_a])          # A completes; B never ran
    check(ra[g_a].status == GoalStatus.COMPLETED
          and (sb / "a.txt").read_text(encoding="utf-8") == "x",
          "H: goal A's mutation completed before the 'crash'")
    engine_h.shutdown()
    st_h.close()

    # fresh engine on the same DB == process restart (new scheduler id)
    wc2 = SlowWrite(sb, sleep=0.01)
    engine_h2, gm_h2, st_h2 = _engine(db_h, sb, StepPlanner(
        lambda d: ([_write("a.txt")] if "A" in d else [_write("b.txt")])),
        write_cap=wc2, max_concurrency=2, global_max=2)
    check(engine_h2.scheduler_id != engine_h.scheduler_id,
          "H: restarted engine has a fresh durable scheduler identity")
    check(engine_h2._registered and st_h2.scheduler_registration_live(
        engine_h2.scheduler_id),
          "H: restarted engine registered itself durably")
    results_h = engine_h2.run_goals([g_a, g_b])
    check(results_h[g_a].status == GoalStatus.COMPLETED,
          "H: goal A still completed after restart")
    check(results_h[g_b].status == GoalStatus.COMPLETED,
          "H: goal B completed on the restarted engine")
    attempts = [e for e in st_h2.list_events() if e.kind == "mutation.attempted"]
    check(len(attempts) == 2, "H: exactly two mutations - A never replayed")
    check(wc.calls == ["a.txt"] and wc2.calls == ["b.txt"],
          "H: each engine mutated exactly its own file once")
    engine_h2.shutdown()
    st_h2.close()

    print("\n" + "=" * 78)
    print(f"ADR-026 demo PASSED ({CHECKS} checks) - cross-process shared scheduler")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
