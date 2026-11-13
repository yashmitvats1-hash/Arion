"""F06 regression: temporary-file write safety (issue #23 / issue #18 F06).

A planted ``.<name>.arion-write-tmp`` entry next to the write target must
never redirect or influence an approved write.  Staging files are created
EXCLUSIVELY (``O_CREAT|O_EXCL|O_NOFOLLOW``) under UNPREDICTABLE names, so:

- a planted symlink or plain file at ANY name is never followed, reused or
  cleaned up - it is left exactly as found (the generator draws another
  name; the fixed ``.<name>.arion-write-tmp`` name is never used);
- an outside file is never modified by a write whose staging path was
  tampered with;
- a crash-orphaned staging file can never block a later write to the same
  target (the fixed name turned one crash into a permanent fail-closed
  denial of service - the crash-orphan vulnerability of the third-pass
  review).

Invariant under test: outside files are never modified by a write whose
staging path was tampered with, the tampering itself is never cleaned up
or followed by the capability, and staging debris never denies service.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from arion.bootstrap import build_engine
from arion.capabilities import dirfs
from arion.capabilities.registry import CapabilityError
from arion.capabilities.write import FilesystemWriteCapability
from arion.orchestration.authz import (
    AutoApproveHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.state.models import PlanStep, VerificationPolicy

FS = "filesystem:path"
TEMP_SUFFIX = ".arion-write-tmp"


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks not available: {exc}")


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    return sb


def _cap(sandbox):
    return FilesystemWriteCapability(sandbox)


# --------------------------------------------------------------------- #
# capability-level: planted-entry regressions
# --------------------------------------------------------------------- #

def test_planted_temp_symlink_cannot_redirect_write(tmp_path):
    """The F06 counterexample: a temp symlink pointing OUTSIDE must never be
    followed; the write publishes only inside the sandbox and the victim is
    untouched; the planted link is left exactly as found."""
    sandbox = _sandbox(tmp_path)
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, victim)
    cap = _cap(sandbox)
    result = cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert result["written"] is True
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"
    assert planted.is_symlink()  # tampering left exactly as found


def test_planted_temp_symlink_inside_sandbox_also_untouched(tmp_path):
    """Exclusive creation under an unpredictable name never reuses ANY
    existing entry, even a link that stays inside the sandbox."""
    sandbox = _sandbox(tmp_path)
    inner = sandbox / "inner.txt"
    inner.write_text("INNER", encoding="utf-8")
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, inner)
    cap = _cap(sandbox)
    cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert inner.read_text(encoding="utf-8") == "INNER"
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"
    assert planted.is_symlink()


def test_planted_temp_file_left_untouched(tmp_path):
    """A pre-existing entry at the historical staging name is never
    truncated, replaced or cleaned up - and never blocks the write."""
    sandbox = _sandbox(tmp_path)
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    planted.write_text("ATTACKER_CONTENT", encoding="utf-8")
    cap = _cap(sandbox)
    cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert planted.read_text(encoding="utf-8") == "ATTACKER_CONTENT"
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"


def test_planted_temp_dangling_symlink_refused_and_left(tmp_path):
    """Even a dangling staging symlink is never followed or reused; its
    target is never created."""
    sandbox = _sandbox(tmp_path)
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, tmp_path / "never-created.txt")
    cap = _cap(sandbox)
    cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert planted.is_symlink()
    assert not (tmp_path / "never-created.txt").exists()
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"


def test_crash_orphan_staging_never_blocks_future_write(tmp_path):
    """Crash-orphan regression: a staging file orphaned by a crash (at the
    historical fixed name AND at a random name) must not deny service to
    later writes - and is itself left untouched."""
    sandbox = _sandbox(tmp_path)
    old_orphan = sandbox / f".report.txt{TEMP_SUFFIX}"
    old_orphan.write_text("PARTIAL-PAYLOAD", encoding="utf-8")
    random_orphan = sandbox / f".report.txt.deadbeef12{TEMP_SUFFIX}"
    random_orphan.write_text("PARTIAL-PAYLOAD", encoding="utf-8")
    cap = _cap(sandbox)
    cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"
    assert old_orphan.read_text(encoding="utf-8") == "PARTIAL-PAYLOAD"
    assert random_orphan.read_text(encoding="utf-8") == "PARTIAL-PAYLOAD"


def test_staging_name_is_unpredictable_and_unique(tmp_path):
    import os
    sandbox = _sandbox(tmp_path)
    dfd = os.open(str(sandbox), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd1, name1 = dirfs.open_staging(dfd, "report.txt")
        os.close(fd1)
        fd2, name2 = dirfs.open_staging(dfd, "report.txt")
        os.close(fd2)
    finally:
        os.close(dfd)
    assert name1 != name2
    assert name1 != f".report.txt{TEMP_SUFFIX}"
    assert name1.startswith(".report.txt.") and name1.endswith(TEMP_SUFFIX)
    assert name2.startswith(".report.txt.") and name2.endswith(TEMP_SUFFIX)


def test_staging_collision_fails_closed_and_leaves_entry(tmp_path, monkeypatch):
    """If every generated name collides (simulated), the write fails closed
    and the occupying entry is left exactly as found."""
    sandbox = _sandbox(tmp_path)
    monkeypatch.setattr(dirfs.os, "urandom", lambda n: b"\x00" * n)
    planted = sandbox / f".report.txt.{('00' * 8)}{TEMP_SUFFIX}"
    planted.write_text("OCCUPIED", encoding="utf-8")
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError, match="occupied"):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
        })
    monkeypatch.undo()
    assert planted.read_text(encoding="utf-8") == "OCCUPIED"
    assert not (sandbox / "report.txt").exists()


def test_staging_collision_skips_occupied_entry(tmp_path, monkeypatch):
    """A colliding generated name is skipped, never reused."""
    sandbox = _sandbox(tmp_path)
    planted = sandbox / f".report.txt.{'11' * 8}{TEMP_SUFFIX}"
    planted.write_text("OCCUPIED", encoding="utf-8")
    tokens = [b"\x11" * 8, b"\x22" * 8, b"\x33" * 8]  # 3rd: cleanup tomb
    monkeypatch.setattr(dirfs.os, "urandom", lambda n: tokens.pop(0))
    cap = _cap(sandbox)
    cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    monkeypatch.undo()
    assert planted.read_text(encoding="utf-8") == "OCCUPIED"
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"


def test_successful_write_creates_target_and_no_temp_leftover(tmp_path):
    """Positive control: containment hardening must not break normal writes."""
    sandbox = _sandbox(tmp_path)
    cap = _cap(sandbox)
    result = cap.execute("write", {
        "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
    })
    assert result["written"] is True and result["size"] == len(b"AGENT_PAYLOAD")
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"
    leftovers = [p for p in sandbox.iterdir() if p.name.endswith(TEMP_SUFFIX)]
    assert leftovers == []


def test_overwrite_semantics_preserved(tmp_path):
    sandbox = _sandbox(tmp_path)
    (sandbox / "report.txt").write_text("OLD", encoding="utf-8")
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "NEW", "overwrite": False,
        })
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "OLD"
    cap.execute("write", {
        "path": "report.txt", "content": "NEW", "overwrite": True,
    })
    assert (sandbox / "report.txt").read_text(encoding="utf-8") == "NEW"


def test_target_symlink_escape_still_rejected(tmp_path):
    """Pre-existing guarantee, pinned: a direct target symlink escaping the
    sandbox is still rejected by containment (unchanged behaviour)."""
    sandbox = _sandbox(tmp_path)
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    _symlink_or_skip(sandbox / "report.txt", victim)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": True,
        })
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


# --------------------------------------------------------------------- #
# engine-level: approved write with a planted temp symlink
# --------------------------------------------------------------------- #

class _FixedPlanner:
    last_source = "deterministic"
    last_transformation = None

    def __init__(self, steps, required):
        self._steps = steps
        self._required = set(required)

    def plan(self, goal_description, task_id, registry, context=None):
        return copy.deepcopy(self._steps)

    def required_capabilities(self, goal_description):
        return set(self._required)


def _write_step(path="report.txt", content="AGENT_PAYLOAD"):
    return PlanStep(
        index=0, intent="write report", capability="filesystem.write", action="write",
        scope="filesystem:write",
        params={"path": path, "content": content, "overwrite": False},
        verification=VerificationPolicy("non_empty"),
    )


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        boundaries={FS: RelativePathBoundary()},
        risk_deny=set(),
        risk_approve={"high"},
    )


OFFLINE_MODEL_CONFIG = SimpleNamespace(
    enabled=False, fallback_enabled=False, reflection_enabled=False
)


def test_engine_approved_write_with_planted_temp_symlink_cannot_redirect(tmp_path):
    """End-to-end: with write scope + human approval, a planted staging
    symlink cannot redirect the write; the write lands inside the sandbox,
    the victim is untouched and the tampering is left as found."""
    sandbox = _sandbox(tmp_path)
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, victim)
    planner = _FixedPlanner([_write_step()], {"filesystem.write"})
    engine = build_engine(
        tmp_path / "arion.db", sandbox, planner=planner, policy=_policy(),
        approval_handler=AutoApproveHandler(), model_config=OFFLINE_MODEL_CONFIG,
    )
    try:
        goal = engine.submit_goal("write report")
        final = engine.run_goal(goal.id)
        assert final.status.value == "completed"
        assert victim.read_text(encoding="utf-8") == "ORIGINAL"
        assert (sandbox / "report.txt").read_text(encoding="utf-8") == "AGENT_PAYLOAD"
        assert planted.is_symlink()
    finally:
        engine.close()
