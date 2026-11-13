"""F05 fifth pass: Git metadata read robustness (issue #23 work order E).

Three concrete review targets are pinned here:

1. FIFO substitution could BLOCK the engine at ``os.open`` before any
   regular-file check (``_read_meta`` with blocking ``O_RDONLY``, and
   append with blocking ``O_WRONLY``).  Fixed with ``O_NONBLOCK`` opens +
   descriptor type checks.  Tests run the call in a BOUNDED SUBPROCESS so
   a regression hangs the child (timeout) instead of the suite.
2. A single 256-KiB ``os.read`` silently truncated larger metadata
   (reflog: newest history lost; packed-refs: entries silently omitted).
   Fixed with a bounded full read: oversized reflogs yield the
   newest-history tail; oversized non-reflog metadata is REJECTED with a
   clear error - data is never returned silently truncated.
3. Recursive ``yield from`` ref traversal could hit the Python recursion
   limit on deeply nested ref directories.  Fixed with an iterative walk
   plus a documented depth limit that fails cleanly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from arion.capabilities import git as git_mod
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    git = sb / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "logs").mkdir()
    (git / "logs" / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa A <a@x> 1700000000 +0000\tfirst\n",
        encoding="utf-8",
    )
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8",
    )
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/heads/packed\n",
        encoding="utf-8",
    )
    return sb


def _run_bounded(body: str, timeout: int = 15) -> str:
    """Run ``body`` in a subprocess with a hard timeout; return stdout.

    A blocking-open regression hangs the CHILD (TimeoutExpired fails the
    test cleanly) instead of hanging the test suite.
    """
    proc = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    return proc.stdout + proc.stderr


# --------------------------------------------------------------------- #
# 1. FIFO substitution can never block
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("meta", ["HEAD", "logs/HEAD", "packed-refs"])
def test_fifo_git_metadata_never_blocks(tmp_path, meta):
    sb = _sandbox(tmp_path)
    target = sb / ".git" / meta
    target.unlink()
    os.mkfifo(target)
    out = _run_bounded(f"""
import os, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
from arion.capabilities.git import GitLogCapability
sb = Path({str(sb)!r})
cap = GitLogCapability(sb)
try:
    result = cap.execute("log", {{"repo": ".", "limit": 5}})
    result2 = cap.execute("branches", {{"repo": "."}})
    print("DONE:" + str(result.get("count", len(result.get("commits", [])))))
except Exception as exc:
    print("DONE-EXC:" + type(exc).__name__)
""")
    assert "DONE" in out, out
    assert "TimeoutExpired" not in out


def test_append_to_fifo_never_blocks(tmp_path):
    sb = _sandbox(tmp_path)
    fifo = sb / "pipe.bin"
    os.mkfifo(fifo)
    out = _run_bounded(f"""
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
from arion.capabilities.append import FilesystemAppendCapability
sb = Path({str(sb)!r})
cap = FilesystemAppendCapability(sb)
try:
    cap.execute("append", {{"path": "pipe.bin", "content": "x", "create": True}})
    print("DONE:appended")
except Exception as exc:
    print("DONE-EXC:" + type(exc).__name__)
""")
    assert "DONE" in out, out
    assert "DONE:appended" not in out  # a FIFO is never appended through


def test_append_fifo_toctou_window_refused_not_blocking(tmp_path):
    """The path check passed on a regular file, then the leaf was swapped
    for a FIFO before the open (TOCTOU window): the non-blocking open +
    descriptor type check must refuse promptly, never block or append."""
    sb = _sandbox(tmp_path)
    (sb / "target.txt").write_text("BASE", encoding="utf-8")
    out = _run_bounded(f"""
import os, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
from arion.capabilities.append import FilesystemAppendCapability
sb = Path({str(sb)!r})
# Simulate the TOCTOU window: the is_file() check passes, but the leaf is
# a FIFO at open time.
Path.is_file = lambda self: True
os.unlink(sb / "target.txt")
os.mkfifo(sb / "target.txt")
cap = FilesystemAppendCapability(sb)
try:
    cap.execute("append", {{"path": "target.txt", "content": "x", "create": False}})
    print("DONE:appended")
