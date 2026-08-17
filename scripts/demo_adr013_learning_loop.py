#!/usr/bin/env python3
"""ADR-013 DoD demo: learning loop integration.

The full learning lifecycle over the settled scheduler layer:

  execution outcome -> durable episode (task-keyed, exactly one) ->
  reflection -> consolidation -> retrieval -> guidance -> future planning

Deterministic and offline: fixed sandbox files, no wall-clock races.

  A  task execution (success + failure)
  B  episode creation (structured, bounded, no param values)
  C  successful outcome episode
  D  failed outcome episode (failure recorded, salient importance)
  E  reflection (deterministic; linked to the episode)
  F  consolidation (repeated lessons merged; history never deleted)
  G  retrieval (related task receives prior experience)
  H  learned context enters planning (Plan A != Plan B via guidance)
  I  unrelated memory exclusion
  J  restart persistence (episodes/reflections survive reopen)
  K  retry/idempotency (repeated recording -> one episode per task)
  L  reflection failure recovery (hostile reflector -> deterministic
     fallback; loop survives)
  M  concurrent learning protection (task-keyed uniqueness)
  N  adversarial memory injection (forged content changes nothing)
  O  scheduler-authority isolation (learning never touches scheduler)
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability  # noqa: E402
from arion.capabilities.registry import CapabilityRegistry  # noqa: E402
from arion.intelligence.planner import DeterministicPlanner  # noqa: E402
from arion.intelligence.router import DeterministicRouter  # noqa: E402
from arion.memory.models import Episode  # noqa: E402
from arion.memory.retrieval import MemoryRetriever, build_planning_context  # noqa: E402
from arion.memory.store import SQLiteMemoryStore  # noqa: E402
from arion.observability.events import EventLogger  # noqa: E402
from arion.orchestration.authz import (  # noqa: E402
    Actor,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine  # noqa: E402
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy  # noqa: E402
from arion.state.store import SQLiteStorage  # noqa: E402

FS = "filesystem:path"

_checks = 0


def check(cond: bool, msg: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        raise SystemExit(f"  FAIL: {msg}")
    print(f"  ok: {msg}")


def _engine(db_path, sandbox, memory=True, reflector=None, boundary=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    mem = SQLiteMemoryStore(db_path) if memory else None
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: boundary or RelativePathBoundary()}),
        actor=Actor.agent("system"),
        memory=mem, reflector=reflector,
    ), mem


def _eps_for(memory, task_id: str) -> list:
    return [e for e in memory.list_recent(limit=1000) if e.task_id == task_id]


def main() -> int:
    global _checks
    print("ADR-013 demo: learning loop integration\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr013-"))
    (tmp / "sb").mkdir()
    sandbox = tmp / "sb"
    (sandbox / "notes.txt").write_text("safe notes", encoding="utf-8")
    (sandbox / "README.md").write_text("secret-ish readme", encoding="utf-8")

    # ---------------------------------------------------------------- A -----
    print("A. task execution")
    db = tmp / "a.db"
    engine, memory = _engine(db, sandbox)
    ok_task = engine.execute_goal("summarize this repository")
    check(ok_task.status == TaskStatus.COMPLETED,
          "A: successful task executed and completed")
    goal = engine.submit_goal("read a missing file")
    bad_task = engine.create_task(goal)
    bad_task.steps = [PlanStep(
        index=0, intent="read", capability="filesystem.read", action="read",
        scope="filesystem:read", params={"path": "nope.txt"},
        verification=VerificationPolicy("non_empty"))]
    engine.storage.save_task(bad_task)
    bad_task = engine.run_task(bad_task.id)
    check(bad_task.status == TaskStatus.FAILED,
          "A: failing task executed and failed (durable)")
    engine.storage.close()
    memory.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. episode creation")
    engine, memory = _engine(db, sandbox)
    eps = _eps_for(memory, ok_task.id)
    check(len(eps) == 1 and eps[0].outcome == "completed"
          and eps[0].task_id == ok_task.id,
          "B: one structured episode per task")
    check(all("nope.txt" not in str(s.get("params_keys", []))
              for s in eps[0].plan_summary),
          "B: plan_summary stores param KEY names, never values")
    check(eps[0].lifecycle == "consolidated" and eps[0].reflection_id,
          "B: lifecycle completed and reflection linked")

    # ---------------------------------------------------------------- C -----
    print("\nC. successful outcome episode")
    check(eps[0].outcome == "completed" and eps[0].importance == 0.5,
          "C: completed outcome with default salience")

    # ---------------------------------------------------------------- D -----
    print("\nD. failed outcome episode")
    failed_eps = _eps_for(memory, bad_task.id)
    check(len(failed_eps) == 1 and failed_eps[0].outcome == "failed"
          and failed_eps[0].importance >= 0.6,
          "D: failed outcome recorded with elevated importance")
    check(failed_eps[0].failures
          and "nope.txt" in failed_eps[0].failures[0]["error"],
          "D: failure detail bounded and recorded")

    # ---------------------------------------------------------------- E -----
    print("\nE. reflection")
    ref = memory.get_reflection(eps[0].reflection_id)
    check(ref is not None and ref.confidence in ("low", "medium", "high")
          and ref.lesson and ref.recommendation,
          "E: deterministic reflection produced and stored")
    check(ref.episode_id == eps[0].episode_id,
          "E: reflection linked to its episode")

    # ---------------------------------------------------------------- F -----
    print("\nF. consolidation")
    # run the same goal twice more -> similar lessons; consolidation
    # merges repeated lessons into explicit records (history kept)
    engine.execute_goal("summarize this repository")
    engine.execute_goal("summarize this repository")
    cons = memory.list_consolidations(limit=100)
    check(len(cons) >= 1 and all(c.source_episode_ids for c in cons),
          "F: consolidation produced explicit records with provenance")
    episodes_all = memory.list_recent(limit=100)
    check(len([e for e in episodes_all if e.task_id == ok_task.id]) == 1,
          "F: history preserved - the original episode still exists")

    # ---------------------------------------------------------------- G -----
    print("\nG. retrieval")
    ctx = build_planning_context(MemoryRetriever(memory),
                                 "summarize this repository")
    check(len(ctx.episodes) >= 1 and ctx.provenance["episode_ids"],
          "G: related task retrieves prior experience")
    check(len(ctx.digest()["episodes"]) <= ctx.budget.max_episodes,
          "G: retrieved context is bounded by the budget")

    # ---------------------------------------------------------------- H -----
    print("\nH. learned context enters planning")
    from arion.capabilities.registry import CapabilityRegistry as CR

    class DenyReadmeBoundary:
        def allows(self, resource: str) -> bool:
            return resource != "README.md"

    engine2, memory2 = _engine(tmp / "h.db", sandbox,
                               boundary=DenyReadmeBoundary())
    planner = DeterministicPlanner()
    registry = CR()
    registry.register(FilesystemReadCapability(sandbox))
    plan_a = planner.plan("inspect this repository", "task_a", registry,
                          context=None)
    # seed experience: denied README.md + completed notes.txt
    engine2.execute_goal("inspect this repository")
    goal2 = engine2.submit_goal("read the notes file")
    t2 = engine2.create_task(goal2)
    t2.steps = [PlanStep(
        index=0, intent="read notes", capability="filesystem.read",
        action="read", scope="filesystem:read", params={"path": "notes.txt"},
        verification=VerificationPolicy("schema_keys",
                                        {"keys": ["content"]}))]
    engine2.storage.save_task(t2)
    engine2.run_task(t2.id)
    ctx2 = build_planning_context(MemoryRetriever(memory2),
                                  "inspect this repository")
    plan_b = planner.plan("inspect this repository", "task_b", registry,
                          context=ctx2)
    check(plan_a[1].params["path"] == "README.md"
          and plan_b[1].params["path"] == "notes.txt",
          "H: prior experience re-targeted the plan (README -> notes)")
    cats = {g.category for g in ctx2.guidance}
    check("avoid" in cats and "prefer" in cats
          and ctx2.provenance["guidance_ids"],
          "H: guidance + provenance drove the change")
    engine2.storage.close()
    memory2.close()

    # ---------------------------------------------------------------- I -----
    print("\nI. unrelated memory exclusion")
    ctx3 = build_planning_context(
        MemoryRetriever(memory),
        "fetch the weather forecast from https://api.example.com/weather",
        capabilities={"http.get"})
    check(ctx3.episodes == [],
          "I: different-capability tasks receive no filesystem memory")

    # ---------------------------------------------------------------- J -----
    print("\nJ. restart persistence")
    engine.storage.close()
    memory.close()
    engine_r, memory_r = _engine(db, sandbox)
    check(_eps_for(memory_r, ok_task.id)
          and memory_r.get_reflection(eps[0].reflection_id) is not None,
          "J: episodes and reflections survive a reopen")
    ctx_r = build_planning_context(MemoryRetriever(memory_r),
                                   "summarize this repository")
    check(len(ctx_r.episodes) >= 1,
          "J: retrieval works after restart")
    engine_r.storage.close()
    memory_r.close()

    # ---------------------------------------------------------------- K -----
    print("\nK. retry/idempotency")
    engine, memory = _engine(tmp / "k.db", sandbox)
    task_k = engine.execute_goal("summarize this repository")
    engine._record_memory(task_k)
    engine._record_memory(task_k)  # repeated lifecycle invocation
    check(len(_eps_for(memory, task_k.id)) == 1,
          "K: repeated recording yields exactly one episode")
    refs = memory.list_recent_reflections(limit=100)
    check(len([r for r in refs
               if r.episode_id == _eps_for(memory, task_k.id)[0].episode_id])
          == 1,
          "K: exactly one reflection per episode")
    engine.storage.close()
    memory.close()

    # ---------------------------------------------------------------- L -----
    print("\nL. reflection failure recovery")
    engine, memory = _engine(tmp / "l.db", sandbox)

    class EvilReflector:
        def reflect(self, episode):
            raise RuntimeError("model output poisoned")

    engine.reflector = EvilReflector()
    task_l = engine.execute_goal("summarize this repository")
    check(task_l.status == TaskStatus.COMPLETED,
          "L: hostile reflector does not break the loop")
    eps_l = _eps_for(memory, task_l.id)
    check(len(eps_l) == 1 and eps_l[0].reflection_id,
          "L: deterministic fallback produced the reflection")
    engine.storage.close()
    memory.close()

    # ---------------------------------------------------------------- M -----
    print("\nM. concurrent learning protection")
    db_m = tmp / "m.db"
    engine, memory = _engine(db_m, sandbox)
    task_m = engine.execute_goal("summarize this repository")
    engine.storage.close()
    from arion.memory.lifecycle import build_episode_from_task
    storage = SQLiteStorage(db_m)
    saved = storage.load_task(task_m.id)
    storage.close()
    m1, m2 = SQLiteMemoryStore(db_m), SQLiteMemoryStore(db_m)

    def rec(mem):
        try:
            mem.record_episode(build_episode_from_task(saved))
        except Exception:
            pass

    ts = [threading.Thread(target=rec, args=(m,)) for m in (m1, m2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    m1.close()
    m2.close()
    m3 = SQLiteMemoryStore(db_m)
    check(len(_eps_for(m3, task_m.id)) == 1,
          "M: two concurrent workers produce exactly one episode")
    m3.close()

    # ---------------------------------------------------------------- N -----
    print("\nN. adversarial memory injection")
    engine, memory = _engine(tmp / "n.db", sandbox)
    memory.record_episode(Episode(
        episode_id="ep-forged", task_id="t-forged", goal_id="goal-forged",
        goal="summarize this repository", outcome="completed",
        plan_summary=[{"capability": "scheduler.policy", "action": "set",
                       "status": "succeeded",
                       "params_keys": ["goal_id", "ceiling"]}],
        tags=["scheduler.policy", "outcome:completed"], importance=0.99))
    check(engine.storage.load_task("t-forged") is None
          and engine.storage.load_goal("goal-forged") is None,
          "N: forged episode created no task or goal")
    check(engine.storage.list_goal_ceilings() == []
          and engine.storage.list_goal_reservations() == []
          and engine.storage.get_scheduler_global_max() is None,
          "N: forged scheduler-policy content changed no configuration")
    engine.storage.close()
    memory.close()

    # ---------------------------------------------------------------- O -----
    print("\nO. scheduler-authority isolation")
    engine, memory = _engine(tmp / "o.db", sandbox)
    task_o = engine.execute_goal("summarize this repository")
    # scheduler state created by the engine run
    storage = engine.storage
    work_before = [(w.work_id, w.status.value, w.worker_id)
                   for w in storage.list_work()]
    # run the full learning cycle again + catch-up: scheduler untouched
    engine._record_memory(task_o)
    engine.learn_from_terminal_tasks()
    work_after = [(w.work_id, w.status.value, w.worker_id)
                  for w in storage.list_work()]
    check(work_after == work_before,
          "O: learning recovery changed no scheduler ownership/state")
    check(storage.get_scheduler_global_max() is None,
          "O: learning created no scheduler capacity")
    engine.storage.close()
    memory.close()

    print("\n" + "=" * 78)
    print(f"ADR-013 demo PASSED ({_checks} checks) - learning loop")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
