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
import stat as stat_mod
from pathlib import PurePosixPath
from typing import Optional, Sequence, Tuple

from arion.capabilities.registry import CapabilityError


class ComponentSymlinkError(CapabilityError):
    """A path component or leaf is a symlink; the boundary never follows.

    Distinct from generic walk failures so callers can keep their own
    symlink-refusal message contracts.  Note the kernel reports a symlinked
    component under ``O_DIRECTORY|O_NOFOLLOW`` as ``ENOTDIR``/``ELOOP``
    depending on the link's target, so the boundary distinguishes by
    ``lstat`` at the failure site, not by errno alone.
    """


def is_symlink_at(parent_fd: int, name: str) -> bool:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat_mod.S_ISLNK(st.st_mode)


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

# Supported-platform contract for identity-bound publication (issue #23
# F06 fifth pass): the exact pinned inode can only be published through a
# held descriptor via the /proc/self/fd magic symlink (Linux).  Where that
# primitive is absent the name-based fallback is NOT race-proof against
# staging substitution and is NOT offered as equivalent - the mutating
# capability fails closed instead.  Distinct from fully atomic replace,
# which NO platform provides (renameat2 lacks AT_EMPTY_PATH).
IDENTITY_BOUND_PUBLISH = os.path.isdir("/proc/self/fd")


