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
    def plan(
        self,
        goal_description: str,
        task_id: str,
        registry: CapabilityRegistry,
        context: Any | None = None,  # PlanningContext (memory digest) - informational only
    ) -> list[PlanStep]: ...


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
        self.last_transformation = None  # PlanTransformation | None (audit, ADR-013)

    def plan(
        self,
        goal_description: str,
        task_id: str,
        registry: CapabilityRegistry,
        context: Any | None = None,
    ) -> list[PlanStep]:
        text = goal_description.lower().strip()

        # Git-history goals (ADR-017): the git.log capability is used when it
        # is registered and the goal asks about history/commits/branches.
        if self._is_git_goal(text) and registry.has("git.log"):
            steps = [
                PlanStep(
                    index=0,
                    intent="read git history",
                    capability="git.log",
                    action="log",
                    scope="git:read",
                    params={"repo": ".", "limit": 10},
                    verification=VerificationPolicy("schema_keys", {"keys": ["commits"]}),
                ),
                PlanStep(
                    index=1,
                    intent="list branches",
                    capability="git.log",
                    action="branches",
                    scope="git:read",
                    params={"repo": "."},
                    verification=VerificationPolicy("schema_keys", {"keys": ["branches"]}),
                ),
            ]
        else:
            if not registry.has("filesystem.read"):
                raise ValueError("capability 'filesystem.read' not registered - cannot plan")
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
            else:
                # Fallback: attempt a direct read of a path mentioned in the goal (e.g. a file name).
                m = re.search(r"(\S+\.\w+)", goal_description)
                if not m:
                    raise ValueError(
                        f"goal not decomposable by DeterministicPlanner: {goal_description!r} "
                        "(extend planner templates or use a model-backed planner)"
                    )
                steps = [
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

        # Memory-driven planning: if the context carries structured guidance
        # (from prior experience), re-target steps away from known-failing
        # resources. Informational only - authorization still decides.
        # Non-mutating + auditable: the original plan is retained in
        # self.last_transformation, and each transformed step carries its
        # guidance provenance.
        self.last_transformation = None
        if context is not None and getattr(context, "guidance", None):
            from arion.memory.guidance import apply_guidance_to_steps, registry_resource_param

            transformation = apply_guidance_to_steps(
                steps,
                context.guidance,
                resource_param_resolver=lambda cap, act: registry_resource_param(registry, cap, act),
                action_meta_resolver=lambda cap, act: registry.action_spec(cap, act),
            )
            self.last_transformation = transformation
            steps = transformation.transformed
        return steps

    @staticmethod
    def _is_git_goal(text: str) -> bool:
        """Deterministic heuristic: the goal asks about repository history."""
        return any(k in text for k in ("git", "history", "commit", "branch", "reflog"))

    def required_capabilities(self, goal_description: str) -> set[str]:
        """Which capabilities THIS planner needs for a goal (ADR-017). The
        engine gates on this so a goal whose required capability is missing is
        durably BLOCKED instead of failing/replanning in a loop."""
        text = goal_description.lower().strip()
        if self._is_git_goal(text):
            return {"git.log"}
        return {"filesystem.read"}

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
