"""ADR-062 D2 — registration implies transit.

`tests/test_search.py` (M9-B.1, 8 tests) exercises `FilesystemSearchCapability`
by calling `execute()` directly and hand-constructing its `AuthorizationRequest`.
Every one of those tests passed while the capability was incapable of crossing
the authority boundary at all: its action was a raw dict, so `action_spec()`
raised and a planned `search` step crashed the engine.

That is a test-SHAPE gap, not a test-count gap, and more direct-execution tests
cannot close it. The property that actually matters is:

    for every capability the default bootstrap registers, a step targeting it
    can be driven plan -> validate -> authorize -> execute -> verify through the
    REAL engine, reaching a DURABLE terminal status — and never raising out of
    the engine.

Allowed actions must complete. Fail-closed actions must be denied durably with an
explainable reason. Neither may crash.
"""

from __future__ import annotations

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.registry import ActionSpec
from arion.orchestration.authz import (
    ApprovalOutcome,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.state.models import (
    TASK_TERMINAL_STATUSES,
    PlanStep,
    StepStatus,
    TaskStatus,
    VerificationPolicy,
)

FS = "filesystem:path"


class OneStepPlanner:
    """Emits exactly one caller-supplied step (the deterministic planner can
    only reach 5 of the 8 registered actions, so transit of the rest has to be
    driven by an explicit plan — the same route a model-produced plan takes)."""

    def __init__(self, capability: str, action: str, params: dict, scope: str,
                 verification: VerificationPolicy):
        self._step = dict(capability=capability, action=action, params=params,
                          scope=scope, verification=verification)

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(index=0, intent=f"{self._step['action']} step",
                         **self._step)]

    def required_capabilities(self, goal_description):
        return {self._step["capability"]}


def _sandbox(tmp_path, with_git: bool = False):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    (sb / "README.md").write_text("# repo\n", encoding="utf-8")
    (sb / "notes.txt").write_text("hello\n", encoding="utf-8")
    if with_git:
        git = sb / ".git"
        (git / "logs").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "logs" / "HEAD").write_text(
            "0" * 40 + " " + "a" * 40 + " Tester <t@example.com> 1700000000 +0000"
            "\tcommit (initial): first\n",
            encoding="utf-8",
        )
    return sb


def _permissive_policy() -> ResourcePolicy:
    """Everything the shipped vocabulary needs to be ALLOWED, so transit failures
    can only come from the boundary being unable to govern the action."""
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write", "git:read"},
        risk_deny=set(),
        risk_approve=set(),
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(tmp_path, capability, action, params, scope, verification,
            policy=None, with_git=False):
    sandbox = _sandbox(tmp_path, with_git=with_git)
    planner = OneStepPlanner(capability, action, params, scope, verification)
    engine = build_engine(
        db_path=tmp_path / "arion.db",
        sandbox_root=sandbox,
        policy=policy if policy is not None else _permissive_policy(),
        planner=planner,
    )
    return engine, sandbox


# --------------------------------------------------------------------------
# Allowed actions transit end to end
# --------------------------------------------------------------------------

TRANSIT = [
    pytest.param(
        "filesystem.read", "read", {"path": "README.md"}, "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["content"]}), False,
        id="read",
    ),
    pytest.param(
        "filesystem.read", "list", {"path": "."}, "filesystem:read",
        VerificationPolicy("non_empty"), False,
        id="list",
    ),
    # The M9-B.1 regression: search must transit, not merely execute.
    pytest.param(
        "filesystem.search", "search",
        {"pattern": "*.md", "directory": ".", "max_results": 10},
        "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["results", "count"]}), False,
        id="search",
    ),
    pytest.param(
        "filesystem.write", "write", {"path": "out/new.txt", "content": "hello"},
        "filesystem:write", VerificationPolicy("write_verified"), False,
        id="write",
    ),
    pytest.param(
        "filesystem.append", "append",
        {"path": "notes.txt", "content": "more\n", "create": False},
        "filesystem:write", VerificationPolicy("append_verified"), False,
        id="append",
    ),
    pytest.param(
        "git.log", "log", {"repo": "."}, "git:read",
        VerificationPolicy("schema_keys", {"keys": ["commits"]}), True,
        id="git-log",
    ),
    pytest.param(
        "git.log", "branches", {"repo": "."}, "git:read",
        VerificationPolicy("schema_keys", {"keys": ["branches"]}), True,
        id="git-branches",
    ),
]


