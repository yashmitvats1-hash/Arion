"""Capability layer: capabilities, permissions and the registry.

A capability is a capability only if it is safe to call from the agent loop.
Every capability:

- declares the permission scope(s) it needs (e.g. "filesystem:read");
- is self-describing (name, description, actions) for discovery;
- returns structured observations;
- raises CapabilityError on failure.

No capability may perform privileged action implicitly - permission checking
lives in the orchestration layer (ADR-006).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class CapabilityError(Exception):
    """Raised when a capability fails to execute its action."""


@dataclass
class ActionSpec:
    name: str
    description: str
    required_scope: str
    params: dict[str, Any] = field(default_factory=dict)


class Capability(Protocol):
    """Contract every capability implements."""

    name: str
    description: str
    actions: list[ActionSpec]

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute an action with the given params, returning a structured observation."""
        ...


class Permission:
    """A granted permission: capability scope + optional parameter constraints."""

    def __init__(self, scope: str, constraints: dict[str, Any] | None = None):
        self.scope = scope
        self.constraints = constraints or {}

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "constraints": self.constraints}


class CapabilityRegistry:
    """Discovers capabilities by name and provides introspection for planning."""

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        self._caps[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def has(self, name: str) -> bool:
        return name in self._caps

    def list(self) -> list[str]:
        return sorted(self._caps)

    def action_spec(self, capability: str, action: str) -> ActionSpec | None:
        cap = self._caps.get(capability)
        if cap is None:
            return None
        for a in cap.actions:
            if a.name == action:
                return a
        return None

    def capabilities_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "name": cap.name,
                "description": cap.description,
                "actions": [
                    {"name": a.name, "description": a.description, "required_scope": a.required_scope}
                    for a in cap.actions
                ],
            }
            for cap in sorted(self._caps.values(), key=lambda c: c.name)
        ]
