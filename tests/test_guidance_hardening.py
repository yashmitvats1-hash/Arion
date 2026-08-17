"""Guidance hardening tests (architecture directive).

- apply_guidance_to_steps is NON-MUTATING: original plan retained.
- Resources resolved through ActionSpec.resource_param (registry), NOT a
  hardcoded 'path'.
- Strategy-level action substitution (different action of same capability).
- Provenance on every transformation (decisions + step.guidance).
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.memory.guidance import (
    MemoryGuidance,
    PlanTransformation,
    apply_guidance_to_steps,
    registry_resource_param,
)
from arion.state.models import PlanStep, VerificationPolicy


def _step(index, capability="filesystem.read", action="read", params=None):
    return PlanStep(
        index=index, intent=f"s{index}", capability=capability, action=action,
        scope="filesystem:read", params=params or {"path": "README.md"},
        verification=VerificationPolicy("non_empty"),
    )


def _registry(sandbox):
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(sandbox))
    return reg


def _guidance(category="avoid", capability="filesystem.read", action="read",
              resource="README.md", episode="ep_1", reflection="refl_1", gid="g_1"):
    return MemoryGuidance(
        guidance_id=gid, category=category, capability=capability, action=action,
        resource=resource, episode_id=episode, reflection_id=reflection,
        confidence="high", importance=0.9,
    )


def test_apply_guidance_is_non_mutating(sandbox):
    reg = _registry(sandbox)
    steps = [_step(0), _step(1, action="list", params={"path": "."})]
    guidance = [_guidance(resource="README.md")]
    original_ids = [id(s) for s in steps]

    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )

    assert isinstance(tr, PlanTransformation)
    # original plan retained (deep copies), inputs NOT mutated
    assert len(tr.original) == 2
    assert tr.original[0].params == {"path": "README.md"}
    assert steps[0].params == {"path": "README.md"}  # untouched
    assert [id(s) for s in steps] == original_ids    # same objects
    assert tr.original[0] is not tr.transformed[0]


def test_apply_guidance_resource_substitution_via_registry(sandbox):
    """Resolver comes from the registry's ActionSpec.resource_param."""
    reg = _registry(sandbox)
    steps = [_step(0)]  # read README.md
    guidance = [
        _guidance(resource="README.md", gid="g_avoid"),
        _guidance(category="prefer", resource="notes.txt", gid="g_prefer"),
    ]
    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    assert tr.transformed[0].params == {"path": "notes.txt"}  # 'path' came from ActionSpec
    assert tr.decisions[0]["category"] == "resource_substitution"
    assert tr.decisions[0]["episode_id"] == "ep_1"
    assert tr.decisions[0]["guidance_id"] == "g_prefer"
    # per-step provenance attached
    assert tr.transformed[0].guidance and tr.transformed[0].guidance[0]["category"] == "resource_substitution"


def test_custom_capability_uses_its_own_resource_param(sandbox):
    """No 'path' assumption: a custom capability with resource_param='target'."""
    class CustomCap:
        name = "db.query"
        description = "query a database"
        actions = [
            ActionSpec(name="select", description="select", required_scope="db:read",
                       resource_kind="table", resource_param="target",
                       param_schema={"target": {"type": "string", "required": True}})
        ]

        def execute(self, action, params):
            return {"rows": []}

    reg = _registry(sandbox)
    reg.register(CustomCap())
    steps = [PlanStep(index=0, intent="q", capability="db.query", action="select",
                      scope="db:read", params={"target": "users"})]
    guidance = [
        _guidance(capability="db.query", action="select", resource="users", gid="ga"),
        _guidance(category="prefer", capability="db.query", action="select",
                  resource="public_users", gid="gp", episode="ep_2", reflection="refl_2"),
    ]
    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    # 'target' is the declared resource param - substituted through it
    assert tr.transformed[0].params == {"target": "public_users"}
    assert tr.original[0].params == {"target": "users"}
    assert tr.decisions[0]["new_resource"] == "public_users"


def test_action_substitution_strategy(sandbox):
    """avoided action read -> substituted with prefer action list (strategy-level)."""
    reg = _registry(sandbox)
    steps = [_step(0)]  # read README.md - avoid'ed, no prefer for read
    guidance = [
        _guidance(resource="README.md", gid="ga"),  # avoid read/README.md
        _guidance(category="prefer", action="list", resource="docs",
                  gid="gp", episode="ep_2", reflection="refl_2"),  # prefer list/docs
    ]
    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    assert tr.decisions[0]["category"] == "action_substitution"
    assert tr.decisions[0]["strategy"] == "alternative_action"
    assert tr.transformed[0].action == "list"
    assert tr.transformed[0].params == {"path": "docs"}
    assert tr.transformed[0].guidance[0]["new_action"] == "list"


