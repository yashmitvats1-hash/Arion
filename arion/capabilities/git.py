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
  expose bytes from outside the sandbox).  Reads are DESCRIPTOR-RELATIVE:
  every metadata component is opened ``O_NOFOLLOW|O_DIRECTORY`` through
  held directory descriptors and every file ``O_RDONLY|O_NOFOLLOW`` (the
  ``dirfs`` boundary), so a component or leaf swapped for a symlink after
  validation is refused, never followed;
- worktree indirection is UNSUPPORTED and fails closed: a worktree-style
  ``.git`` regular FILE containing a ``gitdir:`` pointer is not a metadata
  directory, so any request touching it is refused ("not a git repository")
  and the pointer is NEVER dereferenced - whether it points outside the
  sandbox or inside it.  True linked worktrees (``.git/worktrees/<name>``
  metadata directories) remain supported as real directories only;
- read-only by construction (no writes, no subprocess).

Actions:
  log      - recent commit history from the reflog (.git/logs/HEAD)
  branches - local branch refs (.git/refs/heads + .git/packed-refs)
"""

from __future__ import annotations

import errno
import os
import stat as stat_mod
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

from arion.capabilities import dirfs
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
        self._root_identity = dirfs.root_identity(self.sandbox_root)

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

    # ------------------------------------------------------------------ #
    # descriptor-relative metadata access (F05, second/third pass)
    # ------------------------------------------------------------------ #
    #
    # Metadata READS never re-enter the path namespace by name: every
    # component is opened O_NOFOLLOW|O_DIRECTORY from the sandbox root
    # descriptor and every file O_RDONLY|O_NOFOLLOW through its parent
    # descriptor (``dirfs``).  A component - or the leaf - swapped for a
    # symlink after validation is REFUSED, never followed (issue #23
    # reviews).  Missing files/components read as absent (the historical
    # ``is_file()``/``is_dir()`` False semantics); symlinked ones raise the
    # long-standing "git metadata path is a symlink (refused)" error.

    def _validated_repo_parts(self, rel_repo: str) -> tuple[str, ...]:
        """Run the static checks (message contract) and return walk parts."""
        self._git_dir(rel_repo)
        return dirfs.normalize_rel_parts(rel_repo)

    @staticmethod
    def _map_walk_error(exc: CapabilityError, label: str) -> Optional[Any]:
        """Map a dirfs walk failure: absent -> None, symlink -> refused."""
        if isinstance(exc, dirfs.ComponentSymlinkError):
            raise CapabilityError(
                f"git metadata path is a symlink (refused): {label!r}"
            ) from exc
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, FileNotFoundError):
            return None
        if isinstance(cause, OSError) and cause.errno == errno.ENOTDIR:
            return None  # file-substituted component: nothing to read
        if isinstance(cause, OSError) and cause.errno == errno.ELOOP:
            raise CapabilityError(
                f"git metadata path is a symlink (refused): {label!r}"
            ) from exc
        raise exc

    def _open_meta_parent(
        self, repo_parts: tuple[str, ...], parts: tuple[str, ...],
    ) -> Optional[Tuple[int, str]]:
        """Open the parent of one metadata path (no-follow chain).

        Returns ``(parent_fd, leaf_name)`` (caller closes) or None when
        absent.  ``parts`` must be non-empty.
        """
        label = "/".join(parts)
        full = tuple(repo_parts) + (".git",) + tuple(parts)
        try:
            return dirfs.open_contained_parent(
                self.sandbox_root, full, create=False,
                root_identity=self._root_identity,
            )
        except CapabilityError as exc:
            return self._map_walk_error(exc, label)

    def _read_meta(
        self, repo_parts: tuple[str, ...], *parts: str,
    ) -> Optional[str]:
        """Read one metadata file descriptor-relatively; None when absent."""
        label = "/".join(parts)
        got = self._open_meta_parent(repo_parts, tuple(parts))
        if got is None:
            return None
        parent_fd, leaf = got
        try:
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if dirfs.is_symlink_at(parent_fd, leaf) or exc.errno == errno.ELOOP:
                    raise CapabilityError(
                        f"git metadata path is a symlink (refused): {label!r}"
                    ) from exc
                raise CapabilityError(
                    f"git metadata unreadable: {label!r}: {exc}"
                ) from exc
            try:
                if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
                    return None  # directory-substituted leaf: not a ref file
                data = os.read(fd, 262_144)
            finally:
                os.close(fd)
            return data.decode("utf-8", errors="replace")
        finally:
            os.close(parent_fd)

    def _open_meta_dir(
        self, repo_parts: tuple[str, ...], *parts: str,
    ) -> Optional[int]:
        """Open one metadata DIRECTORY descriptor-relatively; None when
        absent.  Caller closes the descriptor."""
        label = "/".join(parts)
        got = self._open_meta_parent(repo_parts, tuple(parts))
        if got is None:
            return None
        parent_fd, leaf = got
        try:
            try:
                return os.open(
                    leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if dirfs.is_symlink_at(parent_fd, leaf) or exc.errno == errno.ELOOP:
                    raise CapabilityError(
                        f"git metadata path is a symlink (refused): {label!r}"
                    ) from exc
                if exc.errno == errno.ENOTDIR:
                    return None  # file-substituted leaf: not a directory
                raise CapabilityError(
                    f"git metadata unreadable: {label!r}: {exc}"
                ) from exc
        finally:
            os.close(parent_fd)

    def _iter_ref_files_fd(
        self, dir_fd: int, prefix: str = "",
    ) -> Iterator[Tuple[str, str]]:
        """Yield ``(rel_name, text)`` for real ref files below ``dir_fd``.

        The walk never follows symlinks: a symlinked directory or ref file
        is refused instead of traversed, so enumeration cannot leave the
        contained metadata tree.  Every entry is examined through the held
        directory descriptor (no name-based re-entry).
        """
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            raise CapabilityError(f"git refs unreadable: {exc}") from exc
        for name in names:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat_mod.S_ISLNK(st.st_mode):
                raise CapabilityError(
                    f"git metadata path is a symlink (refused): {name!r}"
                )
            if stat_mod.S_ISDIR(st.st_mode):
                sub = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
                try:
                    yield from self._iter_ref_files_fd(sub, f"{prefix}{name}/")
                finally:
                    os.close(sub)
            elif stat_mod.S_ISREG(st.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    data = os.read(fd, 262_144)
                finally:
                    os.close(fd)
                yield f"{prefix}{name}", data.decode("utf-8", errors="replace")

    def _git_dir(self, rel_repo: str) -> Path:
        """Locate the repository's real ``.git`` metadata directory.

        Only a real directory named ``.git`` qualifies.  A worktree-style
        ``.git`` regular FILE (``gitdir:`` pointer indirection) is
        UNSUPPORTED: it is not a metadata directory, the request fails
        closed with "not a git repository", and the pointer is never
        dereferenced - inside-pointing and outside-pointing pointers alike
        (regression-tested in ``tests/test_git_worktree_indirection.py``).
        """
        repo = self._resolve_inside(rel_repo)
        git = repo / ".git"
        if git.is_symlink():
            raise CapabilityError(
                f"git metadata directory is a symlink (refused): {rel_repo!r}"
            )
        if not git.is_dir():
            raise CapabilityError(f"not a git repository: {rel_repo!r}")
        return self._contain_final(git, ".git")

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
        repo_parts = self._validated_repo_parts(rel)
        current_branch = self._current_branch(repo_parts)
        reflog = self._read_meta(repo_parts, "logs", "HEAD")
        commits: list[dict[str, Any]] = []
        if reflog is not None:
            lines = reflog.splitlines()
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
        repo_parts = self._validated_repo_parts(rel)
        branches: list[dict[str, str]] = []
        heads_fd = self._open_meta_dir(repo_parts, "refs", "heads")
        if heads_fd is not None:
            try:
                for name, text in self._iter_ref_files_fd(heads_fd):
                    sha = text.strip()
                    if sha:
                        branches.append({"name": name, "sha": sha})
            finally:
                os.close(heads_fd)
        packed = self._read_meta(repo_parts, "packed-refs")
        if packed is not None:
            for line in packed.splitlines():
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

    def _current_branch(self, repo_parts: tuple[str, ...]) -> str | None:
        content = self._read_meta(repo_parts, "HEAD")
        if content is None:
            return None
        content = content.strip()
        if content.startswith("ref: refs/heads/"):
            return content[len("ref: refs/heads/"):]
        return None
