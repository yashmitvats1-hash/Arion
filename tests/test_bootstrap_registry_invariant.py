"""ADR-062 D3 — the default bootstrap produces a self-consistent registry.

`build_engine()` registers every shipped capability unconditionally, so ONE
unreadable declaration took the whole catalog down: `capabilities_summary()`
raised for every capability, which killed the model-planning catalog
(`model_planner.py:112`) and `arion capabilities` (`cli.py:398`) alike. Under
M9-B.1 that meant the M9-A model path was dead in any default-built engine.

These are the cheap, permanent invariants that catch that class at merge time
rather than in a downstream milestone's test run.
"""

from __future__ import annotations

import json

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.registry import (
    KNOWN_VERIFICATION_POLICIES,
    ActionSpec,
)
from arion.intelligence.model_planner import RealModelPlanner
from arion.intelligence.plan_schema import (
    PLAN_SCHEMA_VERSION,
    PlanSchema,
    StructuredStep,
)

EXPECTED_CAPABILITIES = {
    "filesystem.read",
    "filesystem.write",
    "filesystem.append",
    "filesystem.search",
    "git.log",
    "http.get",
}

EXPECTED_ACTIONS = {
    ("filesystem.read", "read"),
    ("filesystem.read", "list"),
    ("filesystem.write", "write"),
    ("filesystem.append", "append"),
    ("filesystem.search", "search"),
    ("git.log", "log"),
    ("git.log", "branches"),
    ("http.get", "get"),
}


@pytest.fixture
def bootstrapped(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# repo\n", encoding="utf-8")
    engine = build_engine(db_path=tmp_path / "arion.db", sandbox_root=sandbox)
    try:
        yield engine, sandbox
    finally:
        engine.storage.close()


def test_default_bootstrap_registers_the_full_vocabulary(bootstrapped):
    engine, _ = bootstrapped
    assert set(engine.registry.list()) == EXPECTED_CAPABILITIES


def test_capabilities_summary_succeeds_on_the_default_bootstrap(bootstrapped):
    """The exact call the model planner and the CLI make. Under M9-B.1 this
    raised AttributeError: 'dict' object has no attribute 'to_dict'."""
    engine, _ = bootstrapped
    summary = engine.registry.capabilities_summary()
    assert {c["name"] for c in summary} == EXPECTED_CAPABILITIES
    actions = {(c["name"], a["name"]) for c in summary for a in c["actions"]}
    assert actions == EXPECTED_ACTIONS


def test_capabilities_summary_is_json_serializable(bootstrapped):
    """The catalog crosses a wire to a provider adapter; it must be plain JSON
    data, not objects with attributes."""
    engine, _ = bootstrapped
    summary = engine.registry.capabilities_summary()
    encoded = json.dumps(summary)
    assert json.loads(encoded) == summary


def test_every_action_spec_resolves_on_the_default_bootstrap(bootstrapped):
    engine, _ = bootstrapped
    for capability, action in sorted(EXPECTED_ACTIONS):
        spec = engine.registry.action_spec(capability, action)
        assert isinstance(spec, ActionSpec), f"{capability}.{action} unresolved"
        assert spec.required_scope, f"{capability}.{action} has no scope"


def test_search_is_discoverable_in_the_model_catalog(bootstrapped):
    """M9-B.1's own acceptance criterion, actually measured through the surface
    a model sees."""
    engine, _ = bootstrapped
    summary = engine.registry.capabilities_summary()
    search = next(c for c in summary if c["name"] == "filesystem.search")
    action = search["actions"][0]
    assert action["name"] == "search"
    assert action["required_scope"] == "filesystem:read"
    assert action["resource_kind"] == "filesystem:path"
    assert action["resource_param"] == "directory"
    # ADR-061 D9: the derived multi-resource view is present for every action
    assert action["resources"] == [{"role": "directory", "kind": "filesystem:path"}]


class RecordingRouter:
    """Captures the catalog exactly as `RealModelPlanner` hands it over."""

    def __init__(self):
        self.catalog = None
        self.calls = 0

    def generate(self, prompt, **kwargs):
        return ""

    def plan_structured(self, goal, capabilities, context):
        self.calls += 1
        self.catalog = capabilities
        return PlanSchema(
            version=PLAN_SCHEMA_VERSION,
            intent=goal[:80],
            steps=[StructuredStep(intent="read the readme",
                                  capability="filesystem.read", action="read",
                                  params={"path": "README.md"})],
        )


def test_model_planner_receives_a_live_catalog_from_the_default_bootstrap(bootstrapped):
    """End-to-end proof that the M9-A path is alive: the planner reaches the
    router, the router receives the full catalog including search, and the
    validated plan comes back."""
    engine, _ = bootstrapped
    router = RecordingRouter()
    planner = RealModelPlanner(router, fallback_enabled=False)

    steps = planner.plan("read the readme", "task-1", engine.registry)

    assert router.calls == 1, "the model router was never consulted"
    assert {c["name"] for c in router.catalog} == EXPECTED_CAPABILITIES
    assert planner.last_source == "model"
    assert steps and steps[0].capability == "filesystem.read"
    assert steps[0].scope == "filesystem:read"  # registry authority, not the model


def test_model_planner_can_propose_a_search_step(bootstrapped):
    """The capability M9-B.1 added must be proposable by a model and survive
    validation against the live registry."""
    engine, _ = bootstrapped

    class SearchRouter(RecordingRouter):
        def plan_structured(self, goal, capabilities, context):
            self.calls += 1
            self.catalog = capabilities
            return PlanSchema(
                version=PLAN_SCHEMA_VERSION,
                intent=goal[:80],
                steps=[StructuredStep(intent="find markdown",
                                      capability="filesystem.search",
                                      action="search",
                                      params={"pattern": "*.md",
                                              "directory": "."})],
            )

    router = SearchRouter()
    planner = RealModelPlanner(router, fallback_enabled=False)
    steps = planner.plan("find markdown files", "task-2", engine.registry)

    assert steps[0].capability == "filesystem.search"
    assert steps[0].action == "search"
    assert steps[0].scope == "filesystem:read"
    # ADR-060 D4 is deliberately asymmetric: the registry's default_verification
    # OUTRANKS the plan only for a MUTATING action. `search` is read-only, so the
    # explicit policy the step carries is honoured (the StructuredStep default),
    # and the invariant that matters is that it is a policy this engine build can
    # actually evaluate — an unknown one fails closed in `_verify`.
    assert steps[0].verification.policy in KNOWN_VERIFICATION_POLICIES


def test_cli_capabilities_command_succeeds(tmp_path, capsys, monkeypatch):
    """`arion capabilities` iterates `capabilities_summary()`; under M9-B.1 it
    crashed with AttributeError."""
    from arion.interfaces.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# repo\n", encoding="utf-8")
    rc = main(["capabilities", "--db", str(tmp_path / "arion.db")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "filesystem.search" in out
    assert "filesystem.read" in out
