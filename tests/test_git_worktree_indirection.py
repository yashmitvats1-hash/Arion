"""F05 worktree-indirection regressions (issue #23 second-pass review).

The F05 containment fix validates every git metadata component and the
final resolved path.  The review additionally asked for an explicit,
tested position on worktree-style ``.git`` regular FILES containing a
``gitdir:`` pointer.  Arion's position: worktree indirection is UNSUPPORTED
and fails CLOSED - the pointer is never dereferenced, whether it points
outside the sandbox (which would be an escape) or inside it (which would be
an unreviewed metadata redirection).  These tests pin that position for
both the write path (mutations inside ``.git``) and the read path
(history inspection).
"""

from __future__ import annotations

import pytest

from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError
from arion.capabilities.write import FilesystemWriteCapability


def _worktree_repo(tmp_path, gitdir_pointer: str):
    """A repo whose ``.git`` is a worktree-style pointer FILE."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {gitdir_pointer}\n", encoding="utf-8")
    return repo


def test_worktree_gitdir_pointer_outside_never_dereferenced(tmp_path):
    """A .git FILE pointing at an outside directory is refused; the outside
    metadata is never read or written through."""
    outside = tmp_path / "outside-metadata"
    outside.mkdir()
    (outside / "HEAD").write_text("ref: refs/heads/evil\n", encoding="utf-8")
    repo = _worktree_repo(tmp_path, str(outside))

    wcap = FilesystemWriteCapability(repo)
    with pytest.raises(CapabilityError):
        wcap.execute("write", {
            "path": ".git/evil.txt", "content": "x", "overwrite": True,
        })
    assert not (outside / "evil.txt").exists()
    assert (outside / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/evil\n"

    gcap = GitLogCapability(repo)
    with pytest.raises(CapabilityError):
        gcap.execute("log", {"repo": "."})


def test_worktree_gitdir_pointer_inside_also_refused(tmp_path):
    """Even an inside-pointing gitdir: pointer is unsupported: indirection
    is never followed in either direction."""
    repo = _worktree_repo(tmp_path, ".git/worktrees/feature")

    wcap = FilesystemWriteCapability(repo)
    with pytest.raises(CapabilityError):
        wcap.execute("write", {
            "path": ".git/evil.txt", "content": "x", "overwrite": True,
        })
    assert not (repo / "evil.txt").exists()
    # the pointer file is left exactly as found
    assert (repo / ".git").read_text(encoding="utf-8") == (
        "gitdir: .git/worktrees/feature\n"
    )

    gcap = GitLogCapability(repo)
    with pytest.raises(CapabilityError):
        gcap.execute("branches", {"repo": "."})


def test_real_git_dir_still_works(tmp_path):
    """Positive control: a genuine .git directory is unaffected."""
    repo = tmp_path / "repo"
    logs = repo / ".git" / "logs"
    logs.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (logs / "HEAD").write_text(
        "0" * 40 + " " + "a" * 40
        + " Dev <dev@example.invalid> 1700000000 +0000\tcommit: init\n",
        encoding="utf-8",
    )
    gcap = GitLogCapability(repo)
    out = gcap.execute("log", {"repo": "."})
    assert out["commits"]