@pytest.mark.parametrize(
    "capability,action,params,scope,verification,with_git", TRANSIT)
def test_allowed_action_transits_the_boundary(
        tmp_path, capability, action, params, scope, verification, with_git):
    engine, sandbox = _engine(tmp_path, capability, action, params, scope,
                              verification, with_git=with_git)
    try:
        # MUST NOT raise: an ungovernable action previously escaped as an
        # unhandled AttributeError out of the planning path.
        task = engine.execute_goal(f"transit {capability}.{action}")
        assert task.status == TaskStatus.COMPLETED, (
            f"{capability}.{action} did not transit: "
            f"{task.status} / {task.error} / "
            f"{task.steps[0].error if task.steps else 'no steps'}"
        )
        assert task.steps[0].status == StepStatus.SUCCEEDED
        assert task.steps[0].result is not None
    finally:
        engine.storage.close()


def test_search_transit_returns_real_results(tmp_path):
    """Transit is not merely 'did not crash' — the observation must come back
    through the engine, verified."""
    engine, sandbox = _engine(
        tmp_path, "filesystem.search", "search",
        {"pattern": "*.md", "directory": ".", "max_results": 10},
        "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["results", "count"]}),
    )
    try:
        task = engine.execute_goal("find markdown")
        assert task.status == TaskStatus.COMPLETED
        result = task.steps[0].result
        assert result["count"] == 1
        assert result["results"][0]["path"] == "README.md"
        assert "verification.passed" in [e.kind for e in engine.storage.list_events()]
    finally:
        engine.storage.close()


def test_write_transit_acquires_and_releases_a_mutation_lock(tmp_path):
    """A mutating action's transit must include the coordination window."""
    engine, sandbox = _engine(
        tmp_path, "filesystem.write", "write",
        {"path": "out/new.txt", "content": "hello"},
        "filesystem:write", VerificationPolicy("write_verified"),
    )
    try:
        task = engine.execute_goal("write a file")
        assert task.status == TaskStatus.COMPLETED
        kinds = [e.kind for e in engine.storage.list_events()]
        assert "mutation.lock.requested" in kinds
        assert "mutation.lock.acquired" in kinds
        assert "mutation.lock.released" in kinds
        assert (sandbox / "out" / "new.txt").read_text(encoding="utf-8") == "hello"
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# Fail-closed actions transit durably — denied, never crashed
# --------------------------------------------------------------------------


def test_http_get_without_a_url_boundary_is_denied_durably(tmp_path):
    """http.get is registered by default but the default bootstrap configures no
    'url' boundary, so it must be DENIED (ADR-009 step 4, fail closed) — and the
    denial must be a durable task failure, not an exception."""
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:read", "http:get"},   # scope granted...
        risk_deny=set(),
        risk_approve=set(),
        boundaries={FS: RelativePathBoundary()},          # ...but NO url boundary
    )
    engine, _ = _engine(
        tmp_path, "http.get", "get", {"url": "https://example.invalid/x"},
        "http:get",
        VerificationPolicy("schema_keys", {"keys": ["status", "body"]}),
        policy=policy,
    )
    try:
        task = engine.execute_goal("fetch a url")
        assert task.status == TaskStatus.FAILED
        assert task.status in TASK_TERMINAL_STATUSES
        joined = " ".join(filter(None, [task.error, task.steps[0].error]))
        assert "no resource boundary configured" in joined
        assert "'url'" in joined
        assert "permission.denied" in [e.kind for e in engine.storage.list_events()]
    finally:
        engine.storage.close()


def test_write_denied_by_the_default_bootstrap_policy(tmp_path):
    """Under the DEFAULT policy (no filesystem:write scope, high risk denied)
    a write step must be refused durably — the default engine stays read-only."""
    default = ResourcePolicy(
        allowed_scopes={"filesystem:read", "git:read"},
        boundaries={FS: RelativePathBoundary()},
    )
    engine, sandbox = _engine(
        tmp_path, "filesystem.write", "write",
        {"path": "out/new.txt", "content": "hello"},
        "filesystem:write", VerificationPolicy("write_verified"), policy=default,
    )
    try:
        task = engine.execute_goal("write a file")
        assert task.status == TaskStatus.FAILED
        assert not (sandbox / "out" / "new.txt").exists()
        assert "mutation.lock.acquired" not in [
            e.kind for e in engine.storage.list_events()]
    finally:
        engine.storage.close()


