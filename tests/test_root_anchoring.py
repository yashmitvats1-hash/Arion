"""F05 fourth pass: root anchoring + absolute-path rejection (issue #23).

Two gaps closed here, deterministically:

1. ABSOLUTE targets were lexically split into bare components ("/etc/passwd"
   -> ("/", "etc", "passwd")) and ``os.open`` treats an absolute name as
   absolute EVEN WITH ``dir_fd`` set - the descriptor anchor is silently
   ignored.  ``normalize_rel_parts``/``split_rooted`` now reject absolute
   (and separator-bearing) input outright; it is never silently treated as
   relative.

2. The authorized root was re-opened BY NAME on every operation with no
   identity check: replacing the root directory (rename dance) or
   redirecting the root path through a swapped ANCESTOR between operations
   re-anchored all later I/O onto attacker storage.  The root identity is
   pinned at capability construction and fstat-verified at every root open;
   substitution is refused ("sandbox root was replaced").

Preserved unchanged: git.py's read-only descriptor-relative ``.git`` access,
the worktree ``gitdir:`` indirection rejection, and the general
capabilities' refusal to mutate Git metadata (pinned here too: any ``.git``
path component is refused for write/append, while ``.gitmodules`` and other
ordinary files remain writable - no blanket ban).
"""

from __future__ import annotations

import os

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.dirfs import (
    normalize_rel_parts,
    open_contained_parent,
    refuse_git_metadata,
    root_identity,
    split_rooted,
)
from arion.capabilities.write import FilesystemWriteCapability
from arion.capabilities.registry import CapabilityError


# --------------------------------------------------------------------- #
# 1. absolute input must never be treated as relative
# --------------------------------------------------------------------- #

def test_normalize_rejects_absolute_input():
    for candidate in ("/etc/passwd", "/", "/tmp/evil.txt"):
        with pytest.raises(CapabilityError) as excinfo:
            normalize_rel_parts(candidate)
        assert "absolute path" in str(excinfo.value)


def test_split_rooted_rejects_absolute_components():
    for parts in (("/", "etc"), ("/etc",), ("a", "/b")):
        with pytest.raises(CapabilityError):
            split_rooted(parts)


def test_absolute_input_never_escapes_the_dirfd_anchor(tmp_path):
    """Full boundary call: the absolute target is refused before any open,
    so the dirfd anchor can never be ignored by an absolute name."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(CapabilityError) as excinfo:
        open_contained_parent(
            sandbox, normalize_rel_parts(str(outside / "evil.txt")),
            create=True,
        )
    assert "absolute path" in str(excinfo.value)
    assert list(outside.iterdir()) == []


def test_absolute_input_refused_through_the_capability(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cap = FilesystemWriteCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": str(tmp_path / "evil.txt"), "content": "x",
        })
    assert not (tmp_path / "evil.txt").exists()


# --------------------------------------------------------------------- #
# 2. root identity is pinned; replacement between operations is refused
# --------------------------------------------------------------------- #

def test_root_replaced_between_operations_is_refused(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cap = FilesystemWriteCapability(sandbox)
    cap.execute("write", {"path": "one.txt", "content": "1"})
    assert (sandbox / "one.txt").read_text(encoding="utf-8") == "1"

    # Attacker: move the real root away and put a fresh directory (with a
    # decoy file) at the same name.
    real = tmp_path / "real-root"
    os.rename(sandbox, real)
    sandbox.mkdir()
    (sandbox / "decoy.txt").write_text("attacker", encoding="utf-8")

    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("write", {"path": "two.txt", "content": "2"})
    assert "sandbox root was replaced" in str(excinfo.value)

    # The substituted root was never written; the real root keeps its data.
    assert (sandbox / "two.txt").exists() is False
    assert (sandbox / "decoy.txt").read_text(encoding="utf-8") == "attacker"
    assert (real / "one.txt").read_text(encoding="utf-8") == "1"


def test_ancestor_replaced_with_symlink_is_refused(tmp_path):
    """Swapping an ANCESTOR of the root for a symlink to attacker storage
    between operations must be refused: the root open resolves to the
    attacker's inode, which does not match the pinned identity."""
    base = tmp_path / "a" / "b"
    sandbox = base / "sandbox"
    sandbox.mkdir(parents=True)
    cap = FilesystemWriteCapability(sandbox)
    cap.execute("write", {"path": "one.txt", "content": "1"})

    # Attacker: prepare their own tree, then swap the ancestor "b" for a
    # symlink pointing at it.
    evil_b = tmp_path / "a" / "evil-b"
    (evil_b / "sandbox").mkdir(parents=True)
    (evil_b / "sandbox" / "decoy.txt").write_text("attacker", encoding="utf-8")
    os.rename(base, tmp_path / "a" / "b-real")
    os.symlink(evil_b, base)

    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("write", {"path": "two.txt", "content": "2"})
    # Refused either by the caller-side resolve() containment check or by
    # the root-identity pin - both are deterministic fail-closed refusals.
    assert (
        "sandbox root was replaced" in str(excinfo.value)
        or "path escapes sandbox" in str(excinfo.value)
    )

    assert (evil_b / "sandbox" / "two.txt").exists() is False
    assert (evil_b / "sandbox" / "decoy.txt").read_text(
        encoding="utf-8"
    ) == "attacker"
    real = tmp_path / "a" / "b-real" / "sandbox"
    assert (real / "one.txt").read_text(encoding="utf-8") == "1"


