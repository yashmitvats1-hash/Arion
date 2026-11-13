"""F05/F06 second-pass adversarial regressions (issue #23 review blockers).

The original containment fix validated the target path and then performed
the I/O on the resolved absolute path.  Independent reviews identified the
TOCTOU gap: between validation and I/O an adversarial sibling can swap a
directory component (or the leaf) for a symlink that redirects the write or
append outside - or into - the sandbox.  These tests pin the
directory-relative boundary (``arion.capabilities.dirfs``):

- ``..`` components can never walk out through a held descriptor;
- an in-tree directory component that is a symlink is refused, never
  followed (name-based indirection is gone entirely);
- leaf symlinks are never FOLLOWED: write replaces the symlink name itself
  (its target is never opened), append refuses outright;
- a concurrent creator of the final name wins against ``overwrite=false``
  (atomic no-replace publish - the clobber race is closed);
- staging cleanup is identity-gated (a swapped name is left alone);
- platforms without ``O_NOFOLLOW``/``dir_fd`` primitives fail CLOSED.
"""

from __future__ import annotations

import os

import pytest

from arion.capabilities import dirfs
from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.registry import CapabilityError
from arion.capabilities.write import FilesystemWriteCapability


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks not available: {exc}")


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    return sb


# --------------------------------------------------------------------- #
# .. components and traversal
# --------------------------------------------------------------------- #

def test_dotdot_that_escapes_root_refused_write(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    for path in ("..", "a/../../outside.txt"):
        with pytest.raises(CapabilityError):
            cap.execute("write", {"path": path, "content": "x"})
    assert not (tmp_path / "outside.txt").exists()


def test_dotdot_that_escapes_root_refused_append(tmp_path):
    sb = _sandbox(tmp_path)
    victim = tmp_path / "outside.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    cap = FilesystemAppendCapability(sb)
    for path in ("..", "a/../../outside.txt"):
        with pytest.raises(CapabilityError):
            cap.execute("append", {"path": path, "content": "x", "create": True})
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


def test_in_root_dotdot_collapses_lexically_like_resolve(tmp_path):
    """`a/../b.txt` keeps its historical meaning (write b.txt) - the
    normalization matches ``Path.resolve`` for in-root paths - while an
    escaping `..` stays refused (tested above)."""
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    out = cap.execute("write", {"path": "a/../b.txt", "content": "x"})
    assert out["written"] is True
    assert (sb / "b.txt").read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------- #
# component substitution: symlinked directories are never followed
# --------------------------------------------------------------------- #

def test_directory_component_symlink_inside_root_refused(tmp_path):
    """An in-tree directory component that is a symlink is refused: the
    boundary never follows name-based indirection (hardening over the old
    resolve-then-open behaviour, which followed in-tree links)."""
    sb = _sandbox(tmp_path)
    real = sb / "real"
    real.mkdir()
    _symlink_or_skip(sb / "evil", real)
    cap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "evil/f.txt", "content": "x", "overwrite": True,
        })
    assert not (real / "f.txt").exists()
    assert (sb / "evil").is_symlink()


def test_directory_component_symlink_outside_root_refused(tmp_path):
    sb = _sandbox(tmp_path)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    _symlink_or_skip(sb / "evil", outside)
    cap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("write", {
            "path": "evil/f.txt", "content": "x", "overwrite": True,
        })
    assert not (outside / "f.txt").exists()


def test_directory_component_symlink_append_refused(tmp_path):
    sb = _sandbox(tmp_path)
    real = sb / "real"
    real.mkdir()
    (real / "f.txt").write_text("INNER", encoding="utf-8")
    _symlink_or_skip(sb / "evil", real)
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError):
        cap.execute("append", {
            "path": "evil/f.txt", "content": "x", "create": True,
        })
    assert (real / "f.txt").read_text(encoding="utf-8") == "INNER"


# --------------------------------------------------------------------- #
# leaf symlinks are never followed
# --------------------------------------------------------------------- #

