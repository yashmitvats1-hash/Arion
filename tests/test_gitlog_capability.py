"""GitLogCapability tests (ADR-017): read-only git history inspection.

- declares complete ActionSpec metadata (scope, risk, resource semantics,
  param_schema, verification);
- discoverable through the registry / capability summary;
- reads git metadata directly (.git/logs/HEAD, refs, packed-refs) - NO shell;
- enforces the same sandbox containment as filesystem operations;
- works end-to-end through registry -> planning -> authorization -> execution
  -> verification.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    logs = git / "logs"
    logs.mkdir(parents=True)
    (logs / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Alice <a@x.io> 1700000000 +0000\tfirst commit\n"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb Bob <b@x.io> 1700000100 +0000\tsecond commit\n",
        encoding="utf-8",
    )
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n", encoding="utf-8")
    (refs / "feature").write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    return root


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(GitLogCapability(sandbox))
    planner = DeterministicPlanner()
    return (
        ArionEngine(
            storage=storage, registry=registry, planner=planner,
            router=DeterministicRouter(planner), events=EventLogger(sinks=[storage]),
            policy=ResourcePolicy(allowed_scopes={"filesystem:read", "git:read"},
                                  boundaries={FS: RelativePathBoundary()}),
        ),
        registry,
        storage,
    )


def test_discoverable_with_full_metadata(tmp_path, sandbox):
    _git_repo(sandbox)
    _, registry, storage = _engine(tmp_path / "d.db", sandbox)
    summary = {c["name"]: c for c in registry.capabilities_summary()}
    assert "git.log" in summary
    actions = {a["name"]: a for a in summary["git.log"]["actions"]}
    assert set(actions) == {"log", "branches"}
    for spec in actions.values():
        assert spec["required_scope"] == "git:read"
        assert spec["risk"] == "low" and spec["side_effects"] == "read_only"
        assert spec["resource_kind"] == FS and spec["resource_param"] == "repo"
        assert "repo" in spec["param_schema"]
        assert spec["default_verification"]
    storage.close()


def test_log_reads_reflog(tmp_path, sandbox):
    _git_repo(sandbox)
    cap = GitLogCapability(sandbox)
    out = cap.execute("log", {"repo": ".", "limit": 10})
    assert out["current_branch"] == "main"
    commits = out["commits"]
    assert len(commits) == 2
    assert commits[0]["sha"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert commits[0]["author"] == "Bob <b@x.io>"
    assert commits[0]["message"] == "second commit"
    assert commits[1]["message"] == "first commit"
    # limit respected
    out2 = cap.execute("log", {"repo": ".", "limit": 1})
    assert len(out2["commits"]) == 1


def test_branches_lists_refs_and_packed(tmp_path, sandbox):
    _git_repo(sandbox)
    git = sandbox / ".git"
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "cccccccccccccccccccccccccccccccccccccccc refs/heads/release\n",
        encoding="utf-8",
    )
    cap = GitLogCapability(sandbox)
    out = cap.execute("branches", {"repo": "."})
    names = {b["name"] for b in out["branches"]}
    assert names == {"main", "feature", "release"}
    by_name = {b["name"]: b for b in out["branches"]}
    assert by_name["main"]["sha"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_not_a_git_repo_fails(tmp_path, sandbox):
    cap = GitLogCapability(sandbox)  # sandbox has no .git
    with pytest.raises(CapabilityError, match="not a git repository"):
        cap.execute("log", {"repo": "."})


def test_path_escape_rejected_by_capability(tmp_path, sandbox):
    _git_repo(sandbox)
    cap = GitLogCapability(sandbox)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("log", {"repo": "../outside"})
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("branches", {"repo": "/etc"})


def test_path_escape_rejected_by_policy(tmp_path, sandbox):
    _git_repo(sandbox)
    engine, registry, storage = _engine(tmp_path / "p.db", sandbox)
    from arion.orchestration.authz import AuthorizationRequest

    req = AuthorizationRequest(
        actor=engine.actor, task_id="t", step_index=0,
        capability="git.log", action="log", scope="git:read",
        params={"repo": "../outside"}, resource="../outside",
        resource_kind=FS, risk="low", side_effects="read_only",
    )
    decision = engine.policy.decide(req)
    assert decision.outcome.value == "deny"
    assert "outside boundary" in decision.reason
    storage.close()


def test_git_goal_through_full_engine_loop(tmp_path, sandbox):
    _git_repo(sandbox)
    engine, registry, storage = _engine(tmp_path / "e.db", sandbox)
    # wired with a goal manager so the durable loop drives it
    from arion.cognition.goals import GoalManager
    from arion.cognition.progress import DeterministicProgressEvaluator
    from arion.cognition.store import SQLiteCognitiveStore
    from arion.cognition.strategy import StrategySelector
    from arion.cognition.world_state import WorldStateMonitor

    cognitive = SQLiteCognitiveStore(tmp_path / "e.db")
    events = EventLogger(sinks=[storage])
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine.goal_manager = gm
    engine.world_monitor = world_monitor

    goal = engine.submit_goal("inspect git history of this repository")
    final = engine.run_goal(goal.id)
    assert final.status == GoalStatus.COMPLETED
    tasks = gm.task_history(goal.id)
    assert tasks[-1].status == TaskStatus.COMPLETED
    actions = [s.action for s in tasks[-1].steps]
    assert "log" in actions and "branches" in actions
    events = storage.list_events()
    kinds = [e.kind for e in events]
    assert "capability.discovered" in kinds
    assert "permission.checked" in kinds
    assert "capability.executed" in kinds
    assert "verification.passed" in kinds
    # observations are structured (metadata only)
    assert any(e.kind == "observation.recorded" for e in events)
    engine.storage.close()


def test_param_schema_rejects_missing_repo(tmp_path, sandbox):
    """The declared param_schema is enforced by the plan validator."""
    _git_repo(sandbox)
    engine, registry, storage = _engine(tmp_path / "v.db", sandbox)
    from arion.intelligence.plan_schema import PlanSchema, StructuredStep
    from arion.intelligence.plan_validator import PlanValidator
    from arion.intelligence.errors import PlanCapabilityValidationError

    schema = PlanSchema(
        version="1.0",
        intent="inspect git history",
        steps=[StructuredStep(
            intent="log", capability="git.log", action="log",
            params={},  # missing required 'repo'
            verification={"policy": "schema_keys", "args": {"keys": ["commits"]}},
        )],
    )
    with pytest.raises(PlanCapabilityValidationError):
        PlanValidator(registry).validate(schema)
    storage.close()