def test_root_replaced_with_symlink_is_refused(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cap = FilesystemWriteCapability(sandbox)
    cap.execute("write", {"path": "one.txt", "content": "1"})

    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "decoy.txt").write_text("attacker", encoding="utf-8")
    real = tmp_path / "real-root"
    os.rename(sandbox, real)
    os.symlink(evil, sandbox)

    with pytest.raises(CapabilityError):
        cap.execute("write", {"path": "two.txt", "content": "2"})

    assert (evil / "two.txt").exists() is False
    assert (evil / "decoy.txt").read_text(encoding="utf-8") == "attacker"


def test_append_capability_pins_root_identity_too(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cap = FilesystemAppendCapability(sandbox)
    cap.execute("append", {
        "path": "log.txt", "content": "1", "create": True,
    })
    real = tmp_path / "real-root"
    os.rename(sandbox, real)
    sandbox.mkdir()
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("append", {
            "path": "log.txt", "content": "2", "create": True,
        })
    assert "sandbox root was replaced" in str(excinfo.value)
    assert (sandbox / "log.txt").exists() is False


# --------------------------------------------------------------------- #
# 3. Git metadata write boundary preserved (git.py reads unchanged)
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    ".git", ".git/config", ".git/objects/ab/cdef", "sub/.git/hooks/pre-commit",
])
def test_git_metadata_write_paths_refused(tmp_path, path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / ".git").mkdir()
    (sandbox / "sub").mkdir()
    write_cap = FilesystemWriteCapability(sandbox)
    append_cap = FilesystemAppendCapability(sandbox)
    with pytest.raises(CapabilityError) as excinfo:
        write_cap.execute("write", {"path": path, "content": "x"})
    assert "Git metadata" in str(excinfo.value)
    with pytest.raises(CapabilityError):
        append_cap.execute("append", {
            "path": path, "content": "x", "create": True,
        })


def test_bare_git_component_refused_even_when_absent(tmp_path):
    """Creating a FILE named .git through write/append must be refused by
    the metadata rule itself, not only by later type checks."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    write_cap = FilesystemWriteCapability(sandbox)
    append_cap = FilesystemAppendCapability(sandbox)
    with pytest.raises(CapabilityError) as excinfo:
        write_cap.execute("write", {"path": ".git", "content": "x"})
    assert "Git metadata" in str(excinfo.value)
    with pytest.raises(CapabilityError) as excinfo:
        append_cap.execute("append", {
            "path": ".git", "content": "x", "create": True,
        })
    assert "Git metadata" in str(excinfo.value)
    assert (sandbox / ".git").exists() is False


def test_ordinary_files_including_gitmodules_remain_writable(tmp_path):
    """No blanket ban: the refusal covers Git metadata components only."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cap = FilesystemWriteCapability(sandbox)
    cap.execute("write", {"path": ".gitmodules", "content": "[submodule]"})
    assert (sandbox / ".gitmodules").read_text(
        encoding="utf-8"
    ) == "[submodule]"
    cap.execute("write", {"path": "docs/readme.md", "content": "hi"})
    assert (sandbox / "docs" / "readme.md").read_text(encoding="utf-8") == "hi"


def test_refuse_git_metadata_helper_is_component_wise():
    refuse_git_metadata(("docs", "readme.md"))  # fine
    with pytest.raises(CapabilityError):
        refuse_git_metadata(("a", ".git", "b"))
    with pytest.raises(CapabilityError):
        refuse_git_metadata((".git",))
