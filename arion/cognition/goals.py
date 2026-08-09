"""Durable goal management and long-horizon execution (ADR-016).

GoalManager is the AUTHORITATIVE state machine for long-lived goals:

  Goal -> Goal State -> Strategy -> Plan -> Execute -> Observe -> Learn
  -> Replan

It owns:
  - goal lifecycle state transitions (validated; invalid transitions FAIL
    CLOSED via GoalStateError), persisted and restart-safe;
  - goal versioning (goal.version increments on every state change);
  - plan versioning (monotonic, immutable previous plans, replay-safe);
  - progress evaluation (via the ProgressEvaluator seam);
  - strategy selection (via StrategySelector, explainable + provenance).

It NEVER infers goal completion from a single successful task: completion
requires a plan whose steps are covered and no blockers/outstanding work.

INFORMATIONAL ONLY: goals, strategies, and progress can influence planning;
only the live authorization layer authorizes execution.
"""

from __future__ import annotations

from typing import Any

from arion.cognition.progress import DeterministicProgressEvaluator, ProgressEvaluator, ProgressResult
from arion.cognition.strategy import StrategySelector
from arion.state.models import (
    GOAL_TRANSITIONS,
    Goal,
    GoalStateError,
    GoalStatus,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)


class GoalManager:
    """Authoritative, persistent goal state machine (ADR-016)."""

    def __init__(
        self,
        storage: Any,                       # SQLiteStorage (goals + tasks authoritative)
        cognitive_store: Any | None = None,  # SQLiteCognitiveStore (goal_plans + beliefs)
        events: Any | None = None,          # EventLogger (audit events)
        strategy_selector: Any | None = None,
        progress_evaluator: ProgressEvaluator | None = None,
        world_monitor: Any | None = None,
    ):
        self.storage = storage
        self.cognitive_store = cognitive_store
        self.events = events
        self.strategy_selector = strategy_selector or StrategySelector()
        self.progress_evaluator = progress_evaluator or DeterministicProgressEvaluator()
        self.world_monitor = world_monitor

    # ------------------------------------------------------------------ #
    # Goal lifecycle
    # ------------------------------------------------------------------ #

    def create_goal(self, description: str, source: str = "cli") -> Goal:
        goal = Goal(id=new_id("goal"), description=description, source=source)
        self.storage.save_goal(goal)
        self._emit("goal.created", goal_id=goal.id, detail={
            "goal_id": goal.id, "description": description[:200], "source": source,
        })
        return goal

    def get_goal(self, goal_id: str) -> Goal | None:
        return self.storage.load_goal(goal_id)

    def list_goals(self, status: str | None = None) -> list[Goal]:
        return self.storage.list_goals(status=status)

    def transition(self, goal_id: str, to_state: str, reason: str, actor: str = "system") -> Goal:
        """Validate + persist a goal state transition (fail closed)."""
        goal = self.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        to_state = to_state.value if isinstance(to_state, GoalStatus) else to_state
        if to_state not in GOAL_TRANSITIONS:
            raise GoalStateError(f"unknown goal state {to_state!r}")
        allowed = GOAL_TRANSITIONS[goal.status.value]
        if to_state not in allowed:
            raise GoalStateError(
                f"invalid goal transition {goal.status.value!r} -> {to_state!r} for goal {goal_id}"
            )
        old_state = goal.status.value
        goal.status = GoalStatus(to_state)
        goal.version += 1
        goal.updated_at = utcnow()
        if to_state == GoalStatus.ACTIVE.value and goal.blockers:
            # resuming/unblocking clears resolved blockers
            goal.blockers = []
        self.storage.save_goal(goal)
        self._emit("goal.state.changed", goal_id=goal_id, detail={
            "goal_id": goal_id,
            "from": old_state,
            "to": to_state,
            "reason": reason[:200],
            "goal_version": goal.version,
            "actor": actor,
        })
        return goal

    def pause(self, goal_id: str, reason: str = "explicit_pause") -> Goal:
        return self.transition(goal_id, GoalStatus.PAUSED.value, reason)

    def resume(self, goal_id: str, reason: str = "explicit_resume") -> Goal:
        return self.transition(goal_id, GoalStatus.ACTIVE.value, reason)

    def cancel(self, goal_id: str, reason: str = "explicit_cancel") -> Goal:
        return self.transition(goal_id, GoalStatus.CANCELLED.value, reason)

    def fail_goal(self, goal_id: str, reason: str = "goal_failed") -> Goal:
        goal = self.transition(goal_id, GoalStatus.FAILED.value, reason)
        goal.last_replan_reason = reason
        goal.updated_at = utcnow()
        self.storage.save_goal(goal)
        return goal

    def complete_goal(self, goal_id: str, reason: str = "all_work_complete") -> Goal:
        return self.transition(goal_id, GoalStatus.COMPLETED.value, reason)

    def set_blocked(self, goal_id: str, blocker: dict[str, Any], reason: str = "blocker") -> Goal:
        """Attach a blocker (idempotent by key) and move to BLOCKED."""
        goal = self.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        key = blocker.get("key") or blocker.get("type") or blocker.get("reason") or "blocker"
        existing = [b for b in (goal.blockers or []) if (b.get("key") or b.get("type")) == key]
        if not existing:
            goal.blockers = list(goal.blockers or []) + [{**blocker, "key": key, "added_at": utcnow()}]
            goal.updated_at = utcnow()
            self.storage.save_goal(goal)
        if goal.status == GoalStatus.ACTIVE:
            goal = self.transition(goal_id, GoalStatus.BLOCKED.value, reason)
        self._emit("goal.blocked", goal_id=goal_id, detail={
            "goal_id": goal_id,
            "blocker_key": key,
            "blocker_type": blocker.get("type", key),
            "reason": reason[:200],
        })
        return self.get_goal(goal_id)

    def clear_blocker(self, goal_id: str, key: str, reason: str = "blocker_resolved") -> Goal:
        """Remove ONE blocker by key; unblocks the goal when none remain."""
        goal = self.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        kept = [b for b in (goal.blockers or []) if (b.get("key") or b.get("type")) != key]
        if len(kept) == len(goal.blockers or []):
            return self.get_goal(goal_id)  # nothing to clear
        goal.blockers = kept
        goal.updated_at = utcnow()
        self.storage.save_goal(goal)
        self._emit("goal.unblocked", goal_id=goal_id, detail={
            "goal_id": goal_id, "blocker_key": key, "reason": reason[:200],
        })
        if goal.status == GoalStatus.BLOCKED and not kept:
            goal = self.transition(goal_id, GoalStatus.ACTIVE.value, reason)
        return self.get_goal(goal_id)

    def clear_blockers(self, goal_id: str, reason: str = "blocker_resolved") -> Goal:
        goal = self.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        if not goal.blockers:
            return self.get_goal(goal_id)
        goal.blockers = []
        goal.updated_at = utcnow()
        self.storage.save_goal(goal)
        self._emit("goal.unblocked", goal_id=goal_id, detail={
            "goal_id": goal_id, "blocker_key": "*", "reason": reason[:200],
        })
        if goal.status == GoalStatus.BLOCKED:
            return self.transition(goal_id, GoalStatus.ACTIVE.value, reason)
        return self.get_goal(goal_id)

    def recheck_blockers(self, goal_id: str) -> bool:
        """Re-evaluate the goal's blockers against the CURRENT world state.

        Drops a `missing_capability` blocker whose required capabilities are
        now registered, and an `approval_pending` blocker whose task is no
        longer awaiting approval. Returns True when blockers were cleared
        (the goal may need re-evaluation/replanning); False otherwise.
        """
        goal = self.get_goal(goal_id)
        if goal is None or goal.status != GoalStatus.BLOCKED or not goal.blockers:
            return False
        world = self.world_monitor.current_state() if self.world_monitor else {}
        reg = world.get("registered_capabilities") or {}
        caps = list(reg.get("value", [])) if isinstance(reg, dict) else []
        dropped_keys: set[str] = set()
        newly_available: set[str] = set()
        for b in list(goal.blockers):
            key = b.get("key") or b.get("type")
            if key == "missing_capability":
                need = list(b.get("capabilities") or [])
                if need and all(c in caps for c in need):
                    dropped_keys.add(key)
                    newly_available.update(c for c in need if c in caps)
            elif key == "approval_pending":
                tid = b.get("task_id")
                task = self.storage.load_task(tid) if tid else None
                if task is None or task.status != TaskStatus.AWAITING_APPROVAL:
                    dropped_keys.add(key)
        if not dropped_keys:
            return False
        goal.blockers = [
            b for b in (goal.blockers or [])
            if (b.get("key") or b.get("type")) not in dropped_keys
        ]
        goal.updated_at = utcnow()
        self.storage.save_goal(goal)
        for cap in sorted(newly_available):
            self._emit("capability.available", goal_id=goal_id, detail={
                "goal_id": goal_id, "capability": cap, "source": "world_state",
            })
        if not goal.blockers:
            self._emit("goal.unblocked", goal_id=goal_id, detail={
                "goal_id": goal_id, "blocker_key": ",".join(sorted(dropped_keys)),
                "reason": "blockers_resolved",
            })
            goal = self.transition(goal_id, GoalStatus.ACTIVE.value, "blockers_resolved")
        return True

    # ------------------------------------------------------------------ #
    # Plan versioning (immutable, monotonic, replay-safe)
    # ------------------------------------------------------------------ #

    def next_plan_version(self, goal_id: str) -> int:
        latest = self.cognitive_store.latest_goal_plan(goal_id) if self.cognitive_store else None
        return (latest["plan_version"] + 1) if latest else 1

    def plan_history(self, goal_id: str) -> list[dict[str, Any]]:
        if self.cognitive_store is None:
            return []
        return self.cognitive_store.list_goal_plans(goal_id)

    def latest_plan(self, goal_id: str) -> dict[str, Any] | None:
        if self.cognitive_store is None:
            return None
        return self.cognitive_store.latest_goal_plan(goal_id)

    def record_plan_version(
        self,
        goal_id: str,
        strategy: str,
        plan_summary: list[dict],
        reason: str,
    ) -> dict[str, Any]:
        """Record a NEW (immutable) plan version for a goal.

        Replay-safe: if the LATEST plan version already matches
        (strategy, plan_summary, reason) AND no task implements it yet, the
        existing version is returned instead of creating a duplicate. A task
        that failed against the latest version triggers a genuinely NEW
        version (same or different steps) - previous plans are never mutated.

        Returns the plan record {goal_id, plan_version, strategy, plan_summary,
        reason, created_at}.
        """
        latest = self.latest_plan(goal_id)
        if latest is not None:
            same = (
                latest.get("strategy") == strategy
                and latest.get("plan_summary") == plan_summary
                and latest.get("reason") == reason
            )
            if same and not self._any_task_for_plan(goal_id, latest["plan_version"]):
                return latest  # replay of the record step: no duplicate version
        version = self.next_plan_version(goal_id)
        self.cognitive_store.record_goal_plan(goal_id, version, strategy, plan_summary, reason)
        record = {
            "goal_id": goal_id,
            "plan_version": version,
            "strategy": strategy,
            "plan_summary": plan_summary,
            "reason": reason,
            "created_at": utcnow(),
        }
        # The goal's CURRENT strategy follows the latest plan version
        # (persisted, restart-safe, still purely informational).
        goal = self.get_goal(goal_id)
        if goal is not None and goal.strategy != strategy:
            goal.strategy = strategy
            goal.updated_at = utcnow()
            self.storage.save_goal(goal)
        self._emit("plan.versioned", goal_id=goal_id, detail={
            "goal_id": goal_id,
            "plan_version": version,
            "strategy": strategy,
            "reason": reason[:200],
            "steps": len(plan_summary),
        })
        return record

    def _any_task_for_plan(self, goal_id: str, plan_version: int) -> bool:
        for task in self.storage.list_tasks():
            if task.goal_id == goal_id and task.plan_version == plan_version:
                return True
        return False

    def task_history(self, goal_id: str) -> list[Task]:
        return [t for t in self.storage.list_tasks() if t.goal_id == goal_id]

    def progress(self, goal_id: str) -> dict[str, Any]:
        """Per-goal task progress counts (completion is NOT inferred here -
        see ProgressEvaluator for the authoritative evaluation)."""
        tasks = self.task_history(goal_id)
        return {
            "goal_id": goal_id,
            "tasks": len(tasks),
            "completed": sum(1 for t in tasks if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
            "pending": sum(1 for t in tasks if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)),
        }

    def pending_task(self, goal_id: str) -> Task | None:
        """The non-terminal task implementing the LATEST plan version, if any.

        Resume it on restart (replay safety) instead of duplicating work. A
        stale pending task for an older plan version is NOT resumed - after a
        replan, a fresh task for the new version is created."""
        latest = self.latest_plan(goal_id)
        latest_version = latest["plan_version"] if latest else None
        for t in self.task_history(goal_id):
            if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                if latest_version is None or t.plan_version == latest_version:
                    return t
        return None

    # ------------------------------------------------------------------ #
    # Progress evaluation
    # ------------------------------------------------------------------ #

    def evaluate(self, goal_id: str) -> tuple[ProgressResult, Goal]:
        """Deterministic progress evaluation; updates goal.last_evaluated_at.

        Emits progress.evaluated + goal.evaluated with bounded metadata.
        """
        goal = self.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        tasks = self.task_history(goal_id)
        latest_plan = self.latest_plan(goal_id)
        world_changes = self._relevant_world_changes(goal)
        world_state = self.world_monitor.current_state() if self.world_monitor else None
        result = self.progress_evaluator.evaluate(goal, tasks, latest_plan, world_changes, world_state)

        goal.progress_metadata = result.to_dict()
        goal.last_evaluated_at = utcnow()
        goal.updated_at = utcnow()
        self.storage.save_goal(goal)

        self._emit("progress.evaluated", goal_id=goal_id, detail=result.to_dict())
        self._emit("goal.evaluated", goal_id=goal_id, detail={
            "goal_id": goal_id,
            "next_action": result.next_action,
            "status": result.status,
            "progress": round(result.progress, 3),
            "evidence_reason": result.evidence.get("reason"),
        })
        return result, self.get_goal(goal_id)

    def _relevant_world_changes(self, goal: Goal) -> list:
        """Deterministic relevance filter: only changes to facts the goal's
        plan depends on (capabilities, or keys mentioned in the plan summary)
        are treated as material. Unrelated changes do NOT trigger replan."""
        if self.world_monitor is None:
            return []
        changes = self.world_monitor.changed_since(goal.last_evaluated_at or goal.created_at)
        if not changes:
            return []
        latest = self.latest_plan(goal.id)
        plan_text = ""
        if latest is not None:
            try:
                plan_text = str(latest.get("plan_summary", [])).lower()
            except Exception:
                plan_text = ""
        relevant = []
        for change in changes:
            key = change.key
            if key == "registered_capabilities":
                relevant.append(change)
            elif key.lower() in plan_text or key in goal.description.lower():
                relevant.append(change)
        return relevant

    def strategy_for(self, goal_id: str, goal_description: str, beliefs: list,
                     environment: dict, guidance: list) -> Any:
        """Select (and persist) the goal's current strategy with provenance."""
        previous = [p.get("strategy", "") for p in self.plan_history(goal_id)]
        strategy = self.strategy_selector.select(
            goal_description, beliefs, environment, guidance,
            previous_strategies=[s for s in previous if s],
        )
        goal = self.get_goal(goal_id)
        if goal is not None and goal.strategy != strategy.name:
            goal.strategy = strategy.name
            goal.updated_at = utcnow()
            self.storage.save_goal(goal)
        return strategy

    def summarize(self, goal_id: str) -> dict[str, Any]:
        goal = self.get_goal(goal_id)
        if goal is None:
            return {"goal_id": goal_id, "exists": False}
        latest = self.latest_plan(goal_id)
        return {
            "goal_id": goal_id,
            "exists": True,
            "description": goal.description[:300],
            "status": goal.status_value,
            "goal_version": goal.version,
            "strategy": goal.strategy,
            "blockers": goal.blockers,
            "plan_versions": len(self.plan_history(goal_id)),
            "latest_plan_version": latest["plan_version"] if latest else None,
            "latest_strategy": latest["strategy"] if latest else None,
            "latest_reason": latest["reason"] if latest else None,
            "progress": goal.progress_metadata,
            "tasks": len(self.task_history(goal_id)),
            "created_at": goal.created_at,
            "updated_at": goal.updated_at,
        }

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def _emit(self, kind: str, goal_id: str | None, detail: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            from arion.observability.events import AuditEvent

            self.events.emit(AuditEvent(kind=kind, task_id=None, success=True, detail=detail))
        except Exception:
            pass
