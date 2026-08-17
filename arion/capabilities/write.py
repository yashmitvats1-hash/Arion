"""The single mutating filesystem capability (ADR-019).

filesystem.write is the ONLY write path in Arion:

- writes plain text to a sandboxed, repo-relative path;
- never overwrites an existing file unless `overwrite: true` is EXPLICIT
  (overwrite is a security-relevant parameter, fingerprinted in approvals);
- bounded input size (hard cap, configurable);
- NO shell, NO subprocess, NO os.system - pure Path I/O;
- strict containment: the resolved path must stay inside the sandbox root
  (symlink escapes and ``../`` traversal are resolved away and rejected);
- deterministic verification contract: returns the exact byte size so the
  engine can confirm the postcondition without another mutation.

The capability NEVER decides authorization. It enforces its own sandbox
containment only; whether a write may happen at all is decided by the
orchestration authorization layer (scope/boundary/risk/approval), never here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arion.capabilities.registry import ActionSpec, CapabilityError

_MAX_WRITE_BYTES = 1_000_000  # 1 MB content cap (hard default, configurable)


class FilesystemWriteCapability:
    """Write plain text inside a sandbox directory tree (single action)."""

    name = "filesystem.write"
    description = "Sandboxed text-file write capability (ADR-019)."
    actions = [
        ActionSpec(
            name="write",
            description=(
                "Write text content to a repo-relative path inside the sandbox. "
                "Refuses to overwrite an existing file unless overwrite=true."
            ),
            required_scope="filesystem:write",
            risk="high",
            side_effects="mutating",
            reversible=False,
            idempotent=False,
            retry_safe=False,  # a failed write may have partially applied - never auto-retry
            resource_kind="filesystem:path",
            resource_param="path",
            param_schema={
                "path": {"type": "string", "required": True},
                "content": {"type": "string", "required": True},
                "overwrite": {"type": "boolean", "required": False},
            },
            default_verification={"policy": "write_verified", "args": {}},
            security_relevant_params=["overwrite"],
        ),
    ]

    def __init__(self, sandbox_root: str | Path, max_bytes: int = _MAX_WRITE_BYTES):
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
        fail closed (same pattern as the read capability).
        """
        candidate = (self.sandbox_root / rel_path).resolve()
        try:
            candidate.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise CapabilityError(f"path escapes sandbox: {rel_path!r}") from exc
        return candidate

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action != "write":
            raise CapabilityError(f"unknown action {action!r} for {self.name}")
        return self._write(params)

    def _write(self, params: dict[str, Any]) -> dict[str, Any]:
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
        overwrite = bool(params.get("overwrite", False))

        target = self._resolve_inside(rel)
        if target.exists() and not overwrite:
            raise CapabilityError(
                f"target already exists and overwrite was not requested: {rel!r}"
            )
        # Atomic-ish bounded write: parents created on demand, file replaced
        # only after the write succeeds (never a partial overwrite of a
        # pre-existing file via truncate-then-fail paths).
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.arion-write-tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return {
            "written": True,
            "path": str(rel),
            "canonical_path": str(target),
            "size": len(data),
        }
