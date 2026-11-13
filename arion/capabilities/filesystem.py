"""Constrained, read-only filesystem capability.

Security boundary (per architecture decision): this capability is read-only
and uses NO shell execution. All paths are resolved against the configured
sandbox root and must stay inside it (symlink-safe). Access is allowed only
for the requested mode.

F05 boundary statement (issue #23 fifth pass): unlike filesystem.write /
filesystem.append / git.log, this capability is PATH-BASED (``Path.resolve``
containment + check-then-use), NOT descriptor-relative under the ``dirfs``
pinned-root/no-follow boundary.  It therefore does not share the dirfs
root-identity and component-substitution guarantees; the residual
same-user check-then-use window on read/list is explicitly OUTSIDE the F05
descriptor guarantee.  What IS enforced here: exact-``.git`` path
components are refused for read/list (lexical and resolved-through-symlink)
and ``.git`` entries are filtered from listings - repository metadata is
exposed only through the dedicated ``git.log`` capability.  Ordinary names
(``.gitmodules``, ``*.git``) stay accessible.

Mutating filesystem actions live in their OWN capabilities behind the same
boundary model (filesystem.write - ADR-019, filesystem.append - ADR-020).
Delete/move/glob/stat are explicitly NOT built yet.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from arion.capabilities.dirfs import has_git_metadata_component, refuse_git_metadata_access
from arion.capabilities.registry import ActionSpec, CapabilityError

_MAX_READ_BYTES = 1_000_000  # 1 MB per file, hard cap


class FilesystemReadCapability:
    """Read-only access to a sandbox directory tree."""

    name = "filesystem.read"
    description = "Read-only, sandboxed filesystem access (read + list) for Arion tasks."
    actions = [
        ActionSpec(
            name="read",
            description="Read a text file from the sandbox (size-capped).",
            required_scope="filesystem:read",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="filesystem:path",
            resource_param="path",
            param_schema={"path": {"type": "string", "required": True}},
            default_verification={"policy": "schema_keys", "args": {"keys": ["content"]}},
        ),
        ActionSpec(
            name="list",
            description="List entries of a directory inside the sandbox.",
            required_scope="filesystem:read",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="filesystem:path",
            resource_param="path",
            param_schema={"path": {"type": "string", "required": True}},
            default_verification={"policy": "non_empty"},
        ),
    ]

    def __init__(self, sandbox_root: str | Path):
        self.sandbox_root = Path(sandbox_root).resolve()
        if not self.sandbox_root.is_dir():
            raise CapabilityError(f"sandbox root does not exist: {self.sandbox_root}")

    def _resolve_inside(self, rel_path: str) -> Path:
        """Resolve a repo-relative path and enforce the sandbox boundary."""
        candidate = (self.sandbox_root / rel_path).resolve()
        try:
            candidate.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise CapabilityError(f"path escapes sandbox: {rel_path!r}") from exc
        return candidate

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "read":
            return self._read(params)
        if action == "list":
            return self._list(params)
        raise CapabilityError(f"unknown action {action!r} for {self.name}")

    def _read(self, params: dict[str, Any]) -> dict[str, Any]:
        rel = params.get("path")
        if not isinstance(rel, str) or not rel:
            raise CapabilityError("read requires string param 'path'")
        # F05: refuse Git metadata by lexical component AND by resolved
        # path (a symlink alias into .git resolves to a .git component).
        refuse_git_metadata_access(PurePosixPath(rel).parts, action="read")
        path = self._resolve_inside(rel)
        refuse_git_metadata_access(
            path.relative_to(self.sandbox_root).parts, action="read",
        )
        if not path.is_file():
            raise CapabilityError(f"not a file: {rel!r}")
        size = path.stat().st_size
        if size > _MAX_READ_BYTES:
            raise CapabilityError(f"file too large to read: {size} bytes > {_MAX_READ_BYTES}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CapabilityError(f"not a text file: {rel!r}") from exc
        return {
            "action": "read",
            "capability": self.name,
            "path": rel,
            "size": size,
            "truncated": False,
            "content": content,
        }

    def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        rel = params.get("path", ".")
        if not isinstance(rel, str):
            raise CapabilityError("list param 'path' must be a string")
        refuse_git_metadata_access(PurePosixPath(rel).parts, action="list")
        path = self._resolve_inside(rel)
        refuse_git_metadata_access(
            path.relative_to(self.sandbox_root).parts, action="list",
        )
        if not path.is_dir():
            raise CapabilityError(f"not a directory: {rel!r}")
        entries = []
        for child in sorted(path.iterdir()):
            try:
                if has_git_metadata_component((child.name,)):
                    continue  # .git metadata is never enumerated here
                entries.append({"name": child.name, "is_dir": child.is_dir()})
            except OSError:
                continue  # unreadable entry: skip, do not fail the whole listing
        return {
            "action": "list",
            "capability": self.name,
            "path": rel,
            "entries": entries,
        }


# Simple path check used by permission policy evaluation
def is_within_sandbox(rel_path: str, sandbox_root: str | Path) -> bool:
    try:
        root = Path(sandbox_root).resolve()
        candidate = (root / rel_path).resolve()
        candidate.relative_to(root)
        return True
    except (ValueError, OSError):
        return False