def test_leaf_symlink_write_replaces_link_not_target(tmp_path):
    """Documented refinement: writing over an in-tree leaf symlink replaces
    the SYMLINK NAME (os.replace semantics), never opening or clobbering
    the file the link pointed at."""
    sb = _sandbox(tmp_path)
    inner = sb / "inner.txt"
    inner.write_text("INNER", encoding="utf-8")
    _symlink_or_skip(sb / "link.txt", inner)
    cap = FilesystemWriteCapability(sb)
    cap.execute("write", {
        "path": "link.txt", "content": "PAYLOAD", "overwrite": True,
    })
    assert not (sb / "link.txt").is_symlink()
    assert (sb / "link.txt").read_text(encoding="utf-8") == "PAYLOAD"
    assert inner.read_text(encoding="utf-8") == "INNER"


def test_leaf_symlink_append_refused_not_followed(tmp_path):
    sb = _sandbox(tmp_path)
    inner = sb / "inner.txt"
    inner.write_text("INNER", encoding="utf-8")
    _symlink_or_skip(sb / "link.txt", inner)
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError):
        cap.execute("append", {"path": "link.txt", "content": "x"})
    assert inner.read_text(encoding="utf-8") == "INNER"


def test_dangling_leaf_symlink_write_replaces_link_name(tmp_path):
    """A dangling leaf symlink is replaced as a NAME; the path it pointed
    at is never created."""
    sb = _sandbox(tmp_path)
    _symlink_or_skip(sb / "link.txt", sb / "missing-target.txt")
    cap = FilesystemWriteCapability(sb)
    cap.execute("write", {
        "path": "link.txt", "content": "PAYLOAD", "overwrite": True,
    })
    assert not (sb / "link.txt").is_symlink()
    assert not (sb / "missing-target.txt").exists()
    assert (sb / "link.txt").read_text(encoding="utf-8") == "PAYLOAD"


# --------------------------------------------------------------------- #
# concurrent target creation vs overwrite=false (atomic no-replace publish)
# --------------------------------------------------------------------- #

def test_concurrent_creator_wins_against_overwrite_false(
    tmp_path, monkeypatch,
):
    """The race the review identified: `exists()` passes, a concurrent
    creator lands the final name before the publish.  The publish must NOT
    clobber the concurrent file - the attempt fails closed."""
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    real_publish = dirfs.publish_no_overwrite

    def racing_publish(parent_fd, staging, leaf, identity, staging_fd=None):
        # Simulate the concurrent creator winning at the last instant.
        fd = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=parent_fd,
        )
        os.write(fd, b"CONCURRENT")
        os.close(fd)
        return real_publish(
            parent_fd, staging, leaf, identity, staging_fd=staging_fd,
        )

    monkeypatch.setattr(dirfs, "publish_no_overwrite", racing_publish)
    with pytest.raises(CapabilityError, match="exists"):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT", "overwrite": False,
        })
    monkeypatch.undo()
    assert (sb / "report.txt").read_text(encoding="utf-8") == "CONCURRENT"
    leftovers = [p for p in sb.iterdir() if p.name.endswith(".arion-write-tmp")]
    assert leftovers == []


# --------------------------------------------------------------------- #
# staging cleanup identity
# --------------------------------------------------------------------- #

def test_unlink_if_identity_unit():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dfd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = os.open(
                "x.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=dfd,
            )
            ident = dirfs.file_identity(fd)
            os.close(fd)
            # identity mismatch -> untouched
            os.rename("x.tmp", "y.tmp", src_dir_fd=dfd, dst_dir_fd=dfd)
            fd2 = os.open(
                "x.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=dfd,
            )
            os.close(fd2)
            assert dirfs.unlink_if_identity(dfd, "x.tmp", ident) is False
            assert dirfs.name_identity(dfd, "x.tmp") is not None
            # identity match -> removed
            moved_ident = dirfs.name_identity(dfd, "y.tmp")
            assert dirfs.unlink_if_identity(dfd, "y.tmp", moved_ident) is True
            assert dirfs.name_identity(dfd, "y.tmp") is None
        finally:
            os.close(dfd)


