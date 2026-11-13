"""The single mutating filesystem capability (ADR-019).

filesystem.write is the ONLY write path in Arion:

- writes plain text to a sandboxed, repo-relative path;
- never overwrites an existing file unless `overwrite: true` is EXPLICIT
  (overwrite is a security-relevant parameter, fingerprinted in approvals);
- bounded input size (hard cap, configurable);
- NO shell, NO subprocess, NO os.system - pure Path I/O;
- strict containment: the resolved path must stay inside the sandbox root
  (symlink escapes and ``../`` traversal are resolved away and rejected);
- secure temporary-file handling (F06): the staging file is created
  EXCLUSIVELY with no-follow semantics (``O_CREAT|O_EXCL|O_NOFOLLOW``), so a
  planted ``.<name>.arion-write-tmp`` symlink - or any pre-existing entry at
  that name - can never redirect the write; occupied temp paths fail closed
  and are left untouched;
- directory-relative containment (F05/F06 second pass): every filesystem
  operation runs through held directory descriptors (``dirfs``) -
  parent components are opened ``O_NOFOLLOW|O_DIRECTORY`` from the root, so
  a component swapped for a symlink after validation is refused, never
  followed; publish is atomic (``os.link`` - never replaces - for
  ``overwrite=false``; ``os.rename`` - atomic name replace that never
  follows symlinks - for ``overwrite=true``), and staging cleanup is
  identity-gated;
- deterministic verification contract: returns the exact byte size so the
  engine can confirm the postcondition without another mutation.

The capability NEVER decides authorization. It enforces its own sandbox
containment only; whether a write may happen at all is decided by the
orchestration authorization layer (scope/boundary/risk/approval), never here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from arion.capabilities import dirfs
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
        self._root_identity = dirfs.root_identity(self.sandbox_root)
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

        # Symlink-safe containment check (unchanged messages are part of the
        # capability contract: "path escapes sandbox").  `.resolve()` follows
        # links, so an outside-pointing link fails closed here exactly as it
        # always has.  The I/O below never re-enters the path namespace by
        # name - it runs through held directory descriptors (``dirfs``).
        target = self._resolve_inside(rel)
        rel_parts = dirfs.normalize_rel_parts(rel)
        dirfs.refuse_git_metadata(rel_parts)
        dirfs.require_secure_primitives()
        # Platform boundary (issue #23 F06 fifth pass): refuse BEFORE any
        # staging file exists when identity-bound publication is
        # unavailable - no name-based fallback is offered.
        dirfs.require_identity_bound_publish()

        parent_fd, leaf = dirfs.open_contained_parent(
            self.sandbox_root, rel_parts, create=True,
            root_identity=self._root_identity,
        )
        try:
            # Fast path preserving the long-standing error contract.  The
            # atomic no-replace publish below additionally closes the race
            # where a concurrent creator wins BETWEEN this check and the
            # publish (overwrite=false can never clobber it).
            if not overwrite and dirfs.name_identity(parent_fd, leaf) is not None:
                raise CapabilityError(
                    f"target already exists and overwrite was not requested: "
                    f"{rel!r}"
                )
            # F06 (hardened): EXCLUSIVE no-follow creation under an
            # UNPREDICTABLE name - a crash-orphaned staging file can never
            # block a later write, and no pre-existing entry is ever reused,
            # followed or touched (entries at any name are left as found).
            # Cleanup is identity-gated and publish re-verifies the staging
            # identity against the held descriptor: only the exact file
            # created here is ever published or unlinked.
            fd, staging = dirfs.open_staging(parent_fd, leaf)
            # The staging descriptor is held until AFTER the identity-gated
            # cleanup: an open descriptor pins the inode, so a name swapped
            # for an innocent file can never alias it (see
            # dirfs.file_identity).  Only the exact file created here is
            # ever unlinked.
            identity = dirfs.file_identity(fd)
            try:
                dirfs.write_all(fd, data)
                try:
                    if overwrite:
                        # Identity-bound replace (held staging descriptor):
                        # the pinned inode is published via /proc/self/fd;
                        # a staging-name substitution can never redirect it.
                        dirfs.publish_replace(
                            parent_fd, staging, leaf, identity,
                            staging_fd=fd,
                        )
                    else:
                        # Identity-bound no-replace publish (hard link via
                        # the held descriptor): a concurrent creator of
                        # `leaf` wins and is never clobbered.
                        try:
                            dirfs.publish_no_overwrite(
                                parent_fd, staging, leaf, identity,
                                staging_fd=fd,
                            )
                        except FileExistsError as exc:
                            raise CapabilityError(
                                f"target already exists and overwrite was not "
                                f"requested: {rel!r}"
                            ) from exc
                finally:
                    dirfs.cleanup_staging(parent_fd, staging, identity)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
        except CapabilityError:
            raise
        except OSError as exc:
            raise CapabilityError(f"write failed: {exc}") from exc
        finally:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        return {
            "written": True,
            "path": str(rel),
            "canonical_path": str(target),
            "size": len(data),
        }
