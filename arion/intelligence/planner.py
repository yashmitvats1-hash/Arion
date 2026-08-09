"""Intelligence layer: planning behind a protocol.

The planner turns a goal into an ordered list of PlanSteps. The default
implementation is a small deterministic task-decomposition engine so the whole
spine runs without any LLM (ADR-005, ADR-008). Future model-backed planners
implement the same Planner protocol and are selected via the ModelRouter.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.router import ModelRouter
from arion.state.models import PlanStep, VerificationPolicy


class Planner(Protocol):
    def plan(self, goal_description: str, task_id: str, registry: CapabilityRegistry) -> list[PlanStep]: ...


# Action templates keyed by the capability.plan intent keyword.
_ACTION_TEMPLATES: dict[str, dict[str, Any]] = {
    "read": {"action": "read", "scope": "filesystem:read", "verify": VerificationPolicy("non_empty")},
    "list": {"action": "list", "scope": "filesystem:read", "verify": VerificationPolicy("non_empty")},
}


class DeterministicPlanner:
    """Rule-based planner: map goal intents to capability actions.

    Currently understands repository-inspection goals that decompose into
    list/read steps against the filesystem.read capability. It is deliberately
    tiny - it exists to prove the spine, not to be clever.
    """

    def __init__(self, router: ModelRouter | None = None):
        self._router = router  # reserved: planner can consult the router later

    def plan(self, goal_description: str, task_id: str, registry: CapabilityRegistry) -> list[PlanStep]:
        if not registry.has("filesystem.read"):
            raise ValueError("capability 'filesystem.read' not registered - cannot plan")

        text = goal_description.lower().strip()
        if "summarize" in text or "explore" in text or "inspect" in text:
            # Explore the repository tree, then read the most relevant files.
            steps = [
                PlanStep(
                    index=0,
                    intent="list root",
                    capability="filesystem.read",
                    action="list",
                    scope="filesystem:read",
                    params={"path": "."},
                    verification=VerificationPolicy("non_empty"),
                ),
                PlanStep(
                    index=1,
                    intent="read key files",
                    capability="filesystem.read",
                    action="read",
                    scope="filesystem:read",
                    params=self._key_files(text),
                    verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
                ),
            ]
            return steps

        # Fallback: attempt a direct read of a path mentioned in the goal (e.g. a file name).
        m = re.search(r"(\S+\.\w+)", goal_description)
        if m:
            return [
                PlanStep(
                    index=0,
                    intent="read file",
                    capability="filesystem.read",
                    action="read",
                    scope="filesystem:read",
                    params={"path": m.group(1)},
                    verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
                )
            ]

        raise ValueError(
            f"goal not decomposable by DeterministicPlanner: {goal_description!r} "
            "(extend planner templates or use a model-backed planner)"
        )

    @staticmethod
    def _key_files(text: str) -> dict[str, Any]:
        """Heuristic: which files to read when summarizing a repo."""
        files: list[str] = []
        for candidate in ("README.md", "readme.md", "pyproject.toml", "package.json"):
            if candidate.lower() in text:
                files.append(candidate)
        if not files:
            files = ["README.md"]
        return {"path": files[0]}
