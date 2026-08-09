"""Resource-aware authorization tests (ADR-009, hardened).

Required adversarial scenarios:
1. resource-sensitive action + missing boundary        -> DENY (fail closed)
2. resource-sensitive action + valid boundary          -> normal policy evaluation
3. resource outside boundary                           -> DENY
4. non-resource action is NOT denied for lacking a path
5. plan cannot bypass resource policy by manipulating its declared
   scope/resource (scope spoofing, resource-param smuggling)
6. existing filesystem sandbox tests remain green (regression suite)
7. approval and retry behavior remains unchanged (test_approval.py,
   test_semantics.py)

Plus identity/delegation tests (user -> agent -> delegated agent).
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    Actor,
    AuthorizationRequest,
    PathPrefixBoundary,
    PolicyOutcome,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def fs_policy(**kw) -> ResourcePolicy:
    """Policy with the filesystem boundary configured (as bootstrap does)."""
    boundaries = kw.pop("boundaries", None)
    if boundaries is None:
        boundaries = {FS: RelativePathBoundary()}
    return ResourcePolicy(boundaries=boundaries, **kw)


def _request(**overrides) -> AuthorizationRequest:
    base = dict(
        actor=Actor.agent("system"),
        task_id="task_t",
        step_index=0,
        capability="filesystem.read",
        action="read",
        scope="filesystem:read",
        params={"path": "README.md"},
        resource="README.md",
        resource_kind=FS,
        risk="low",
        side_effects="read_only",
        idempotent=True,
        retry_safe=True,
    )
    base.update(overrides)
    return AuthorizationRequest(**base)


# ---------------------------------------------------------------------------
# Policy unit tests - the 5 core scenarios
# ---------------------------------------------------------------------------


def test_resource_action_with_valid_boundary_allowed():
    """Scenario 2: resource-sensitive action + valid boundary -> ALLOW."""
    assert fs_policy().decide(_request()).outcome == PolicyOutcome.ALLOW


def test_resource_action_missing_boundary_denied():
    """Scenario 1: fail closed - no boundary for the resource kind -> DENY."""
    decision = ResourcePolicy().decide(_request())
    assert decision.outcome == PolicyOutcome.DENY
    assert "no resource boundary" in decision.reason


def test_resource_outside_boundary_denied():
    """Scenario 3: resource outside the configured boundary -> DENY."""
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    decision = policy.decide(_request(params={"path": "notes.txt"}, resource="notes.txt"))
    assert decision.outcome == PolicyOutcome.DENY
    assert "outside boundary" in decision.reason


def test_resource_within_prefix_boundary_allowed():
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    decision = policy.decide(_request(params={"path": "public/a.txt"}, resource="public/a.txt"))
    assert decision.outcome == PolicyOutcome.ALLOW


def test_non_resource_action_not_denied_without_path():
    """Scenario 4: an action with no resource is never denied for lacking a path."""
    decision = ResourcePolicy().decide(_request(resource_kind=None, resource=None))
    assert decision.outcome == PolicyOutcome.ALLOW


def test_resource_action_missing_resource_param_fails_closed():
    """A resource-sensitive action whose resource param is absent -> DENY."""
    decision = fs_policy().decide(_request(params={}, resource=None))
    assert decision.outcome == PolicyOutcome.DENY
    assert "missing resource" in decision.reason


def test_policy_denies_write_and_unknown_scopes():
    assert ResourcePolicy().decide(_request(scope="filesystem:write", action="write", risk="medium")).outcome == PolicyOutcome.DENY
    assert ResourcePolicy().decide(_request(scope="shell:exec", risk="high")).outcome == PolicyOutcome.DENY


def test_policy_denies_high_risk_and_approves_medium():
    policy = ResourcePolicy(allowed_scopes={"risk:run"})
    assert policy.decide(_request(scope="risk:run", risk="high", side_effects="irreversible", resource_kind=None, resource=None)).outcome == PolicyOutcome.DENY
    assert policy.decide(_request(scope="risk:run", risk="medium", side_effects="mutating", resource_kind=None, resource=None)).outcome == PolicyOutcome.REQUIRE_APPROVAL


# ---------------------------------------------------------------------------
# Identity / delegation tests
# ---------------------------------------------------------------------------


def test_policy_evaluates_actor_chain():
    """user:alice -> agent:arion delegation: matching an ancestor permits."""
    policy = ResourcePolicy(allowed_agents={"user:alice"}, boundaries={FS: RelativePathBoundary()})
    delegated = Actor.user("alice").delegated("arion")
    assert policy.decide(_request(actor=delegated)).outcome == PolicyOutcome.ALLOW

    bob = Actor.agent("bob")
    assert policy.decide(_request(actor=bob)).outcome == PolicyOutcome.DENY

    # direct actor match also works
    assert policy.decide(_request(actor=Actor.user("alice"))).outcome == PolicyOutcome.ALLOW


def test_actor_builds_delegation_chain():
    actor = Actor.user("alice").delegated("arion").delegated("delegate-7")
    assert actor.id == "agent:delegate-7"
    assert actor.chain == ("user:alice", "agent:arion", "agent:delegate-7")


# ---------------------------------------------------------------------------
# Engine-level authorization (real orchestration path)
# ---------------------------------------------------------------------------


def _run_steps(sandbox, db_path, steps, policy=None, actor=None, extra_caps=None):
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    for cap in (extra_caps or []):
        registry.register(cap)
    storage = SQLiteStorage(db_path)
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=DeterministicPlanner(),
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=policy or fs_policy(),
        actor=actor,
    )
    goal = engine.submit_goal("test goal")
    task = engine.create_task(goal)
    task.steps = steps
    engine.storage.save_task(task)
    result = engine.run_task(task.id)
    return engine, result


def _step(intent="read", capability="filesystem.read", action="read", scope="filesystem:read",
          params=None, verification=None):
    return PlanStep(
        index=0, intent=intent, capability=capability, action=action, scope=scope,
        params=params or {}, verification=verification or VerificationPolicy("non_empty"),
    )


def test_authorized_filesystem_operation(sandbox, db_path):
    """Scenario 2 (end-to-end): authorized read completes."""
    engine, task = _run_steps(sandbox, db_path, [_step(params={"path": "README.md"})])
    assert task.status == TaskStatus.COMPLETED
    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert "content" in task.steps[0].result


def test_unauthorized_path_denied(sandbox, db_path):
    """Scenario 3 (end-to-end): path outside the configured boundary -> DENY."""
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    engine, task = _run_steps(sandbox, db_path, [_step(params={"path": "notes.txt"})], policy=policy)
    assert task.status == TaskStatus.FAILED
    assert "outside boundary" in (task.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "permission.denied" in kinds


def test_non_resource_capability_runs_without_boundary(sandbox, db_path):
    """Scenario 4 (end-to-end): a no-resource capability runs under a bare policy."""

    class NonResourceCapability:
        name = "clock.now"
        description = "reads the clock"
        actions = [ActionSpec(name="now", description="now", required_scope="clock:read",
                              risk="low", side_effects="read_only", retry_safe=True)]

        def execute(self, action, params):
            return {"time": "12:00:00"}

    engine, task = _run_steps(
        sandbox, db_path,
        [_step(intent="time", capability="clock.now", action="now", scope="clock:read", params={})],
        policy=ResourcePolicy(allowed_scopes={"clock:read"}),
        extra_caps=[NonResourceCapability()],
    )
    assert task.status == TaskStatus.COMPLETED
    assert task.steps[0].result["time"] == "12:00:00"


# ---------------------------------------------------------------------------
# Adversarial: plan attempts to bypass the resource policy
# ---------------------------------------------------------------------------


def test_scope_spoofing_cannot_escalate(sandbox, db_path):
    """Scenario 5a: plan claims filesystem:write - still authorized as read."""
    engine, task = _run_steps(sandbox, db_path, [_step(scope="filesystem:write", params={"path": "README.md"})])
    assert task.status == TaskStatus.COMPLETED
    checked = [e for e in engine.storage.list_events(task.id) if e.kind == "permission.checked"]
    assert checked[0].detail["scope"] == "filesystem:read"          # resolved from ActionSpec
    assert checked[0].detail["step_declared_scope"] == "filesystem:write"  # the spoof is audited, not honored


def test_scope_spoofing_does_not_bypass_boundary(sandbox, db_path):
    """Scenario 5b: spoofing the scope cannot escape the path boundary."""
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    engine, task = _run_steps(sandbox, db_path, [_step(scope="filesystem:write", params={"path": "notes.txt"})], policy=policy)
    assert task.status == TaskStatus.FAILED
    assert "outside boundary" in (task.error or "")


def test_resource_param_smuggling_cannot_bypass(sandbox, db_path):
    """Scenario 5c: the resource is always read from ActionSpec.resource_param.

    - A plan that omits the declared param ("path") and smuggles the target
      under another key is treated as missing resource -> DENY (fail closed).
    - A plan that includes a valid "path" PLUS an extra key is not escalated:
      the extra key is inert; execution targets only the declared resource.
    """
    policy = ResourcePolicy(boundaries={FS: PathPrefixBoundary(["public/"])})
    for smuggled in ({"Path": "public/a.txt"}, {"filename": "public/a.txt"}):
        engine, task = _run_steps(sandbox, db_path, [_step(params=smuggled)], policy=policy)
        assert task.status == TaskStatus.FAILED, f"smuggled params {smuggled!r} were not denied"
        assert "missing resource" in (task.error or "")

    # valid "path" + evil extra key: the extra key is inert
    engine, task = _run_steps(
        sandbox, db_path,
        [_step(params={"path": "README.md", "Path": "../outside.txt"})],
        policy=fs_policy(),
    )
    assert task.status == TaskStatus.COMPLETED
    assert task.steps[0].result["path"] == "README.md"
    assert "content" in task.steps[0].result


def test_adversarial_path_traversal_denied(sandbox, db_path):
    for evil in ["../etc/passwd", "/etc/passwd", "public/../../etc/passwd", ".."]:
        engine, task = _run_steps(sandbox, db_path, [_step(params={"path": evil})])
        assert task.status == TaskStatus.FAILED, f"traversal {evil!r} was not denied"
        assert "outside boundary" in (task.error or "")


def test_adversarial_capability_and_action_rejected(sandbox, db_path):
    # unregistered capability
    engine, task = _run_steps(sandbox, db_path, [_step(capability="shell.exec", action="exec", scope="shell:exec", params={"cmd": "cat /etc/passwd"})])
    assert task.status == TaskStatus.FAILED
    assert "capability not found" in (task.error or "")

    # unknown action on a real capability - never executed
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))

    class SpyCapability(FilesystemReadCapability):
        executed: list = []

        def execute(self, action, params):
            SpyCapability.executed.append(action)
            return super().execute(action, params)

    registry.register(SpyCapability(sandbox))
    storage = SQLiteStorage(db_path)
    engine = ArionEngine(storage=storage, registry=registry, planner=DeterministicPlanner(),
                         router=DeterministicRouter(DeterministicPlanner()),
                         events=EventLogger(sinks=[storage]), policy=fs_policy())
    goal = engine.submit_goal("delete")
    task = engine.create_task(goal)
    task.steps = [_step(intent="delete", action="delete", scope="filesystem:write", params={"path": "README.md"})]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert "unknown action" in (result.error or "")
    assert SpyCapability.executed == []


def test_denied_high_risk_capability_never_executes(sandbox, db_path):
    class HighRiskCapability:
        name = "format.disk"
        description = "pretend disk format"
        actions = [ActionSpec(name="format", description="format", required_scope="storage:write",
                              risk="high", side_effects="irreversible", reversible=False,
                              idempotent=False, retry_safe=False)]

        def execute(self, action, params):
            raise AssertionError("high-risk capability must never execute")

    policy = ResourcePolicy(allowed_scopes={"storage:write"})  # no boundaries: any resource kind denied too
    engine, task = _run_steps(
        sandbox, db_path,
        [_step(intent="format", capability="format.disk", action="format", scope="storage:write", params={"drive": "/dev/sda"})],
        policy=policy, extra_caps=[HighRiskCapability()],
    )
    assert task.status == TaskStatus.FAILED
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "permission.denied" in kinds


def test_audit_events_carry_decision_and_identity(sandbox, db_path):
    engine, task = _run_steps(
        sandbox, db_path,
        [_step(params={"path": "README.md"})],
        actor=Actor.user("alice").delegated("arion"),
    )
    assert task.status == TaskStatus.COMPLETED
    checked = [e for e in engine.storage.list_events(task.id) if e.kind == "permission.checked"][0]
    detail = checked.detail
    assert detail["outcome"] == "allow"
    assert detail["scope"] == "filesystem:read"
    assert detail["resource"] == "README.md"
    assert detail["resource_kind"] == "filesystem:path"
    assert detail["risk"] == "low"
    assert detail["actor"] == "agent:arion"
    assert detail["actor_chain"] == ["user:alice", "agent:arion"]
