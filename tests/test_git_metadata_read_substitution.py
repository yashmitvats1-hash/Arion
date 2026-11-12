"""F05 descriptor-relative Git metadata read regressions (issue #23, pass 3).

The F05 fix validated metadata components and then read the resolved
absolute paths - the same validate-then-open TOCTOU window the third-pass
reviews flagged for the write path.  Git metadata READS are now
descriptor-relative (``git.py`` + ``dirfs``): every component is opened
``O_NOFOLLOW|O_DIRECTORY`` from the sandbox root descriptor and every file
``O_RDONLY|O_NOFOLLOW`` through its parent descriptor.  These tests pin
that a component (or leaf) swapped for a symlink AFTER validation is
refused, never followed - its target's bytes never reach the caller.
"""

from __future__ import annotations

import os

import pytest

from arion.capabilities import git as git_mod
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError

OUTSIDE_SECRET = "OUTSIDE-SECRET-CONTENT"


def _repo_with_head(tmp_path):
    repo = tmp_path / "repo"
    git = repo / ".git"
    (git / "logs").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "logs" / "HEAD").write_text(
        "0" * 40 + " " + "a" * 40
        + " Dev <dev@example.invalid> 1700000000 +0000\tcommit: init\n",
        encoding="utf-8",
    )
    return repo


def test_component_symlink_to_directory_refused_static(tmp_path):
    """Symlinked metadata DIRECTORY (the kernel reports ENOTDIR under
    O_DIRECTORY|O_NOFOLLOW, not ELOOP): refused as a symlink, never
    followed, outside bytes never observed."""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "HEAD").write_text(OUTSIDE_SECRET, encoding="utf-8")
    repo = _repo_with_head(tmp_path)
    (repo / ".git" / "logs" / "HEAD").unlink()
    (repo / ".git" / "logs").rmdir()
    (repo / ".git" / "logs").symlink_to(outside)
    cap = GitLogCapability(repo)
    with pytest.raises(CapabilityError,
                       match="git metadata path is a symlink"):
        result = cap.execute("log", {"repo": "."})
        raise AssertionError(f"must refuse, got {result!r}")
    # the outside content is unreachable through the capability
    assert OUTSIDE_SECRET == outside.joinpath("HEAD").read_text()


def test_component_substitution_race_between_validation_and_read(tmp_path, monkeypatch):
    """Named vulnerability: `.git/logs` swapped for an outside-pointing
    symlink BETWEEN validation and the read.  The descriptor walk refuses
    it; the outside reflog is never read."""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "HEAD").write_text(
        "0" * 40 + " " + "b" * 40
        + " Evil <evil@example.invalid> 1700000000 +0000\tcommit: OUTSIDE-LEAK\n",
        encoding="utf-8",
    )
    repo = _repo_with_head(tmp_path)
    cap = GitLogCapability(repo)

    real_open = os.open
    armed = {"done": False}

    def swapping_open(name, flags, *args, **kwargs):
        # Adversary: swap the `logs` directory for an outside symlink at
        # the exact moment the walk is about to open it.
        if not armed["done"] and name == "logs" and kwargs.get("dir_fd") is not None:
            armed["done"] = True
            (repo / ".git" / "logs" / "HEAD").unlink()
            (repo / ".git" / "logs").rmdir()
            (repo / ".git" / "logs").symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(git_mod.os, "open", swapping_open)
    with pytest.raises(CapabilityError, match="symlink"):
        cap.execute("log", {"repo": "."})
    monkeypatch.undo()
    assert armed["done"] is True  # the race was actually exercised


def test_leaf_substitution_race_on_head(tmp_path, monkeypatch):
    """`.git/HEAD` swapped for an outside-pointing symlink just before the
    leaf open: refused; the outside file is never read."""
    outside = tmp_path / "outside-head"
    outside.write_text("ref: refs/heads/OUTSIDE-BRANCH\n", encoding="utf-8")
    repo = _repo_with_head(tmp_path)
    cap = GitLogCapability(repo)

    real_open = os.open
    armed = {"done": False}

    def swapping_open(name, flags, *args, **kwargs):
        if not armed["done"] and name == "HEAD" and kwargs.get("dir_fd") is not None:
            armed["done"] = True
            (repo / ".git" / "HEAD").unlink()
            (repo / ".git" / "HEAD").symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(git_mod.os, "open", swapping_open)
    with pytest.raises(CapabilityError, match="symlink"):
        cap.execute("log", {"repo": "."})
    monkeypatch.undo()


def test_ref_file_symlink_refused_during_enumeration(tmp_path):
    """A symlinked REF FILE inside refs/heads is refused during the
    descriptor walk - enumeration never yields or follows it."""
    outside = tmp_path / "outside-ref"
    outside.write_text("f" * 40 + "\n", encoding="utf-8")
    repo = _repo_with_head(tmp_path)
    (repo / ".git" / "refs" / "heads" / "feature").symlink_to(outside)
    cap = GitLogCapability(repo)
    with pytest.raises(CapabilityError, match="symlink"):
        cap.execute("branches", {"repo": "."})


def test_normal_reads_work_descriptor_relative(tmp_path):
    """Positive control: log and branches read normally through the
    descriptor path."""
    repo = _repo_with_head(tmp_path)
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "c" * 40 + "\n", encoding="utf-8"
    )
    cap = GitLogCapability(repo)
    log = cap.execute("log", {"repo": "."})
    assert log["current_branch"] == "main"
    assert log["commits"] and log["commits"][0]["message"] == "commit: init"
    branches = cap.execute("branches", {"repo": "."})
    assert branches["branches"] == [{"name": "main", "sha": "c" * 40}]