def require_identity_bound_publish() -> None:
    """Fail closed when identity-bound publication is unavailable.

    Mutating filesystem publication is supported ONLY where the exact
    pinned inode can be published through a held descriptor (Linux
    ``/proc/self/fd``).  Platforms without it are refused BEFORE any
    staging file is created - there is no name-based fallback, because a
    name-based check-then-publish window is not race-proof against a
    staging substitution and must not be presented as equivalent.
    """
    if not IDENTITY_BOUND_PUBLISH:
        raise CapabilityError(
            "identity-bound publication requires /proc/self/fd (Linux) to "
            "publish the exact pinned inode through a held descriptor; "
            "this platform lacks it and the name-based fallback is NOT "
            "race-proof against staging substitution - refusing to mutate"
        )


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
    opening ``..`` through a held descriptor would escape the root.  An
    absolute component is equally fatal: ``os.open`` treats an absolute
    name as absolute even with ``dir_fd`` set, silently IGNORING the
    descriptor anchor (issue #23 F05).
    """
    for part in rel_parts:
        if part in ("", ".", ".."):
            raise CapabilityError(
                f"invalid path component {part!r}; refusing to traverse"
            )
        if part.startswith("/") or os.sep in part or (
            os.altsep is not None and os.altsep in part
        ):
            raise CapabilityError(
                f"invalid path component {part!r}; absolute or "
                "separator-bearing components are refused"
            )


def root_identity(root) -> Tuple[int, int]:
    """Pin the identity of the authorized root directory (issue #23 F05).

    Captured once (capability construction) through a no-follow directory
    open + fstat.  Every later operation re-opens the root BY NAME and
    compares against this pinned identity: a root replaced by another
    directory (rename dance) or redirected through a swapped ancestor
    resolves to a different inode and is refused, never silently re-anchored.
    Documented residual boundary: an attacker who can forge mount
    namespaces / device-inode pairs is outside this threat model; the
    guarantee is against same-user name substitution, which is the model
    the rest of the boundary assumes.  Construction must NOT gate on
    ``require_secure_primitives()`` (capabilities may be constructed on
    any platform; every OPERATION refuses there instead).
    """
    try:
        fd = os.open(str(root), _DIR_OPEN_FLAGS)
    except OSError as exc:
        raise CapabilityError(
            f"sandbox root {root} is not a secure directory: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def has_git_metadata_component(parts: Sequence[str]) -> bool:
    """True when any path component names Git metadata (exact ``.git``).

    Exact-component match only: ordinary names like ``.gitmodules`` or
    ``x.git`` are NOT metadata and must stay accessible.
    """
    return any(part == ".git" for part in parts)


def refuse_git_metadata_access(
    rel_parts: Sequence[str], *, action: str = "access",
) -> None:
    """Refuse generic access to paths addressing Git metadata (F05).

    Any exact ``.git`` component - at any depth, lexical or resolved
    through a symlink alias - targets repository metadata.  General
    filesystem capabilities must not read, list, or search it: the
    dedicated ``git.log`` capability is the narrow read-only metadata
    interface (descriptor-relative, symlink-refusing).
    """
    for part in rel_parts:
        if part == ".git":
            raise CapabilityError(
                f"refusing to {action} Git metadata (path component "
                f"{part!r}); repository metadata is exposed only through "
                "the dedicated git.log capability"
            )


def refuse_git_metadata(rel_parts: Sequence[str]) -> None:
    """Refuse write targets that address Git metadata (issue #23 F05).

    Any path component naming ``.git`` - at any depth - targets repository
    metadata and is refused for general write/append capabilities.  The
    read-only Git capability walks ``.git`` descriptor-relatively by
    design; this guard is only for the mutating general filesystem
    capabilities.
    """
    for part in rel_parts:
        if part == ".git":
            raise CapabilityError(
                f"refusing to write Git metadata (path component {part!r}); "
                "repository metadata is read-only for general filesystem "
                "writes"
            )


def open_contained_parent(
    root,
    rel_parts: Sequence[str],
    *,
    create: bool,
    root_identity: Optional[Tuple[int, int]] = None,
) -> Tuple[int, Optional[str]]:
    """Open the parent directory of ``rel_parts`` under ``root``, no-follow.

    Returns ``(parent_dir_fd, leaf_name)``; the caller owns the descriptor
    and must close it.  ``leaf_name`` is the final component (never opened
    here).  With ``create=True`` missing intermediate directories are
    created (mode 0o755); every created *and* existing component is then
    re-opened with ``O_NOFOLLOW | O_DIRECTORY`` so a symlinked or
    file-substituted component is refused, never followed.

    When ``root_identity`` (pinned at capability construction via
    ``root_identity()``) is given, the root opened here is fstat-verified
    against it (issue #23 F05): a root name replaced between operations -
    or a root path redirected through a swapped ANCESTOR - lands on a
    different inode and is refused before any traversal.  Residual
    boundary: a mount-capable attacker forging (st_dev, st_ino) is out of
    threat model; same-user name substitution is caught.
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
    if root_identity is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != root_identity:
            os.close(fd)
            raise CapabilityError(
                f"sandbox root was replaced (refusing to operate): {root}"
            )
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
                    if is_symlink_at(fd, name):
                        raise ComponentSymlinkError(
                            f"path component {name!r} is a symlink (refused)"
                        ) from exc
                    raise CapabilityError(
                        f"path component {name} under {root} is not a "
                        f"secure directory: {exc}"
                    ) from exc
            except OSError as exc:
                if is_symlink_at(fd, name):
                    raise ComponentSymlinkError(
                        f"path component {name!r} is a symlink (refused)"
                    ) from exc
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
    could leave the root when walked by descriptor (``..``, empty).  An
    ABSOLUTE target is rejected outright (issue #23 F05): it must never be
    silently reinterpreted as relative - ``os.open`` ignores the ``dir_fd``
    anchor for absolute names, so an absolute component would escape the
    root entirely.
    """
    if os.path.isabs(rel) or PurePosixPath(rel).is_absolute():
        raise CapabilityError(
            f"absolute path {rel!r} is refused; targets must be relative "
            "to the root"
        )
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
    """Unlink ``name`` only when it is provably the exact created entry.

    Attacker model (issue #23 fifth pass - cleanup race): processes able
    to mutate entries in the target parent directory ARE INSIDE the
    supported attacker model (consistent with the rest of this boundary).
    POSIX offers NO inode-conditional unlink, so identity-check-then-unlink
    is NOT claimed to survive a same-user substitution.  The enforceable
    design is TAKE + verify + leave-uncertain-untouched:

    1. ``os.rename(name -> fresh unpredictable tomb)`` ATOMICALLY takes
       whatever entry the name holds - there is no check/use gap between
       the name and the captured entry;
    2. the TAKEN entry is verified against the held-descriptor identity
       (``identity`` must come from a STILL-OPEN descriptor, see
       ``file_identity`` - only then can the recorded inode not have been
       freed and reused by an innocent file);
    3. if it is NOT the created entry (substituted/reused), it is restored
       to its original name NO-REPLACE (hard link, never rename): if a
       concurrent creator occupies the name, the entry is preserved under
       the private tomb name instead - uncertain entries are NEVER
       unlinked and neither party's data is ever discarded;
    4. only the created entry is deleted (at the tomb name).

    Documented residual (NOT claimed away): the final unlink in step 4 is
    name-based; a same-user attacker substituting the unpredictable tomb
    name in that exact window can have their entry removed.  The window is
    two syscalls on a name invented after the take; deployments requiring
    a hard guarantee must isolate the directory (separate uid / 0700
    ownership - the enforceable ownership assumption).  Returns True when
    the created entry was removed.
    """
    tomb = f".{name}.{os.urandom(8).hex()}.arion-unlink-tmp"
    try:
        os.rename(name, tomb, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError:
        return False  # absent: nothing to clean
    if name_identity(parent_fd, tomb) != identity:
        # Uncertain entry: restore NO-REPLACE; never unlink it here.
        try:
            os.link(
                tomb, name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass  # concurrent creator owns `name`; entry preserved at tomb
        except OSError:
            pass  # preserved at tomb; never discarded
        else:
            try:
                os.unlink(tomb, dir_fd=parent_fd)
            except OSError:
                pass  # both names hold the entry; harmless
        return False
    try:
        os.unlink(tomb, dir_fd=parent_fd)
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


def _proc_self_fd_path(fd: int) -> Optional[str]:
    """``/proc``-backed path that binds to the EXACT inode behind ``fd``.

    ``linkat`` on this magic symlink (with ``follow_symlinks=True``) hard
    links the descriptor's own inode - a staging-NAME substitution cannot
    redirect it.  Returns None when identity-bound publication is
    unavailable (``IDENTITY_BOUND_PUBLISH``): publication then FAILS
    CLOSED - there is no name-based fallback.  If the pinned inode has been
    unlinked the link also fails closed (ENOENT) - publication yields OUR
    inode or nothing, never the attacker's entry.
    """
    if not IDENTITY_BOUND_PUBLISH:
        return None
    return f"/proc/self/fd/{fd}"


def publish_no_overwrite(
    parent_fd: int,
    staging: str,
    leaf: str,
    identity: Tuple[int, int],
    staging_fd: Optional[int] = None,
) -> None:
    """Atomically publish ``staging`` as ``leaf`` WITHOUT replacing anything.

    ``os.link`` never replaces an existing name: a concurrent creator of
    ``leaf`` wins and this raises ``FileExistsError``.  Both names are used
    through the held parent descriptor, so neither can be redirected by a
    path-component substitution.  The staging entry's identity is
    re-verified against the caller's held descriptor first: a staging name
    substituted for an innocent file after creation is refused, never
    published (issue #23 third-pass review - staging inode substitution).

    With ``staging_fd`` still OPEN, publication is IDENTITY-BOUND (issue
    #23 F06 fourth pass): the link source is the descriptor itself
    (``/proc/self/fd/N`` magic symlink), so a staging-name substitution in
    the verify/publish window can never redirect publication - the result
    is the exact pinned inode at ``leaf`` or a fail-closed error (the
    pinned inode was unlinked), NEVER the substituted entry.  Supported
    platform contract (fifth pass): without an open descriptor or without
    ``/proc`` support publication FAILS CLOSED - there is NO name-based
    fallback, because it is not race-proof and must not be presented as
    equivalent.
    """
    require_secure_primitives()
    if not identity_matches(parent_fd, staging, identity):
        raise CapabilityError(
            f"staging file {staging!r} was substituted after creation "
            f"(refusing to publish it)"
        )
    src = _proc_self_fd_path(staging_fd) if staging_fd is not None else None
    if src is None:
        require_identity_bound_publish()  # raises with the platform contract
        raise CapabilityError(
            "identity-bound publication requires an open staging "
            "descriptor (refusing to publish by name)"
        )
    os.link(src, leaf, follow_symlinks=True, dst_dir_fd=parent_fd)


def publish_replace(
    parent_fd: int,
    staging: str,
    leaf: str,
    identity: Tuple[int, int],
    staging_fd: Optional[int] = None,
) -> None:
    """Publish ``staging`` as ``leaf``, replacing the destination NAME.

    ``os.rename`` semantics (atomic name replacement, never follows
    symlinks: a leaf swapped for a symlink has the symlink itself
    replaced).  The staging entry's identity is re-verified against the
    caller's held descriptor first (see ``publish_no_overwrite``).

    With ``staging_fd`` still OPEN the replacement is IDENTITY-BOUND
    (issue #23 F06 fourth pass): the previous ``leaf`` entry is renamed to
    a random private trash name, the exact pinned inode is hard-linked to
    ``leaf`` through the held descriptor (``/proc/self/fd/N``), and on any
    failure the previous destination is restored NO-REPLACE (hard link,
    never ``rename``): a concurrent creator that landed at ``leaf`` in the
    window is never clobbered (issue #23 fifth pass - rollback collision),
    and if the previous destination cannot be restored without replacing
    such an entry it stays preserved under its private trash name and the
    conflict is reported - neither party's data is ever discarded.  The
    trash name is only unlinked while it still holds the recorded previous
    identity - a substituted or unowned entry is never unlinked.

    DOCUMENTED RESIDUAL (no sweeping claims): there is NO fd-based atomic
    replace primitive (``renameat2`` lacks ``AT_EMPTY_PATH``), so full
    atomicity of REPLACE is unattainable.  Residuals: (1) the brief window
    where ``leaf`` does not exist between the trash rename and the link -
    a concurrent same-user creator winning it is rolled back NO-REPLACE
    and the conflict is reported, never clobbered; (2) supported-platform
    contract: without ``staging_fd``/``/proc`` the operation FAILS CLOSED
    - the name-based fallback is not race-proof and is not offered.
    Publication is identity-bound, not race-proof-replace; callers must not
    claim otherwise.  Crash-orphaned staging/trash files (mode 0600/0644
    at random names) remain as a disk-privacy residual - they are
    deliberately never swept.
    """
    require_secure_primitives()
    if not identity_matches(parent_fd, staging, identity):
        raise CapabilityError(
            f"staging file {staging!r} was substituted after creation "
            f"(refusing to publish it)"
        )
    src = _proc_self_fd_path(staging_fd) if staging_fd is not None else None
    if src is None:
        require_identity_bound_publish()  # raises with the platform contract
        raise CapabilityError(
            "identity-bound publication requires an open staging "
            "descriptor (refusing to publish by name)"
        )
    prev_identity = name_identity(parent_fd, leaf)
    trash: Optional[str] = None
    if prev_identity is not None:
        trash = f".{leaf}.{os.urandom(8).hex()}.arion-replace-tmp"
        os.rename(leaf, trash, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    try:
        os.link(src, leaf, follow_symlinks=True, dst_dir_fd=parent_fd)
    except BaseException as exc:
        if trash is None:
            if isinstance(exc, FileExistsError):
                # A concurrent creator landed `leaf` while the name was
                # free: their entry is never clobbered (issue #23 fifth
                # pass).  Nothing of ours existed to roll back.
                raise CapabilityError(
                    f"publication of {leaf!r} failed: a concurrent entry "
                    f"occupies it (nothing was overwritten)"
                ) from exc
            raise
        # NO-REPLACE rollback (issue #23 fifth pass): a hard link refuses
        # to replace an entry a concurrent creator landed at `leaf` while
        # the destination name was free.  Neither party's data is ever
        # discarded: on conflict the concurrent entry stays and the
        # previous destination is preserved under its private trash name.
        restored = False
        try:
            os.link(
                trash,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            restored = True
        except FileExistsError:
            raise CapabilityError(
                f"publication of {leaf!r} failed ({exc}) and the previous "
                f"destination could not be restored without replacing a "
                f"concurrent entry: the concurrent entry is untouched and "
                f"the previous destination is preserved at {trash!r} "
                f"(recoverable private name); nothing was overwritten"
            ) from exc
        except OSError:
            pass  # previous destination preserved at trash; error below
        if restored:
            try:
                os.unlink(trash, dir_fd=parent_fd)
            except OSError:
                pass  # both names hold the old destination; harmless
        raise
    if trash is not None:
        unlink_if_identity(parent_fd, trash, prev_identity)


def cleanup_staging(
    parent_fd: int, staging: str, identity: Tuple[int, int],
) -> None:
    """Best-effort identity-gated removal of a staging file after failure."""
    unlink_if_identity(parent_fd, staging, identity)