def test_search_outside_the_boundary_is_denied_durably(tmp_path):
    engine, _ = _engine(
        tmp_path, "filesystem.search", "search",
        {"pattern": "*", "directory": "../../etc", "max_results": 10},
        "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["results", "count"]}),
    )
    try:
        task = engine.execute_goal("search outside")
        assert task.status == TaskStatus.FAILED
        assert task.status in TASK_TERMINAL_STATUSES
        joined = " ".join(filter(None, [task.error, task.steps[0].error]))
        assert "outside boundary" in joined
        events = [e.kind for e in engine.storage.list_events()]
        assert "permission.denied" in events
        # denied at authorization: the capability is never reached
        assert "capability.executed" not in events
    finally:
        engine.storage.close()


def test_search_requiring_approval_blocks_durably(tmp_path):
    """Transit includes the approval seam: a high-risk read must reach
    REQUIRE_APPROVAL and stop, durably, without executing."""
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:read"},
        risk_deny=set(),
        risk_approve={"low"},          # force approval even for low risk
        boundaries={FS: RelativePathBoundary()},
    )
    engine, _ = _engine(
        tmp_path, "filesystem.search", "search",
        {"pattern": "*.md", "directory": ".", "max_results": 10},
        "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["results", "count"]}),
        policy=policy,
    )
    try:
        goal = engine.submit_goal("search with approval")
        engine.run_goal(goal.id)                 # must not raise
        requests = engine.approval_store.list_requests()
        assert len(requests) == 1
        engine.resolve_approval_request(requests[0].approval_id,
                                        ApprovalOutcome.APPROVED)
        engine.run_goal(goal.id)                 # must not raise
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status == TaskStatus.COMPLETED
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# The goal-manager path (the one that crashed in the M9-B.1 probe)
# --------------------------------------------------------------------------


def test_run_goal_path_does_not_raise_for_any_registered_action(tmp_path):
    """`run_goal` is what the CLI drives. Under M9-B.1 an unhandled
    AttributeError escaped it for a search step; it must return a durable goal."""
    sandbox = _sandbox(tmp_path, with_git=True)
    planner = OneStepPlanner(
        "filesystem.search", "search",
        {"pattern": "*.md", "directory": ".", "max_results": 10},
        "filesystem:read",
        VerificationPolicy("schema_keys", {"keys": ["results", "count"]}),
    )
    engine = build_engine(db_path=tmp_path / "arion.db", sandbox_root=sandbox,
                          policy=_permissive_policy(), planner=planner)
    try:
        goal = engine.submit_goal("search the repo")
        result = engine.run_goal(goal.id)        # MUST NOT raise
        assert result.status.value in {
            "completed", "failed", "blocked", "paused"}
        task = engine.goal_manager.task_history(goal.id)[-1]
        assert task.status in TASK_TERMINAL_STATUSES
        assert task.status == TaskStatus.COMPLETED
    finally:
        engine.storage.close()


# --------------------------------------------------------------------------
# Every registered action is readable by every consumer of the registry
# --------------------------------------------------------------------------


def test_every_registered_action_resolves_through_the_registry(tmp_path):
    """`action_spec` (authorization, plan validation, audit projection) and
    `capabilities_summary` (model catalog, CLI) must both resolve for every
    action of every capability the default bootstrap registers."""
    sandbox = _sandbox(tmp_path)
    engine = build_engine(db_path=tmp_path / "arion.db", sandbox_root=sandbox)
    try:
        registry = engine.registry
        summary = registry.capabilities_summary()
        assert summary, "the default bootstrap registers no capabilities"
        seen = 0
        for cap in summary:
            for action in cap["actions"]:
                spec = registry.action_spec(cap["name"], action["name"])
                assert isinstance(spec, ActionSpec), (
                    f"{cap['name']}.{action['name']} is not resolvable to an "
                    f"ActionSpec"
                )
                assert spec.required_scope
                seen += 1
        assert seen >= 8, f"expected the full shipped vocabulary, saw {seen}"
    finally:
        engine.storage.close()
