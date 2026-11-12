"""Directory-relative filesystem boundary for mutating capabilities.

Issue #23 second-pass reviews of F05/F06 identified a TOCTOU gap in the
original containment fix: resolving a path to an absolute string and then
opening it leaves a window in which an adversarial sibling can swap a
directory component for a symlink that redirects the open outside the
repository root.  The fix is to never re-enter the path namespace by name:
walk the parent chain from a held root directory descriptor, one component
at a time, with ``O_NOFOLLOW | O_DIRECTORY`` opens relative to the previous
descriptor, and perform every staging/publish/unlink operation through the
resulting parent descriptor (``dir_fd``).

Security properties (regression-tested in ``tests/test_path_component_substitution.py``):

- Every opened path component is a real directory reached from the root
  descriptor without following a symlink at any point, so a component
  swapped for a symlink is refused (``ELOOP``) instead of followed.  The
  root itself is opened with ``O_NOFOLLOW`` as well.
- ``..`` components are refused outright (opening ``..`` through a held
  descriptor would leave the root).
- Staging files are created with ``O_CREAT | O_EXCL | O_NOFOLLOW``: exclusive
  creation means ANY pre-existing entry at the staging name - including a
  planted symlink - fails closed and is left untouched, and no-follow means
  a planted symlink can never become the write destination.
- Cleanup is identity-gated: a staging name is only unlinked while it still
  resolves to the exact ``(st_dev, st_ino)`` record created here.  A stale
  or swapped name is left alone (this narrows accidental self-clobber; an
  omnipotent same-user adversary can still race any name-based unlink -
  the boundary's hard guarantee is containment and no-overwrite publish,
  not adversarial unlink-proofing).
- Atomic publish: ``os.link`` (never replaces) for ``overwrite=false`` -
  a concurrent creator of the final name wins and the attempt fails with
  ``EEXIST``; ``os.rename`` (atomic replace, never follows symlinks) for
  ``overwrite=true``.  Both run through the held parent descriptor.
- Platforms without ``O_NOFOLLOW``/``O_DIRECTORY``/``dir_fd`` primitives
  fail CLOSED: mutating filesystem capabilities refuse to run at all
  rather than falling back to name-based I/O.

The boundary intentionally does not follow in-tree symlinked directories
either (they are refused as components): following them would reintroduce
name-based indirection.  Leaf symlinks are handled at publish time exactly
as ``os.replace`` semantics always did (the rename replaces the symlink
itself, never its target).

Worktree-style ``.git`` pointer files (see ``git.py``) are outside this
module's scope; that capability refuses them separately.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import PurePosixPath
from typing import Optional, Sequence, Tuple

from arion.capabilities.registry import CapabilityError

# The boundary never follows symlinks.  This module-wide constant documents
# the choice; every operation below passes ``follow_symlinks=False`` or uses
# ``O_NOFOLLOW`` accordingly.
FOLLOW_SYMLINKS = False

_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _probe_secure_primitives() -> bool:
    """True when this platform offers the primitives the boundary needs."""
    needed = (os.open, os.mkdir, os.stat, os.link, os.rename, os.unlink)
    return (
        all(func in os.supports_dir_fd for func in needed)
        and os.stat in os.supports_follow_symlinks
        and getattr(os, "O_NOFOLLOW", 0) != 0
        and getattr(os, "O_DIRECTORY", 0) != 0
    )


SECURE_DIR_FD = _probe_secure_primitives()


def require_secure_primitives() -> None:
    """Fail closed when the platform lacks the directory-relative primitives.

    Mutating filesystem capabilities call this before any I/O and refuse to
    run (CapabilityError) instead of degrading to name-based path traversal.
    """
    if not SECURE_DIR_FD:
        raise CapabilityError(
            "secure directory-relative filesystem operations are unavailable "
            "on this platform (missing O_NOFOLLOW/O_DIRECTORY or dir_fd "
            "support); refusing to mutate"
        )


def split_rooted(rel_parts: Sequence[str]) -> None:
    """Reject components that could leave the root when walked by descriptor.

    ``.`` is normalized away by PurePosixPath already; ``..`` is not, and
    opening ``..`` through a held descriptor would escape the root.
    """
    for part in rel_parts:
        if part in ("", ".", ".."):
            raise CapabilityError(
                f"invalid path component {part!r}; refusing to traverse"
            )


def open_contained_parent(
    root,
    rel_parts: Sequence[str],
    *,
    create: bool,
) -> Tuple[int, Optional[str]]:
    """Open the parent directory of ``rel_parts`` under ``root``, no-follow.

    Returns ``(parent_dir_fd, leaf_name)``; the caller owns the descriptor
    and must close it.  ``leaf_name`` is the final component (never opened
    here).  With ``create=True`` missing intermediate directories are
    created (mode 0o755); every created *and* existing component is then
    re-opened with ``O_NOFOLLOW | O_DIRECTORY`` so a symlinked or
    file-substituted component is refused, never followed.
    """
    require_secure_primitives()
    parts = tuple(rel_parts)
    if not parts:
        raise CapabilityError("empty filesystem target; refusing to act")
    split_rooted(parts)
    try:
        fd = os.open(str(root), _DIR_OPEN_FLAGS)
    except OSError as exc:
        raise CapabilityError(
            f"repository root {root} is not a secure directory: {exc}"
        ) from exc
    try:
        for name in parts[:-1]:
            try:
                next_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise CapabilityError(
                        f"path does not exist under {root}: {name}"
                    )
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass  # a name exists; the no-follow open below decides
                except OSError as exc:
                    raise CapabilityError(
                        f"cannot create directory {name} under {root}: {exc}"
                    ) from exc
                try:
                    next_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise CapabilityError(
                        f"path component {name} under {root} is not a "
                        f"secure directory: {exc}"
                    ) from exc
            except OSError as exc:
                raise CapabilityError(
                    f"path component {name} under {root} is not a "
                    f"secure directory: {exc}"
                ) from exc
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def normalize_rel_parts(rel: str) -> tuple[str, ...]:
    """Lexically normalize a relative target into safe components.

    Collapses harmless in-root ``.``/``a/../b`` segments exactly as
    ``Path.resolve`` normalization used to, then rejects any component that
    could leave the root when walked by descriptor (``..``, empty).
    """
    normalized = posixpath.normpath(rel)
    parts = tuple(PurePosixPath(normalized).parts)
    split_rooted(parts)
    return parts


def open_staging(parent_fd: int, leaf: str, attempts: int = 3) -> Tuple[int, str]:
    """Create an EXCLUSIVE, no-follow staging file beside ``leaf`` under an
    UNPREDICTABLE name.

    The random name closes two vulnerabilities of the fixed
    ``.<leaf>.arion-write-tmp`` name (issue #23 third-pass reviews):

    - *crash-orphan*: a fixed name meant one crash mid-write left an orphan
      that made every later write to that target fail closed forever (DoS
      plus stale-payload debris).  Random names never collide with an old
      orphan, so a later write is never blocked by it; orphans themselves
      are left untouched (never cleaned - see the cleanup identity gate).
    - *staging inode substitution*: the name is unknown until the file
      exists, ``O_EXCL`` refuses any pre-existing entry, and every later
      publish/cleanup re-verifies the entry's identity against the held
      descriptor (see ``identity_matches``).  A pre-existing entry at ANY
      name is never reused, followed or touched - the generator simply
      draws another name and fails closed after ``attempts`` collisions.
    """
    require_secure_primitives()
    last_exc: Optional[Exception] = None
    for _ in range(max(1, attempts)):
        staging = f".{leaf}.{os.urandom(8).hex()}.arion-write-tmp"
        try:
            fd = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            return fd, staging
        except FileExistsError as exc:
            last_exc = exc  # never reuse an existing entry; draw another name
            continue
        except OSError as exc:
            raise CapabilityError(
                f"cannot create staging file for {leaf!r}: {exc}"
            ) from exc
    raise CapabilityError(
        f"temporary write path is occupied after {attempts} attempts "
        f"(refusing to follow or reuse any entry): {last_exc}"
    ) from last_exc


def file_identity(fd: int) -> Tuple[int, int]:
    """Device/inode identity of an open file (for cleanup identity gates).

    The sound way to USE this identity is to keep ``fd`` open until the
    cleanup comparison finishes: an open descriptor pins the inode, so it
    can never be freed and reused by a different file between the check and
    the unlink.
    """
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def name_identity(parent_fd: int, name: str) -> Optional[Tuple[int, int]]:
    """Device/inode identity of ``name`` in ``parent_fd``, without following.

    Returns None when the name does not exist.
    """
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def identity_matches(
    parent_fd: int, name: str, identity: Tuple[int, int],
) -> bool:
    """True while ``name`` still is the exact file that produced ``identity``.

    ``identity`` must come from a descriptor that is STILL OPEN (see
    ``file_identity``) - only then can the recorded inode not have been
    freed and reused by an innocent file in the meantime.
    """
    return name_identity(parent_fd, name) == identity


def unlink_if_identity(
    parent_fd: int, name: str, identity: Tuple[int, int],
) -> bool:
    """Unlink ``name`` only while it still is the exact file we created.

    Identity-gated cleanup: a name that was swapped or reused since creation
    is left alone.  ``identity`` must come from a descriptor that is STILL
    OPEN (see ``file_identity``) - only then can the recorded inode not
    have been freed and reused by an innocent file in the meantime.  A
    residual same-user race remains between this check and the unlink
    itself (no atomic "unlink exactly this inode" exists); the boundary's
    hard guarantee is containment and no-overwrite publish.  Returns True
    when the staging file was removed.
    """
    if not identity_matches(parent_fd, name, identity):
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def write_all(fd: int, data: bytes) -> None:
    """Write the full buffer to ``fd`` (looping over partial writes)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to staging file")
        view = view[written:]


def publish_no_overwrite(
    parent_fd: int, staging: str, leaf: str, identity: Tuple[int, int],
) -> None:
    """Atomically publish ``staging`` as ``leaf`` WITHOUT replacing anything.

    ``os.link`` never replaces an existing name: a concurrent creator of
    ``leaf`` wins and this raises ``FileExistsError``.  Both names are used
    through the held parent descriptor, so neither can be redirected by a
    path-component substitution.  The staging entry's identity is
    re-verified against the caller's held descriptor first: a staging name
    substituted for an innocent file after creation is refused, never
    published (issue #23 third-pass review - staging inode substitution).
    """
    require_secure_primitives()
    if not identity_matches(parent_fd, staging, identity):
        raise CapabilityError(
            f"staging file {staging!r} was substituted after creation "
            f"(refusing to publish it)"
        )
    os.link(
        staging,
        leaf,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
        follow_symlinks=False,
    )


def publish_replace(
    parent_fd: int, staging: str, leaf: str, identity: Tuple[int, int],
) -> None:
    """Atomically publish ``staging`` as ``leaf``, replacing the name.

    ``os.rename`` (same semantics as ``os.replace`` on POSIX) atomically
    replaces the destination NAME and never follows symlinks: a leaf that
    was swapped for a symlink has the symlink itself replaced - its target
    is never opened or written.  The staging entry's identity is
    re-verified against the caller's held descriptor first (see
    ``publish_no_overwrite``).
    """
    require_secure_primitives()
    if not identity_matches(parent_fd, staging, identity):
        raise CapabilityError(
            f"staging file {staging!r} was substituted after creation "
            f"(refusing to publish it)"
        )
    os.rename(staging, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def cleanup_staging(
    parent_fd: int, staging: str, identity: Tuple[int, int],
) -> None:
    """Best-effort identity-gated removal of a staging file after failure."""
    unlink_if_identity(parent_fd, staging, identity)
