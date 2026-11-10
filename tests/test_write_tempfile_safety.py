"""F06 regression: temporary-file write safety (issue #23 / issue #18 F06).

A planted ``.<name>.arion-write-tmp`` symlink next to the write target must
never redirect an approved write outside the sandbox.  The staging file is
created exclusively (``O_CREAT|O_EXCL|O_NOFOLLOW``), so ANY pre-existing
entry at the temp name - symlink or plain file - fails the write closed and
is left exactly as found.

Invariant under test: outside files are never modified by a write whose
staging path was tampered with, and the tampering itself is not cleaned up
or followed by the capability.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from arion.bootstrap import build_engine
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
# capability-level: planted-symlink regression
# --------------------------------------------------------------------- #


def test_planted_temp_symlink_cannot_redirect_write(tmp_path):
    """The F06 counterexample: a temp symlink pointing OUTSIDE must not be
    followed; the write fails closed and the victim is untouched."""
    sandbox = _sandbox(tmp_path)
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, victim)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
        })
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert not (sandbox / "report.txt").exists()
    assert planted.is_symlink()  # tampering left exactly as found


def test_planted_temp_symlink_inside_sandbox_also_refused(tmp_path):
    """Exclusive creation refuses ANY existing entry, even a link that stays
    inside the sandbox: the staging name is never followed or reused."""
    sandbox = _sandbox(tmp_path)
    inner = sandbox / "inner.txt"
    inner.write_text("INNER", encoding="utf-8")
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, inner)
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
        })
    assert inner.read_text(encoding="utf-8") == "INNER"
    assert not (sandbox / "report.txt").exists()
    assert planted.is_symlink()


def test_planted_temp_file_left_untouched(tmp_path):
    """A pre-occupied staging name (plain file) also fails closed: exclusive
    creation never truncates or replaces an entry it did not create."""
    sandbox = _sandbox(tmp_path)
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    planted.write_text("ATTACKER_CONTENT", encoding="utf-8")
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
        })
    assert planted.read_text(encoding="utf-8") == "ATTACKER_CONTENT"
    assert not (sandbox / "report.txt").exists()


def test_planted_temp_dangling_symlink_refused(tmp_path):
    """Even a dangling staging symlink (target never created) is refused:
    ``O_CREAT|O_EXCL`` rejects symbolic links outright."""
    sandbox = _sandbox(tmp_path)
    planted = sandbox / f".report.txt{TEMP_SUFFIX}"
    _symlink_or_skip(planted, tmp_path / "never-created.txt")
    cap = _cap(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "report.txt", "content": "AGENT_PAYLOAD", "overwrite": False,
        })
    assert planted.is_symlink()
    assert not (sandbox / "report.txt").exists()


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


def test_engine_approved_write_with_planted_temp_symlink_fails_closed(tmp_path):
    """End-to-end: with write scope + human approval, a planted staging
    symlink must not redirect the write; the goal fails instead."""
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
        assert final.status.value != "completed"
        assert victim.read_text(encoding="utf-8") == "ORIGINAL"
        assert not (sandbox / "report.txt").exists()
        assert planted.is_symlink()
    finally:
        engine.close()
