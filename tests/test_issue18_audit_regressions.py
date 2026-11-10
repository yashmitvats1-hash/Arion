"""Issue #18 audit gates for confirmed baseline defects.

These are STRICT XFAILs on purpose: each test states the desired safety or
semantic invariant, while the current baseline is known to violate it. They
keep the defects visible in the suite without changing existing tests or
pretending the product is fixed. When a fix lands, the corresponding case
must XPASS (which strict xfail reports as a failure) and then be converted
to an ordinary passing regression test in the fix PR.

Gates whose defects have been remediated on this branch live in
``FIXED_GATES`` and run as ordinary passing regression tests; the remaining
open defects (tracked by issue #22) stay in ``UNFIXED_GATES`` as strict
xfails.

The executable counterexamples live in ``scripts/audit_issue18_repro.py``
and hostile-input negative controls in ``scripts/audit_issue18_controls.py``
so the coordinator can reproduce them without pytest. See
``docs/agent_reports/ARENA_ISSUE_18_AUDIT.md`` for severity, reachability and
source locations. No production capability or policy is changed here.
"""
from __future__ import annotations

import pytest

from scripts import audit_issue18_controls as controls
from scripts import audit_issue18_repro as repro


def _root(tmp_path):
    (tmp_path / "sandbox").mkdir()
    return tmp_path


# (probe function, finding id, desired invariant)
#
# FIXED_GATES: defects remediated on this branch.  These run as ORDINARY
# passing regression tests - the probe must report that the undesirable
# baseline behaviour is no longer reachable.
FIXED_GATES = [
    pytest.param(repro.git_dotgit_symlink, "F05", "Git metadata reads must stay inside the sandbox", id="F05-git-dotgit-symlink"),
]

# UNFIXED_GATES: strict expected failures for confirmed defects that are
# still open (tracked by issue #22).  Not product passes; each must be
# converted to an ordinary passing regression test by its own fix PR.
UNFIXED_GATES = [
    pytest.param(repro.deterministic_rename, "F01", "unsupported rename must not complete as a read", id="F01-deterministic-rename"),
    pytest.param(repro.model_rename, "F02", "model-proposed read must not satisfy a rename goal", id="F02-model-rename"),
    pytest.param(repro.memory_skip, "F03", "skipping a required read must not complete the goal", id="F03-memory-skip"),
    pytest.param(repro.http_500, "F04", "HTTP 500 must not verify as a successful fetch", id="F04-http-500"),
    pytest.param(repro.write_temp_symlink, "F06", "approved writes must not follow a planted temp symlink outside the sandbox", id="F06-write-temp-symlink"),
    pytest.param(repro.approval_payload_swap, "F07", "an approval must bind the write payload", id="F07-approval-payload"),
    pytest.param(repro.redirect_allowlist_mismatch, "F08", "redirects must satisfy the policy URL boundary", id="F08-redirect-boundary"),
    pytest.param(repro.provider_unbounded_read, "F09", "provider response size must be bounded during read", id="F09-provider-read-bound"),
    pytest.param(repro.crash_append_duplicate, "F10", "hard-crash resume must not duplicate a non-retry-safe append", id="F10-crash-append"),
    pytest.param(repro.declaration_taxonomy_fail_open, "F11", "unknown risk/side-effects declarations must fail closed at registration", id="F11-declaration-taxonomy"),
    pytest.param(repro.observation_non_finite, "F12", "observation contract must reject non-finite floats", id="F12-observation-non-finite"),
]


@pytest.mark.parametrize("probe,finding,invariant", FIXED_GATES)
def test_issue18_fixed_invariant(probe, finding, invariant, tmp_path):
    result = probe(_root(tmp_path))
    if result.get("skipped"):
        pytest.skip(result["skipped"])
    assert not result.get("reproduced"), {"finding": finding, "invariant": invariant, **result}


@pytest.mark.xfail(reason="Issue #18 confirmed baseline defect; strict gate must be removed by the fix PR", strict=True)
@pytest.mark.parametrize("probe,finding,invariant", UNFIXED_GATES)
def test_issue18_desired_invariant(probe, finding, invariant, tmp_path):
    result = probe(_root(tmp_path))
    if result.get("skipped"):
        pytest.skip(result["skipped"])
    assert not result.get("reproduced"), {"finding": finding, "invariant": invariant, **result}


def test_audit_control_default_policy_still_denies_writes(tmp_path):
    """Positive control: the write probes require explicit operator opt-in.

    The audit harness must not accidentally demonstrate a default-allow
    regression. With the shipped default policy, a proposed write is denied
    and the target is never created.
    """
    root = _root(tmp_path)
    planner = repro.FixedPlanner([repro._write_step(path="denied.txt")], {"filesystem.write"})
    engine = repro._build(root, planner=planner)  # default policy: no filesystem:write
    try:
        goal = engine.submit_goal("write denied.txt")
        engine.run_goal(goal.id)
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status.value == "failed"
        assert not (root / "sandbox" / "denied.txt").exists()
    finally:
        engine.close()


@pytest.mark.parametrize("name", sorted(controls.CONTROLS))
def test_hostile_boundary_negative_control(name):
    """Required-work-5 negative controls: hostile plan/output must be rejected."""
    assert controls.CONTROLS[name](), name