except Exception as exc:
    print("DONE-EXC:" + type(exc).__name__)
""")
    assert "DONE-EXC" in out, out
    assert "DONE:appended" not in out


# --------------------------------------------------------------------- #
# 2. never silently truncated
# --------------------------------------------------------------------- #

def test_oversized_reflog_returns_newest_tail(tmp_path, monkeypatch):
    """A reflog larger than the bound must keep the NEWEST history (tail),
    never silently drop it the way a head-read would."""
    sb = _sandbox(tmp_path)
    monkeypatch.setattr(git_mod, "_MAX_META_BYTES", 4000)
    monkeypatch.setattr(git_mod, "_REFLOG_TAIL_BYTES", 600)
    lines = [
        f"{'a' * 40} {'b' * 40} A <a@x> 1700000{i:04d} +0000\tcommit-{i:04d}\n"
        for i in range(200)
    ]
    (sb / ".git" / "logs" / "HEAD").write_text("".join(lines), encoding="utf-8")

    cap = GitLogCapability(sb)
    result = cap.execute("log", {"repo": ".", "limit": 10})
    text = repr(result)
    assert "commit-0199" in text  # newest line survived
    assert "commit-0000" not in text  # oldest was dropped (tail read)


def test_oversized_packed_refs_rejected_not_truncated(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path)
    monkeypatch.setattr(git_mod, "_MAX_META_BYTES", 500)
    lines = "".join(
        f"{'c' * 40} refs/heads/bulk-{i}\n" for i in range(100)
    )
    (sb / ".git" / "packed-refs").write_text(lines, encoding="utf-8")

    cap = GitLogCapability(sb)
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("branches", {"repo": "."})
    assert "too large" in str(excinfo.value)
    assert "refusing to return truncated data" in str(excinfo.value)


def test_within_bound_metadata_read_is_complete(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path)
    monkeypatch.setattr(git_mod, "_MAX_META_BYTES", 100_000)
    lines = "".join(
        f"{'d' * 40} refs/heads/full-{i}\n" for i in range(300)
    )
    (sb / ".git" / "packed-refs").write_text(lines, encoding="utf-8")
    cap = GitLogCapability(sb)
    result = cap.execute("branches", {"repo": "."})
    text = repr(result)
    assert "full-0'" in text and "full-299" in text  # nothing dropped


# --------------------------------------------------------------------- #
# 3. iterative walk + documented depth limit
# --------------------------------------------------------------------- #

def test_deep_refs_beyond_depth_limit_fail_cleanly(tmp_path):
    sb = _sandbox(tmp_path)
    deep = sb / ".git" / "refs" / "heads"
    for i in range(25):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "leaf").write_text("e" * 40 + "\n", encoding="utf-8")

    cap = GitLogCapability(sb)
    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("branches", {"repo": "."})
    assert "deeper than" in str(excinfo.value)


def test_thousand_deep_refs_no_recursion_limit(tmp_path, monkeypatch):
    """Iterative walk: 1000 nested ref directories traverse fine once the
    documented depth limit allows them - the old recursive ``yield from``
    hit the Python recursion limit (~500 levels)."""
    sb = _sandbox(tmp_path)
    monkeypatch.setattr(git_mod, "_MAX_REF_DEPTH", 2000)
    deep = sb / ".git" / "refs" / "heads"
    deep.mkdir(parents=True, exist_ok=True)
    for i in range(1000):
        deep = deep / "a"
        deep.mkdir()
    (deep / "leaf").write_text("f" * 40 + "\n", encoding="utf-8")

    cap = GitLogCapability(sb)
    result = cap.execute("branches", {"repo": "."})
    assert "leaf" in repr(result)
