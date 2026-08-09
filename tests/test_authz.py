"""Resource-aware authorization tests (ADR-009).

Covers the ten required scenarios:
1. authorized filesystem operation
2. unauthorized path/resource
3. unauthorized operation
4. denied high-risk capability
5. REQUIRE_APPROVAL behavior (see test_approval.py)
6. capability metadata (see test_capabilities.py)
7. retry behavior per execution semantics (see test_semantics.py)
8. restart behavior (see test_persistence.py)
9. preservation of audit events (see test_audit.py)
10. regression coverage: the full suite

Plus adversarial tests: malformed plans attempting to bypass authorization
through their parameters or claimed scopes.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    AuthorizationRequest,
    PolicyOutcome,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage


def _request(**overrides) -> AuthorizationRequest:
    base = dict(
        agent="system",
        task_id="task_t",
        step_index=0,
        capability="filesystem.read",
        action="read",
        scope="filesystem:read",
        params={"path": "README.md"},
        resource="README.md",
        risk="low",
        side_effects="read_only",
        idempotent=True,
        retry_safe=True,
    )
    base.update(overrides)
    return AuthorizationRequest(**base)


def _engine_with(policy, registry, db_path):
    storage = SQLiteStorage(db_path)
    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=DeterministicPlanner(),
        router=DeterministicRouter(DeterministicPlanner()),
        events=EventLogger(sinks=[storage]),
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Policy unit tests
# ---------------------------------------------------------------------------


def test_policy_allows_authorized_read():
    decision = ResourcePolicy().decide(_request())
    assert decision.outcome == PolicyOutcome.ALLOW


def test_policy_denies_write_scope():
    decision = ResourcePolicy().decide(_request(scope="filesystem:write", action="write", risk="medium"))
    assert decision.outcome == PolicyOutcome.DENY
    assert "not permitted" in decision.reason


def test_policy_denies_unknown_scope():
    decision = ResourcePolicy().decide(_request(scope="shell:exec", risk="high"))
    assert decision.outcome == PolicyOutcome.DENY


def test_policy_denies_high_risk_even_when_scope_allowed():
    policy = ResourcePolicy(allowed_scopes={"filesystem:read", "risk:run"})
    decision = policy.decide(_request(scope="risk:run", risk="high", side_effects="irreversible"))
    assert decision.outcome == PolicyOutcome.DENY
    assert "risk" in decision.reason


def test_policy_requires_approval_for_medium_risk():
    policy = ResourcePolicy(allowed_scopes={"filesystem:read", "medium:run"})
    decision = policy.decide(_request(scope="medium:run", risk="medium", side_effects="mutating"))
    assert decision.outcome == PolicyOutcome.REQUIRE_APPROVAL


def test_policy_path_constraints():
    policy = ResourcePolicy(path_constraints={("filesystem.read", "read"): ["public/"]})
    assert policy.decide(_request(params={"path": "public/a.txt"}, resource="public/a.txt")).outcome == PolicyOutcome.ALLOW
    assert policy.decide(_request(params={"path": "notes.txt"}, resource="notes.txt")).outcome == PolicyOutcome.DENY
    assert policy.decide(_request(params={"path": "docs/x.md"}, resource="docs/x.md")).outcome == PolicyOutcome.DENY


def test_policy_agent_identity():
    policy = ResourcePolicy(allowed_agents={"system"})
    assert policy.decide(_request(agent="system")).outcome == PolicyOutcome.ALLOW
    assert policy.decide(_request(agent="guest")).outcome == PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Engine-level authorization (through the real orchestration path)
# ---------------------------------------------------------------------------


def _run_steps(sandbox, db_path, steps, policy=None):
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    engine = _engine_with(policy or ResourcePolicy(), registry, db_path)
    goal = engine.submit_goal("test goal")
    task = engine.create_task(goal)
    task.steps = steps
    engine.storage.save_task(task)
    result = engine.run_task(task.id)
    return engine, result


def test_authorized_filesystem_operation(sandbox, db_path):
    _, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"path": "README.md"},
                  verification=VerificationPolicy("non_empty"))],
    )
    assert task.status == TaskStatus.COMPLETED
    assert task.steps[0].status == StepStatus.SUCCEEDED


def test_unauthorized_path_denied(sandbox, db_path):
    """Scenario 2: policy constrains paths; the plan targets an unallowed path."""
    policy = ResourcePolicy(path_constraints={("filesystem.read", "read"): ["public/"]})
    engine, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"path": "notes.txt"},
                  verification=VerificationPolicy("non_empty"))],
        policy=policy,
    )
    assert task.status == TaskStatus.FAILED
    assert "resource" in (task.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "permission.denied" in kinds


def test_unauthorized_operation_unknown_action(sandbox, db_path):
    """Scenario 3a: the action does not exist on the capability - never executed."""
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))

    class SpyingCapability(FilesystemReadCapability):
        executed_actions: list[str] = []

        def execute(self, action, params):
            SpyingCapability.executed_actions.append(action)
            return super().execute(action, params)

    registry.register(SpyingCapability(sandbox))
    engine = _engine_with(ResourcePolicy(), registry, db_path)
    goal = engine.submit_goal("delete something")
    task = engine.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="delete", capability="filesystem.read", action="delete",
                 scope="filesystem:write", params={"path": "README.md"},
                 verification=VerificationPolicy("non_empty"))
    ]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert "unknown action" in (result.error or "")
    assert SpyingCapability.executed_actions == []  # never reached the capability


def test_denied_high_risk_capability(sandbox, db_path):
    """Scenario 4: scope allowed, but risk=high is denied by policy."""
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))

    class HighRiskCapability:
        name = "format.disk"
        description = "pretend disk format"
        actions = [
            ActionSpec(name="format", description="format", required_scope="storage:write",
                       risk="high", side_effects="irreversible", reversible=False,
                       idempotent=False, retry_safe=False)
        ]

        def execute(self, action, params):
            raise AssertionError("high-risk capability must never execute")

    registry.register(HighRiskCapability())
    policy = ResourcePolicy(allowed_scopes={"filesystem:read", "storage:write"})
    engine = _engine_with(policy, registry, db_path)
    goal = engine.submit_goal("format the disk")
    task = engine.create_task(goal)
    task.steps = [
        PlanStep(index=0, intent="format", capability="format.disk", action="format",
                 scope="storage:write", params={"drive": "/dev/sda"},
                 verification=VerificationPolicy("non_empty"))
    ]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert "risk" in (result.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "permission.denied" in kinds


# ---------------------------------------------------------------------------
# Adversarial: malformed plans trying to bypass authorization via params
# ---------------------------------------------------------------------------


def test_adversarial_path_traversal_denied(sandbox, db_path):
    policy = ResourcePolicy(path_constraints={("filesystem.read", "read"): ["public/"]})
    for evil in ["public/../../etc/passwd", "../etc/passwd", "/etc/passwd", "public/../secret.txt"]:
        _, task = _run_steps(
            sandbox, db_path,
            [PlanStep(index=0, intent="evil", capability="filesystem.read", action="read",
                      scope="filesystem:read", params={"path": evil},
                      verification=VerificationPolicy("non_empty"))],
            policy=policy,
        )
        assert task.status == TaskStatus.FAILED, f"traversal {evil!r} was not denied"
        assert "resource" in (task.error or "")


def test_adversarial_scope_spoofing_cannot_escalate(sandbox, db_path):
    """Plan claims filesystem:write for a read action - must still run as read."""
    engine, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="spoof", capability="filesystem.read", action="read",
                  scope="filesystem:write", params={"path": "README.md"},
                  verification=VerificationPolicy("non_empty"))],
    )
    assert task.status == TaskStatus.COMPLETED
    checked = [e for e in engine.storage.list_events(task.id) if e.kind == "permission.checked"]
    assert checked[0].detail["scope"] == "filesystem:read"


def test_adversarial_missing_path_param_denied_by_policy(sandbox, db_path):
    policy = ResourcePolicy(path_constraints={("filesystem.read", "read"): ["public/"]})
    _, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="no path", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"filename": "public/a.txt"},
                  verification=VerificationPolicy("non_empty"))],
        policy=policy,
    )
    assert task.status == TaskStatus.FAILED


def test_adversarial_capability_swap_rejected(sandbox, db_path):
    """A plan naming a capability that is not registered cannot run anything."""
    _, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="shell", capability="shell.exec", action="exec",
                  scope="shell:exec", params={"cmd": "cat /etc/passwd"},
                  verification=VerificationPolicy("non_empty"))],
    )
    assert task.status == TaskStatus.FAILED
    assert "capability not found" in (task.error or "")


def test_audit_events_include_decision_details(sandbox, db_path):
    engine, task = _run_steps(
        sandbox, db_path,
        [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"path": "README.md"},
                  verification=VerificationPolicy("non_empty"))],
    )
    checked = [e for e in engine.storage.list_events(task.id) if e.kind == "permission.checked"]
    assert checked
    detail = checked[0].detail
    assert detail["outcome"] == "allow"
    assert detail["risk"] == "low"
    assert detail["side_effects"] == "read_only"
    assert detail["resource"] == "README.md"
