"""Capability layer public surface."""

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import (
    ActionSpec,
    Capability,
    CapabilityError,
    CapabilityRegistry,
    Permission,
)

__all__ = [
    "ActionSpec",
    "Capability",
    "CapabilityError",
    "CapabilityRegistry",
    "FilesystemReadCapability",
    "Permission",
]
