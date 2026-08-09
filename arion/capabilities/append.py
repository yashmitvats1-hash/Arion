"""The second write-like capability: filesystem.append (ADR-020).

Appends plain text to a sandboxed, repo-relative file:

- NEVER clobbers: open-for-append only, existing content always preserved;
- refuses to create a missing file unless `create: true` is EXPLICIT
  (creation is a security-relevant parameter, fingerprinted in approvals);
- bounded appended content (hard cap, configurable);
- NO shell, NO subprocess, NO os.system - pure Path I/O;
- strict containment: the resolved path must stay inside the sandbox root
  (symlink escapes and ``../`` traversal are resolved away and rejected);
- deterministic verification contract: reports prior_size / appended_bytes /
  size so the engine can confirm the postcondition without another mutation.

The capability NEVER decides authorization. It enforces its own sandbox
containment only; whether an append may happen at all is decided by the
orchestration authorization layer (scope/boundary/risk/approval), never here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arion.capabilities.registry import ActionSpec, CapabilityError

_MAX_APPEND_BYTES = 1_000_000  # 1 MB content cap (hard default, configurable)


class FilesystemAppendCapability:
    """Append plain text inside a sandbox directory tree (single action)."""

    name = "filesystem.append"
    description = "Sandboxed plain-text append capability (ADR-020)."
    actions = [
        ActionSpec(
            name="append",
            description=(
                "Append text content to a repo-relative file inside the sandbox. "
                "Existing content is never modified; refuses to create a missing "
                "file unless create=true."
            ),
            required_scope="filesystem:write",
            risk="high",
            side_effects="mutating",
            reversible=False,
            idempotent=False,
            retry_safe=False,  # a failed append may have partially applied - never auto-retry
            resource_kind="filesystem:path",
            resource_param="path",
            param_schema={
                "path": {"type": "string", "required": True},
                "content": {"type": "string", "required": True},
                "create": {"type": "boolean", "required": False},
            },
            default_verification={"policy": "append_verified", "args": {}},
            security_relevant_params=["create"],
        ),
    ]

    def __init__(self, sandbox_root: str | Path, max_bytes: int = _MAX_APPEND_BYTES):
        self.sandbox_root = Path(sandbox_root).resolve()
        if not self.sandbox_root.is_dir():
            raise CapabilityError(f"sandbox root does not exist: {self.sandbox_root}")
        if max_bytes <= 0:
            raise CapabilityError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def _resolve_inside(self, rel_path: str) -> Path:
        """Resolve a repo-relative path and enforce the sandbox boundary.

        `.resolve()` follows symlinks, so a link pointing outside the sandbox
        resolves to a path OUTSIDE the root and is rejected - symlink escapes
        fail closed (same pattern as read/write).
        """
        candidate = (self.sandbox_root / rel_path).resolve()
        try:
            candidate.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise CapabilityError(f"path escapes sandbox: {rel_path!r}") from exc
        return candidate

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action != "append":
            raise CapabilityError(f"unknown action {action!r} for {self.name}")
        return self._append(params)

    def _append(self, params: dict[str, Any]) -> dict[str, Any]:
        rel = params.get("path")
        if not isinstance(rel, str) or not rel:
            raise CapabilityError("'path' is required and must be a non-empty string")
        content = params.get("content")
        if not isinstance(content, str):
            raise CapabilityError("'content' is required and must be a string")

        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            raise CapabilityError(
                f"content too large: {len(data)} bytes exceeds the {self.max_bytes} byte cap"
            )
        create = bool(params.get("create", False))

        target = self._resolve_inside(rel)
        if target.is_dir():
            raise CapabilityError(f"not a file: {rel!r}")
        if not target.exists():
            if not create:
                raise CapabilityError(
                    f"target does not exist and create was not requested: {rel!r}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
        prior_size = target.stat().st_size if target.exists() else 0
        try:
            with target.open("ab") as fh:
                fh.write(data)
                fh.flush()
        except OSError as exc:
            raise CapabilityError(f"append failed: {exc}") from exc
        return {
            "appended": True,
            "path": str(rel),
            "canonical_path": str(target),
            "prior_size": prior_size,
            "appended_bytes": len(data),
            "size": prior_size + len(data),
        }
