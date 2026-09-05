"""ADR-061 C4: every declared resource is boundary-checked.

Covers D3 and invariants 5, 6 (and 16 for the unchanged singular path).

This is the FIRST behavioural/security commit of M8. It closes the D8 gap:
before C4 a two-resource action's destination was never boundary-checked, so
`dest=/etc/passwd` and `dest=../../etc/passwd` were ALLOWED by policy.
"""

import pytest

from arion.capabilities.registry import ActionSpec, ResourceRole
from arion.orchestration.authz import (
    Actor,
    AuthorizationRequest,
    PathPrefixBoundary,
    PolicyOutcome,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.resource_set import resolve_resources


def _policy(**kw):
    return ResourcePolicy(
        allowed_scopes={"filesystem:write", "filesystem:read"},
        risk_deny=set(), risk_approve=set(),
        boundaries={"filesystem:path": RelativePathBoundary()},
        **kw,
    )


def _move_spec():
    return ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        risk="high", side_effects="mutating",
        param_schema={"source": {"type": "string", "required": True},
                      "dest": {"type": "string", "required": True}},
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )


def _read_spec():
    return ActionSpec(
        name="read", description="d", required_scope="filesystem:read",
        resource_kind="filesystem:path", resource_param="path",
    )


def _request(spec, params, capability="filesystem.move"):
    return AuthorizationRequest(
        actor=Actor(kind="user", name="tester"),
        task_id="t", step_index=0,
        capability=capability, action=spec.name,
        scope=spec.required_scope, params=dict(params),
        resource=(resolve_resources(spec, params)[0].value
                  if resolve_resources(spec, params) else None),
        resource_kind=spec.resource_kind,
        risk=spec.risk, side_effects=spec.side_effects,
        resources=resolve_resources(spec, params),
    )


def _decide(params, spec=None):
    spec = spec or _move_spec()
    return _policy().decide(_request(spec, params))


# --------------------------------------------------------------------------
# THE D8 SECURITY REGRESSION - destination outside the boundary must DENY
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dest", [
    "../../etc/passwd",     # traversal
    "../outside.txt",       # traversal, one level
    "/etc/passwd",          # absolute
    "/tmp/evil.txt",        # absolute
])
def test_destination_outside_boundary_is_denied(dest):
    """Before C4 every one of these was ALLOWED."""
    decision = _decide({"source": "README.md", "dest": dest})
    assert decision.outcome == PolicyOutcome.DENY
    assert "role 'dest'" in decision.reason


def test_source_outside_boundary_is_still_denied():
    """Pre-existing protection must not regress."""
    decision = _decide({"source": "../../etc/passwd", "dest": "ok.md"})
    assert decision.outcome == PolicyOutcome.DENY
    assert "role 'source'" in decision.reason


def test_both_outside_denies_and_names_the_first_failure():
    decision = _decide({"source": "../a", "dest": "/etc/passwd"})
    assert decision.outcome == PolicyOutcome.DENY
    assert "role 'source'" in decision.reason


# --------------------------------------------------------------------------
# the positive case must survive
# --------------------------------------------------------------------------

def test_both_inside_boundary_is_allowed():
    decision = _decide({"source": "README.md", "dest": "ARCHIVE.md"})
    assert decision.outcome == PolicyOutcome.ALLOW


def test_nested_relative_paths_inside_are_allowed():
    decision = _decide({"source": "docs/a.md", "dest": "docs/archive/b.md"})
    assert decision.outcome == PolicyOutcome.ALLOW


def test_dotted_but_contained_paths_are_allowed():
    decision = _decide({"source": "./README.md", "dest": "d/../ARCHIVE.md"})
    assert decision.outcome == PolicyOutcome.ALLOW


# --------------------------------------------------------------------------
# invariants 5, 6 - fail closed on unresolved / unconfigured
# --------------------------------------------------------------------------

def test_missing_destination_fails_closed():
    """An unresolved role must REFUSE, never be skipped as 'nothing to check'."""
    decision = _decide({"source": "README.md"})
    assert decision.outcome == PolicyOutcome.DENY
    assert "missing resource" in decision.reason
    assert "role 'dest'" in decision.reason


