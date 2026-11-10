"""Read-only git repository history inspection (ADR-017).

A genuinely useful second capability: reads git METADATA directly
(.git/logs/HEAD reflog, .git/HEAD, .git/refs/heads/*, .git/packed-refs).
NO shell execution - every byte comes from constrained file reads inside the
sandbox, exactly like the filesystem capability.

Security:
- resource_kind is "filesystem:path" so the SAME resource boundary as
  filesystem operations applies at the policy layer;
- the repo path is resolved against the sandbox root and must stay inside it
  (symlink-safe), enforced by the capability itself;
- EVERY git metadata path (.git, HEAD, logs, refs, packed-refs, individual
  ref files) is validated before reading: symlinked metadata components are
  refused and the final resolved path must stay inside the sandbox root
  (F05: a planted ``.git``/``HEAD``/``logs``/``refs`` symlink must never
  expose bytes from outside the sandbox);
- read-only by construction (no writes, no subprocess).

Actions:
  log      - recent commit history from the reflog (.git/logs/HEAD)
  branches - local branch refs (.git/refs/heads + .git/packed-refs)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arion.capabilities.registry import ActionSpec, CapabilityError

_MAX_REF_LINES = 500


class GitLogCapability:
    """Read-only, sandboxed git repository history inspection."""

    name = "git.log"
    description = "Read-only git repository history inspection (parses .git metadata; no shell)."
    actions = [
        ActionSpec(
            name="log",
            description="Recent commit history from the reflog (.git/logs/HEAD).",
            required_scope="git:read",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="filesystem:path",
            resource_param="repo",
            param_schema={
                "repo": {"type": "string", "required": True},
                "limit": {"type": "integer", "required": False},
            },
            default_verification={"policy": "schema_keys", "args": {"keys": ["commits"]}},
        ),
        ActionSpec(
            name="branches",
            description="List local branch refs (.git/refs/heads + packed-refs).",
            required_scope="git:read",
            risk="low",
            side_effects="read_only",
            reversible=True,
            idempotent=True,
            retry_safe=True,
            resource_kind="filesystem:path",
            resource_param="repo",
            param_schema={"repo": {"type": "string", "required": True}},
            default_verification={"policy": "schema_keys", "args": {"keys": ["branches"]}},
        ),
    ]

    def __init__(self, sandbox_root: str | Path):
        self.sandbox_root = Path(sandbox_root).resolve()
        if not self.sandbox_root.is_dir():
            raise CapabilityError(f"sandbox root does not exist: {self.sandbox_root}")

    # ------------------------------------------------------------------ #
    # containment
    # ------------------------------------------------------------------ #

    def _resolve_inside(self, rel_repo: str) -> Path:
        """Resolve a repo path and enforce the sandbox boundary (like the
        filesystem capability: symlink-safe, no escapes)."""
        candidate = (self.sandbox_root / rel_repo).resolve()
        try:
            candidate.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise CapabilityError(f"path escapes sandbox: {rel_repo!r}") from exc
        return candidate

    def _contain_final(self, candidate: Path, label: str) -> Path:
        """Resolve the FINAL metadata path and require it to stay inside the
        sandbox root.  This closes the F05 escape: appending ``.git`` (or
        ``HEAD``/``logs``/``refs``/...) after resolving only ``repo`` left the
        appended components free to be symlinks pointing outside."""
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.sandbox_root)
        except ValueError as exc:
            raise CapabilityError(
                f"git metadata path escapes sandbox: {label!r}"
            ) from exc
        return resolved

    def _metadata_path(self, git: Path, *parts: str) -> Path:
        """Build one git metadata path component by component.

        Every existing component must be a REAL file or directory - a
        symlink anywhere in the chain is refused outright (``.git``, ``HEAD``,
        ``logs``, ``refs`` and friends are git-owned metadata, never links in
        a healthy repository).  The final resolved path is also contained
        inside the sandbox root (defense in depth)."""
        candidate = git
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise CapabilityError(
                    f"git metadata path is a symlink (refused): "
                    f"{'/'.join(parts)!r}"
                )
        return self._contain_final(candidate, "/".join(parts) or ".")

    def _git_dir(self, rel_repo: str) -> Path:
        repo = self._resolve_inside(rel_repo)
        git = repo / ".git"
        if git.is_symlink():
            raise CapabilityError(
                f"git metadata directory is a symlink (refused): {rel_repo!r}"
            )
        if not git.is_dir():
            raise CapabilityError(f"not a git repository: {rel_repo!r}")
        return self._contain_final(git, ".git")

    def _iter_ref_files(self, heads: Path):
        """Yield real (non-symlink) ref files below ``refs/heads``.

        The walk never follows symlinks: a symlinked directory or ref file is
        refused instead of traversed, so enumeration cannot leave the
        contained metadata tree."""
        stack = [heads]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError as exc:
                raise CapabilityError(f"git refs unreadable: {exc}") from exc
            for entry in entries:
                if entry.is_symlink():
                    raise CapabilityError(
                        f"git metadata path is a symlink (refused): "
                        f"{entry.name!r}"
                    )
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    yield entry

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "log":
            return self._log(params)
        if action == "branches":
            return self._branches(params)
        raise CapabilityError(f"unknown action {action!r} for {self.name}")

    def _log(self, params: dict[str, Any]) -> dict[str, Any]:
        rel = params.get("repo")
        if not isinstance(rel, str) or not rel:
            raise CapabilityError("log requires string param 'repo'")
        limit = params.get("limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise CapabilityError("'limit' must be a positive integer")
        git = self._git_dir(rel)
        current_branch = self._current_branch(git)
        reflog = self._metadata_path(git, "logs", "HEAD")
        commits: list[dict[str, Any]] = []
        if reflog.is_file():
            lines = reflog.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-_MAX_REF_LINES:]:
                parts = line.split("\t", 1)
                fields = parts[0].split()
                if len(fields) < 5:
                    continue
                old_sha, new_sha, author, ts, tz = fields[0], fields[1], " ".join(fields[2:-2]), fields[-2], fields[-1]
                message = parts[1].strip() if len(parts) > 1 else ""
                if not message:
                    continue
                commits.append({
                    "sha": new_sha,
                    "parent": old_sha if old_sha != "0" * 40 else None,
                    "author": author,
                    "timestamp": ts,
                    "tz": tz,
                    "message": message[:200],
                })
            commits.reverse()  # newest first
            if limit is not None:
                commits = commits[:limit]
        return {
            "action": "log",
            "capability": self.name,
            "repo": rel,
            "current_branch": current_branch,
            "commits": commits,
        }

    def _branches(self, params: dict[str, Any]) -> dict[str, Any]:
        rel = params.get("repo")
        if not isinstance(rel, str) or not rel:
            raise CapabilityError("branches requires string param 'repo'")
        git = self._git_dir(rel)
        branches: list[dict[str, str]] = []
        heads = self._metadata_path(git, "refs", "heads")
        if heads.is_dir():
            for ref in self._iter_ref_files(heads):
                sha = ref.read_text(encoding="utf-8", errors="replace").strip()
                if sha:
                    branches.append({"name": str(ref.relative_to(heads)), "sha": sha})
        packed = self._metadata_path(git, "packed-refs")
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                    branches.append({"name": parts[1][len("refs/heads/"):], "sha": parts[0]})
        # deterministic: sort by name; dedupe (packed may shadow loose)
        seen: dict[str, str] = {}
        for b in branches:
            seen.setdefault(b["name"], b["sha"])
        branches = [{"name": n, "sha": s} for n, s in sorted(seen.items())]
        return {
            "action": "branches",
            "capability": self.name,
            "repo": rel,
            "branches": branches,
        }

    def _current_branch(self, git: Path) -> str | None:
        head = self._metadata_path(git, "HEAD")
        if not head.is_file():
            return None
        content = head.read_text(encoding="utf-8", errors="replace").strip()
        if content.startswith("ref: refs/heads/"):
            return content[len("ref: refs/heads/"):]
        return None
