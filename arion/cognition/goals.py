"""Long-horizon goal management (ADR-015).

Long-horizon goals span multiple planning sessions: a goal can accumulate
PLAN VERSIONS over time (each with its strategy + plan summary), and progress
can be tracked per goal from its tasks.

  Goal -> Long-Horizon Planning (plan versions) -> Strategy Selection ->
  Authorization -> Execution -> Observation -> Verification -> Learning

GoalManager wraps the SQLiteCognitiveStore's goal_plans table plus the
task storage to report per-goal progress. It is deterministic and
informational - goals never authorize anything.
"""

from __future__ import annotations

from typing import Any

from arion.state.models import new_id, utcnow


class GoalManager:
    """Tracks plan versions and progress for long-horizon goals."""

    def __init__(self, store, storage: Any | None = None):
        self.store = store       # SQLiteCognitiveStore (goal_plans table)
        self.storage = storage   # optional SQLiteStorage (tasks per goal)

    def next_plan_version(self, goal_id: str) -> int:
        latest = self.store.latest_goal_plan(goal_id)
        return (latest["plan_version"] + 1) if latest else 1

    def record_plan(self, goal_id: str, strategy: str, plan_summary: list[dict]) -> dict[str, Any]:
        """Record a new plan version for a goal; returns the record."""
        version = self.next_plan_version(goal_id)
        self.store.record_goal_plan(goal_id, version, strategy, plan_summary)
        return {
            "goal_id": goal_id,
            "plan_version": version,
            "strategy": strategy,
            "plan_summary": plan_summary,
            "created_at": utcnow(),
        }

    def plan_history(self, goal_id: str) -> list[dict[str, Any]]:
        return self.store.list_goal_plans(goal_id)

    def latest_plan(self, goal_id: str) -> dict[str, Any] | None:
        return self.store.latest_goal_plan(goal_id)

    def progress(self, goal_id: str) -> dict[str, Any]:
        """Per-goal progress from its tasks (total/completed/failed/pending)."""
        if self.storage is None:
            return {"goal_id": goal_id, "tasks": 0, "completed": 0, "failed": 0, "pending": 0}
        tasks = self.storage.list_tasks()
        goal_tasks = [t for t in tasks if t.goal_id == goal_id]
        return {
            "goal_id": goal_id,
            "tasks": len(goal_tasks),
            "completed": sum(1 for t in goal_tasks if t.status.value == "completed"),
            "failed": sum(1 for t in goal_tasks if t.status.value == "failed"),
            "pending": sum(1 for t in goal_tasks if t.status.value not in ("completed", "failed")),
        }

    def summarize(self, goal_id: str) -> dict[str, Any]:
        latest = self.latest_plan(goal_id)
        return {
            "goal_id": goal_id,
            "plan_versions": len(self.plan_history(goal_id)),
            "latest_strategy": latest["strategy"] if latest else None,
            "latest_plan_version": latest["plan_version"] if latest else None,
            "progress": self.progress(goal_id),
        }