def test_non_string_destination_fails_closed():
    decision = _decide({"source": "README.md", "dest": 42})
    assert decision.outcome == PolicyOutcome.DENY
    assert "role 'dest'" in decision.reason


def test_empty_destination_fails_closed():
    decision = _decide({"source": "README.md", "dest": ""})
    assert decision.outcome == PolicyOutcome.DENY
    assert "role 'dest'" in decision.reason


def test_unconfigured_kind_on_second_role_fails_closed():
    """invariant 6 - ANY resource lacking a boundary fails closed."""
    spec = ActionSpec(
        name="fetch", description="d", required_scope="filesystem:write",
        side_effects="mutating",
        param_schema={"src": {"type": "string", "required": True},
                      "out": {"type": "string", "required": True}},
        resources=[ResourceRole("out", "filesystem:path"),
                   ResourceRole("src", "url")],   # 'url' has no boundary
    )
    decision = _policy().decide(_request(spec, {"out": "a.md", "src": "http://x"}))
    assert decision.outcome == PolicyOutcome.DENY
    assert "no resource boundary configured" in decision.reason


def test_role_name_appears_in_diagnostic():
    """Diagnostics must distinguish which role failed."""
    # NB: assert on the explicit role clause - the word "resource" itself
    # contains the substring "source", so a naive `in` check is misleading.
    d1 = _decide({"source": "../a", "dest": "ok.md"})
    d2 = _decide({"source": "ok.md", "dest": "../a"})
    assert "role 'source'" in d1.reason
    assert "role 'dest'" not in d1.reason
    assert "role 'dest'" in d2.reason
    assert "role 'source'" not in d2.reason


# --------------------------------------------------------------------------
# invariant 16 - the singular path is unchanged
# --------------------------------------------------------------------------

def test_single_resource_inside_is_allowed():
    decision = _policy().decide(
        _request(_read_spec(), {"path": "notes.md"}, capability="filesystem.read"))
    assert decision.outcome == PolicyOutcome.ALLOW


def test_single_resource_outside_is_denied():
    decision = _policy().decide(
        _request(_read_spec(), {"path": "../../etc/passwd"},
                 capability="filesystem.read"))
    assert decision.outcome == PolicyOutcome.DENY


def test_legacy_request_without_resource_set_still_checked():
    """A hand-built request with no `resources` must use the singular path."""
    req = AuthorizationRequest(
        actor=Actor(kind="user", name="t"), task_id="t", step_index=0,
        capability="filesystem.read", action="read", scope="filesystem:read",
        params={"path": "../../etc/passwd"},
        resource="../../etc/passwd", resource_kind="filesystem:path",
    )
    assert req.resources == []
    assert _policy().decide(req).outcome == PolicyOutcome.DENY


def test_resourceless_action_is_not_denied_on_resource_grounds():
    req = AuthorizationRequest(
        actor=Actor(kind="user", name="t"), task_id="t", step_index=0,
        capability="clock", action="now", scope="filesystem:read", params={},
    )
    assert _policy().decide(req).outcome == PolicyOutcome.ALLOW


def test_path_prefix_boundary_applies_per_role():
    """A different boundary implementation must also apply to every role."""
    # PathPrefixBoundary rejects absolute paths outright, so its prefixes are
    # relative (it constrains WHICH subtree inside the sandbox is reachable).
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:write"}, risk_deny=set(), risk_approve=set(),
        boundaries={"filesystem:path": PathPrefixBoundary(["docs"])},
    )
    ok = policy.decide(_request(_move_spec(),
                                {"source": "docs/a.md", "dest": "docs/b.md"}))
    bad = policy.decide(_request(_move_spec(),
                                 {"source": "docs/a.md", "dest": "src/b.md"}))
    assert ok.outcome == PolicyOutcome.ALLOW
    assert bad.outcome == PolicyOutcome.DENY
    assert "role 'dest'" in bad.reason
