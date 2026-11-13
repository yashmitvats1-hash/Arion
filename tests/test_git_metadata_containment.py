"""F05 regression: git metadata path containment (issue #23 / issue #18 F05).

An attacker who can plant symlinks inside the sandbox must not be able to
make ``git.log`` read git metadata from OUTSIDE the sandbox.  Every metadata
component is a reachable escape path and is tested here:

- ``.git`` itself (directory symlink),
- ``.git/HEAD``,
- ``.git/logs`` and ``.git/logs/HEAD``,
- ``.git/refs`` and ``.git/refs/heads``,
- individual ref files under ``refs/heads``,
- ``.git/packed-refs``.

The invariant: a planted metadata symlink is REFUSED (CapabilityError, task
failure at engine level) and outside bytes are never returned.  Symlinked
metadata is rejected even when it points INSIDE the sandbox: git metadata is
real files, never links.
"""

from __future__ import annotations

import json

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
OUTSIDE_SECRET = "OUTSIDE_SECRET_F05"


def _symlink_or_skip(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks not available: {exc}")


def _outside_metadata(root):
    """A complete fake git metadata tree OUTSIDE the sandbox, tagged with a
    secret string that must never leak through git.log."""
    outside = root / "outside-metadata"
    (outside / "logs").mkdir(parents=True)
    (outside / "refs" / "heads").mkdir(parents=True)
    (outside / "HEAD").write_text("ref: refs/heads/private\n", encoding="utf-8")
    (outside / "logs" / "HEAD").write_text(
        "0" * 40 + " " + "1" * 40
        + f" Auditor <audit@example.invalid> 1700000000 +0000\t{OUTSIDE_SECRET}\n",
        encoding="utf-8",
    )
    (outside / "refs" / "heads" / "private").write_text("1" * 40 + "\n", encoding="utf-8")
    (outside / "packed-refs").write_text(
        f"{OUTSIDE_SECRET} refs/heads/leaked\n", encoding="utf-8"
    )
    return outside


def _inside_git(root):
    """A healthy fake git metadata tree inside the sandbox (positive control)."""
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "logs").mkdir()
    (git / "logs" / "HEAD").write_text(
        "0" * 40 + " " + "a" * 40
        + " Alice <a@x.io> 1700000000 +0000\tfirst commit\n",
        encoding="utf-8",
    )
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    return git


def _cap(sandbox):
    return GitLogCapability(sandbox)


def _assert_no_leak(result):
    assert OUTSIDE_SECRET not in json.dumps(result)


# --------------------------------------------------------------------- #
# capability-level adversarial regression (each reachable escape path)
# --------------------------------------------------------------------- #


def test_dotgit_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = _outside_metadata(tmp_path)
    _symlink_or_skip(sandbox / ".git", outside, target_is_directory=True)
    cap = _cap(sandbox)
    for action in ("log", "branches"):
        with pytest.raises(CapabilityError):
            cap.execute(action, {"repo": "."})


def test_dotgit_symlink_inside_sandbox_also_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    real = sandbox / "real-git"
    real.mkdir()
    (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _symlink_or_skip(sandbox / ".git", real, target_is_directory=True)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("log", {"repo": "."})


def test_git_head_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "HEAD").unlink()
    _symlink_or_skip(git / "HEAD", tmp_path / "outside-head")
    (tmp_path / "outside-head").write_text(
        f"ref: refs/heads/{OUTSIDE_SECRET}\n", encoding="utf-8"
    )
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("log", {"repo": "."})


def test_git_logs_dir_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "logs" / "HEAD").unlink()
    (git / "logs").rmdir()
    outside = _outside_metadata(tmp_path)
    _symlink_or_skip(git / "logs", outside / "logs", target_is_directory=True)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("log", {"repo": "."})


def test_git_logs_head_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "logs" / "HEAD").unlink()
    victim = tmp_path / "outside-reflog"
    victim.write_text(
        "0" * 40 + " " + "1" * 40
        + f" X <x@x> 1700000000 +0000\t{OUTSIDE_SECRET}\n",
        encoding="utf-8",
    )
    _symlink_or_skip(git / "logs" / "HEAD", victim)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("log", {"repo": "."})


def test_git_refs_dir_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "refs" / "heads" / "main").unlink()
    (git / "refs" / "heads").rmdir()
    (git / "refs").rmdir()
    outside = _outside_metadata(tmp_path)
    _symlink_or_skip(git / "refs", outside / "refs", target_is_directory=True)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("branches", {"repo": "."})


def test_git_refs_heads_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "refs" / "heads" / "main").unlink()
    (git / "refs" / "heads").rmdir()
    outside = _outside_metadata(tmp_path)
    _symlink_or_skip(git / "refs" / "heads", outside / "refs" / "heads",
                     target_is_directory=True)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("branches", {"repo": "."})


def test_git_ref_file_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    victim = tmp_path / "outside-ref"
    victim.write_text(f"{OUTSIDE_SECRET}\n", encoding="utf-8")
    _symlink_or_skip(git / "refs" / "heads" / "evil", victim)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("branches", {"repo": "."})


def test_git_packed_refs_symlink_rejected(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    victim = tmp_path / "outside-packed"
    victim.write_text(f"{OUTSIDE_SECRET} refs/heads/leaked\n", encoding="utf-8")
    _symlink_or_skip(git / "packed-refs", victim)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("branches", {"repo": "."})


def test_healthy_metadata_still_read(tmp_path):
    """Positive control: containment must not break legitimate reads."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _inside_git(sandbox)
    cap = _cap(sandbox)
    log = cap.execute("log", {"repo": "."})
    assert log["current_branch"] == "main"
    assert [c["message"] for c in log["commits"]] == ["first commit"]
    branches = cap.execute("branches", {"repo": "."})
    assert [b["name"] for b in branches["branches"]] == ["main"]


# --------------------------------------------------------------------- #
# engine-level adversarial regression
# --------------------------------------------------------------------- #


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(GitLogCapability(sandbox))
    planner = DeterministicPlanner()
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            allowed_scopes={"filesystem:read", "git:read"},
            boundaries={FS: RelativePathBoundary()},
        ),
    )


def test_engine_dotgit_symlink_task_fails_without_leak(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = _outside_metadata(tmp_path)
    _symlink_or_skip(sandbox / ".git", outside, target_is_directory=True)
    engine = _engine(tmp_path / "d.db", sandbox)
    try:
        task = engine.execute_goal("show git history for .")
        assert task.status == TaskStatus.FAILED
        result_text = json.dumps([s.result for s in task.steps])
        _assert_no_leak(result_text)
        _assert_no_leak(task.error or "")
    finally:
        engine.close()


def test_engine_git_head_symlink_task_fails_without_leak(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    git = _inside_git(sandbox)
    (git / "HEAD").unlink()
    victim = tmp_path / "outside-head"
    victim.write_text(f"ref: refs/heads/{OUTSIDE_SECRET}\n", encoding="utf-8")
    _symlink_or_skip(git / "HEAD", victim)
    engine = _engine(tmp_path / "d.db", sandbox)
    try:
        task = engine.execute_goal("show git history for .")
        assert task.status == TaskStatus.FAILED
        _assert_no_leak(json.dumps([s.result for s in task.steps]))
    finally:
        engine.close()