def test_swapped_staging_name_never_cleaned_up(tmp_path, monkeypatch):
    """If the staging name is swapped after creation (and the publish then
    fails), cleanup must leave the swapped entry alone: only the identity
    created by this write is ever unlinked."""
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    seen: list[str] = []

    def swapping_failing_replace(
        parent_fd, staging, leaf, identity, staging_fd=None,
    ):
        # Attacker swaps the staging name for a victim file, then the
        # publish fails (e.g. I/O error).
        seen.append(staging)
        ident = dirfs.name_identity(parent_fd, staging)
        assert ident is not None
        os.unlink(staging, dir_fd=parent_fd)
        fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
            dir_fd=parent_fd,
        )
        os.write(fd, b"VICTIM_CONTENT")
        os.close(fd)
        raise OSError("publish failed after swap")

    monkeypatch.setattr(dirfs, "publish_replace", swapping_failing_replace)
    with pytest.raises(CapabilityError, match="write failed"):
        cap.execute("write", {
            "path": "report.txt", "content": "PAYLOAD", "overwrite": True,
        })
    monkeypatch.undo()
    staging_path = sb / seen[0]
    assert staging_path.read_text(encoding="utf-8") == "VICTIM_CONTENT"
    assert not (sb / "report.txt").exists()


def test_failed_publish_leaves_no_staging_residue(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)

    def failing_replace(
        parent_fd, staging, leaf, identity, staging_fd=None,
    ):
        raise OSError("disk full")

    monkeypatch.setattr(dirfs, "publish_replace", failing_replace)
    with pytest.raises(CapabilityError, match="write failed"):
        cap.execute("write", {
            "path": "report.txt", "content": "PAYLOAD", "overwrite": True,
        })
    monkeypatch.undo()
    leftovers = [p for p in sb.iterdir() if p.name.endswith(".arion-write-tmp")]
    assert leftovers == []
    assert not (sb / "report.txt").exists()


def test_staging_inode_substitution_refused_at_publish(tmp_path, monkeypatch):
    """Named vulnerability (third-pass review): a victim file substituted
    onto the staging name after creation must NEVER be published as our
    write and NEVER destroyed - the identity re-verification inside the
    publish refuses it, and the cleanup identity gate leaves it alone."""
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    real_replace = dirfs.publish_replace

    def substituting_replace(
        parent_fd, staging, leaf, identity, staging_fd=None,
    ):
        # Attacker swaps an innocent victim file ONTO the staging name.
        os.unlink(staging, dir_fd=parent_fd)
        victim_fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
            dir_fd=parent_fd,
        )
        os.write(victim_fd, b"VICTIM_CONTENT")
        os.close(victim_fd)
        return real_replace(
            parent_fd, staging, leaf, identity, staging_fd=staging_fd,
        )

    monkeypatch.setattr(dirfs, "publish_replace", substituting_replace)
    with pytest.raises(CapabilityError, match="substituted"):
        cap.execute("write", {
            "path": "report.txt", "content": "PAYLOAD", "overwrite": True,
        })
    monkeypatch.undo()
    # The victim survives at the staging name (never cleaned), was never
    # published as the write result, and the target does not exist.
    leftovers = [p for p in sb.iterdir() if p.name.endswith(".arion-write-tmp")]
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "VICTIM_CONTENT"
    assert not (sb / "report.txt").exists()


# --------------------------------------------------------------------- #
# platform fail-closed
# --------------------------------------------------------------------- #

def test_missing_secure_primitives_fail_closed(tmp_path, monkeypatch):
    """Without O_NOFOLLOW/O_DIRECTORY/dir_fd support the mutating
    filesystem capabilities refuse to run at all - no name-based fallback."""
    sb = _sandbox(tmp_path)
    monkeypatch.setattr(dirfs, "SECURE_DIR_FD", False)
    wcap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError, match="unavailable on this platform"):
        wcap.execute("write", {"path": "f.txt", "content": "x"})
    acap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError, match="unavailable on this platform"):
        acap.execute("append", {"path": "f.txt", "content": "x", "create": True})
    assert list(sb.iterdir()) == []


# --------------------------------------------------------------------- #
# positive controls
# --------------------------------------------------------------------- #

def test_nested_write_and_append_roundtrip(tmp_path):
    sb = _sandbox(tmp_path)
    wcap = FilesystemWriteCapability(sb)
    wcap.execute("write", {"path": "a/b/c.txt", "content": "one"})
    acap = FilesystemAppendCapability(sb)
    out = acap.execute("append", {"path": "a/b/c.txt", "content": "two"})
    assert out["prior_size"] == 3 and out["size"] == 6
    assert (sb / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "onetwo"
    leftovers = [
        p for p in (sb / "a" / "b").iterdir()
        if p.name.endswith(".arion-write-tmp")
    ]
    assert leftovers == []