def test_prefer_never_substitutes_onto_an_avoided_resource(sandbox):
    """Race hardening (ADR-016): a 'prefer' from an old episode must not
    re-target work onto a resource that has SINCE failed. Substitution falls
    through to an explicit SKIPPED step (never silently executed)."""
    reg = _registry(sandbox)
    steps = [_step(0)]  # read README.md
    guidance = [
        _guidance(category="avoid", resource="README.md", gid="g_avoid1", episode="ep_fail1"),
        _guidance(category="avoid", resource="docs/design.md", gid="g_avoid2", episode="ep_fail2"),
        _guidance(category="prefer", resource="docs/design.md", gid="g_pref", episode="ep_ok"),
    ]
    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    out = tr.transformed[0]
    assert out.params == {"path": "README.md"}  # NOT redirected to docs/design.md
    assert out.status.value == "skipped"        # explicitly skipped instead
    assert tr.decisions[0]["category"] == "step_skipped"
    # provenance names the avoided resource that blocked substitution
    assert any(d.get("category") == "step_skipped" and d.get("resource") == "README.md"
               for d in tr.decisions)


def test_action_substitution_refuses_avoided_preferred_resource(sandbox):
    """Same race guard for action substitution: an alternative action whose
    resource is also known-failing must not be chosen."""
    reg = _registry(sandbox)
    steps = [_step(0)]  # read README.md
    guidance = [
        _guidance(category="avoid", resource="README.md", gid="g_avoid1"),
        _guidance(category="avoid", action="list", resource="docs/design.md", gid="g_avoid2"),
        _guidance(category="prefer", action="list", resource="docs/design.md", gid="g_pref"),
    ]
    tr = apply_guidance_to_steps(
        steps, guidance,
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    out = tr.transformed[0]
    assert out.action == "read"             # NOT switched to list
    assert out.status.value == "skipped"    # explicitly skipped
    assert tr.decisions[0]["category"] == "step_skipped"


def test_apply_guidance_skips_step_when_no_safe_alternative(sandbox):
    """EXPLICIT skip: the avoided step is RETAINED with status SKIPPED and its
    provenance - never silently deleted (the engine always has a terminal
    task state)."""
    from arion.state.models import StepStatus

    reg = _registry(sandbox)
    steps = [_step(0)]
    tr = apply_guidance_to_steps(
        steps, [_guidance(resource="README.md")],
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    assert len(tr.transformed) == 1  # step retained, not dropped
    skipped = tr.transformed[0]
    assert skipped.status == StepStatus.SKIPPED
    assert skipped.skipped_reason and "no safe alternative" in skipped.skipped_reason
    assert skipped.guidance and skipped.guidance[0]["category"] == "step_skipped"
    assert skipped.guidance[0]["episode_id"] == "ep_1"
    assert tr.decisions[0]["category"] == "step_skipped"
    assert tr.decisions[0]["guidance_id"] == "g_1"


def test_apply_guidance_fails_closed_without_resolver(sandbox):
    """FAIL CLOSED: no registry-driven resolver -> refuse to transform (no
    filesystem 'path' assumption in the generic guidance layer)."""
    steps = [_step(0)]
    with pytest.raises(ValueError, match="fail closed"):
        apply_guidance_to_steps(steps, [_guidance(resource="README.md")])  # no resolver


def test_plan_transformation_retains_original_and_decisions(sandbox):
    reg = _registry(sandbox)
    steps = [_step(0), _step(1, action="list", params={"path": "."})]
    tr = apply_guidance_to_steps(
        steps, [_guidance(resource="README.md")],
        resource_param_resolver=lambda cap, act: registry_resource_param(reg, cap, act),
    )
    # original has 2 steps; transformed retains both (one now SKIPPED)
    assert len(tr.original) == 2 and len(tr.transformed) == 2
    assert tr.transformed[0].status.value == "skipped"
    assert tr.transformed[1].status.value == "pending"
    assert tr.decisions[0]["step_index"] == 0
    # provenance ids present in every decision
    for d in tr.decisions:
        assert d.get("guidance_id") and d.get("episode_id") and d.get("reflection_id") is not None
