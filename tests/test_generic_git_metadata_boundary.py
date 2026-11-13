"""F05 fifth pass: generic filesystem capabilities must not expose Git
metadata (issue #23 work order item D).

``filesystem.read`` (read/list) and ``filesystem.search`` could read
``.git/config``, ``.git/HEAD`` and enumerate Git internals, while the
intended narrow interface is the dedicated ``git.log`` capability.  Now:

- any exact ``.git`` path component - direct or nested, lexical or
  resolved through a symlink alias - is refused for read/list/search;
- ``.git`` entries are filtered from listings and search results;
- ordinary names (``.gitmodules``, ``*.git`` files) stay accessible;
- ``git.log`` keeps its supported metadata access (positive control).

Also pinned explicitly: filesystem.read/search are PATH-BASED
(check-then-use) capabilities and do NOT share the dirfs descriptor
boundary - their residual window is outside the F05 descriptor guarantee
and is documented as such (module docstrings), not silently implied away.
"""

from __future__ import annotations

import os

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError
from arion.capabilities.search import FilesystemSearchCapability


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    (sb / ".git").mkdir()
    (sb / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (sb / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (sb / "sub").mkdir()
    (sb / "sub" / ".git").mkdir()
    (sb / "sub" / ".git" / "secret").write_text("S", encoding="utf-8")
    (sb / ".gitmodules").write_text("[submodule]\n", encoding="utf-8")
    (sb / "notes.git").write_text("ordinary file\n", encoding="utf-8")
    (sb / "docs").mkdir()
    (sb / "docs" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return sb


# --------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    ".git/config", ".git/HEAD", "sub/.git/secret",
])
def test_read_refuses_git_metadata_direct_and_nested(tmp_path, path):
    sb = _sandbox(tmp_path)
    cap = FilesystemReadCapability(sb)
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("read", {"path": path})
    assert "Git metadata" in str(excinfo.value)


def test_read_refuses_symlink_alias_into_git(tmp_path):
    sb = _sandbox(tmp_path)
    os.symlink(sb / ".git" / "config", sb / "alias.txt")
    cap = FilesystemReadCapability(sb)
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("read", {"path": "alias.txt"})
    assert "Git metadata" in str(excinfo.value)


def test_read_allows_ordinary_files_and_gitmodules(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemReadCapability(sb)
    out = cap.execute("read", {"path": ".gitmodules"})
    assert out["content"] == "[submodule]\n"
    out = cap.execute("read", {"path": "notes.git"})
    assert out["content"] == "ordinary file\n"
    out = cap.execute("read", {"path": "docs/a.py"})
    assert out["content"] == "x = 1\n"


# --------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------- #

def test_list_refuses_git_metadata_target(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemReadCapability(sb)
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("list", {"path": ".git"})
    assert "Git metadata" in str(excinfo.value)
    with pytest.raises(CapabilityError):
        cap.execute("list", {"path": "sub/.git"})


def test_list_filters_git_entries(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemReadCapability(sb)
    out = cap.execute("list", {"path": "."})
    names = {e["name"] for e in out["entries"]}
    assert ".git" not in names
    assert {".gitmodules", "notes.git", "docs", "sub"} <= names
    out = cap.execute("list", {"path": "sub"})
    names = {e["name"] for e in out["entries"]}
    assert ".git" not in names


# --------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------- #

def test_search_refuses_git_metadata_target(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemSearchCapability(sb)
    with pytest.raises(ValueError):
        cap.execute("search", {"pattern": "*", "directory": ".git"})
    with pytest.raises(ValueError):
        cap.execute("search", {"pattern": "*", "directory": "sub/.git"})


def test_search_filters_git_results(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemSearchCapability(sb)
    out = cap.execute("search", {"pattern": "*", "directory": "."})
    paths = {r["path"] for r in out["results"]}
    assert not any(".git" in p.split(os.sep) or p == ".git" for p in paths)
    assert not any(".git" in p.split("/") for p in paths)
    # ordinary entries remain searchable
    assert any(p.endswith("a.py") for p in paths)
    assert any("gitmodules" in p for p in paths)


def test_search_symlink_alias_into_git_filtered(tmp_path):
    sb = _sandbox(tmp_path)
    os.symlink(sb / ".git" / "config", sb / "docs" / "alias.txt")
    cap = FilesystemSearchCapability(sb)
    out = cap.execute("search", {"pattern": "alias.txt", "directory": "."})
    assert out["count"] == 0


# --------------------------------------------------------------------- #
# positive control: git.log keeps its narrow metadata access
# --------------------------------------------------------------------- #

def test_gitlog_still_reads_metadata(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / ".git" / "logs").mkdir()
    (sb / ".git" / "logs" / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa A <a@x> 1700000000 +0000\tfirst\n",
        encoding="utf-8",
    )
    (sb / ".git" / "refs").mkdir()
    (sb / ".git" / "refs" / "heads").mkdir()
    (sb / ".git" / "refs" / "heads" / "main").write_text(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8",
    )
    cap = GitLogCapability(sb)
    out = cap.execute("log", {"repo": ".", "limit": 5})
    assert out["action"] == "log"
    branches = cap.execute("branches", {"repo": "."})
    assert any("main" in str(b) for b in branches.get("branches", []))
