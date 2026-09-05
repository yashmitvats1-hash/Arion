"""M7-A: registry-authoritative verification (ADR-060 D4/D5).

The audit found that Arion reported COMPLETED for a rename it never performed,
and that memory then learned "goal achievable" with HIGH confidence. M7-A is
the foundation that makes such a claim impossible for a mutating action: the
verification policy of a mutation is decided by the capability registry, never
by whoever proposed the plan, and a mutation with no usable policy is refused
before it executes.

These tests exercise the foundation against the FOUR EXISTING policies only.
`move_verified` and `filesystem.move` are M7-B and deliberately absent here.
"""

import pytest

from arion.capabilities.registry import (
    ActionSpec,
    CapabilityRegistry,
    HISTORICAL_DEFAULT_POLICY,
    KNOWN_VERIFICATION_POLICIES,
    VerificationResolutionError,
    is_mutating,
    resolve_verification_policy,
)
from arion.state.models import PlanStep, StepStatus, VerificationPolicy


def _spec(name="act", *, side_effects="read_only", default=None):
    return ActionSpec(
        name=name,
        description="test action",
        required_scope="test:scope",
        side_effects=side_effects,
        default_verification=default,
    )


# ---------------------------------------------------------------------------
# D5 case table: the four cases must not be collapsed
# ---------------------------------------------------------------------------


def test_explicit_known_policy_is_honoured_for_read_only():
    spec = _spec(default={"policy": "non_empty"})
    policy, args, authority = resolve_verification_policy(
        spec, VerificationPolicy("schema_keys", {"keys": ["content"]})
    )
    assert (policy, authority) == ("schema_keys", "explicit")
    assert args == {"keys": ["content"]}


def test_registry_default_applies_when_read_only_request_missing():
    spec = _spec(default={"policy": "schema_keys", "args": {"keys": ["x"]}})
    policy, args, authority = resolve_verification_policy(spec, None)
    assert (policy, authority) == ("schema_keys", "registry")
    assert args == {"keys": ["x"]}


def test_read_only_missing_everything_falls_back_to_history():
    """The historical default survives for reads - M7 fixes MUTATIONS only."""
    policy, args, authority = resolve_verification_policy(_spec(), None)
    assert policy == HISTORICAL_DEFAULT_POLICY
    assert authority == "historical_default"


def test_read_only_unknown_policy_is_left_for_verify_to_fail():
    """Unknown read-only policies stay put; `_verify`'s else-branch fails them."""
    policy, _, authority = resolve_verification_policy(
        _spec(), VerificationPolicy("nonsense_policy")
    )
    assert (policy, authority) == ("nonsense_policy", "explicit")


# ---------------------------------------------------------------------------
# D4: the registry OUTRANKS the proposer for mutations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proposed", ["non_empty", "schema_keys", None])
def test_mutation_verification_comes_from_registry_not_the_proposer(proposed):
    spec = _spec(side_effects="mutating", default={"policy": "write_verified"})
    requested = VerificationPolicy(proposed) if proposed else None
    policy, _, authority = resolve_verification_policy(spec, requested)
    assert policy == "write_verified"
    assert authority == "registry"


def test_irreversible_counts_as_mutating():
    assert is_mutating(_spec(side_effects="irreversible"))
    assert is_mutating(_spec(side_effects="mutating"))
    assert not is_mutating(_spec(side_effects="read_only"))


# ---------------------------------------------------------------------------
# D5: mutations fail closed, with a precise diagnostic
# ---------------------------------------------------------------------------


def test_mutation_without_any_verification_fails_closed():
    with pytest.raises(VerificationResolutionError) as exc:
        resolve_verification_policy(_spec(side_effects="mutating"), None)
    message = str(exc.value)
    assert "carries no verification policy" in message
    assert "default_verification" in message  # precise diagnostic


def test_mutation_with_unknown_policy_and_no_default_fails_closed():
    with pytest.raises(VerificationResolutionError) as exc:
        resolve_verification_policy(
            _spec(side_effects="mutating"), VerificationPolicy("made_up")
        )
    assert "unknown verification policy" in str(exc.value)
    assert "'made_up'" in str(exc.value)


def test_custom_mutating_capability_with_explicit_known_policy_still_runs():
    """A mutating capability declaring no default is NOT bricked.

    Guards the regression the reviewer flagged: refusing every mutation that
    lacks a registry default would make custom capabilities unexecutable even
    when their plan carries a perfectly valid explicit policy.
    """
    policy, _, authority = resolve_verification_policy(
        _spec(side_effects="mutating"), VerificationPolicy("write_verified")
    )
    assert (policy, authority) == ("write_verified", "explicit")


def test_known_policies_match_what_verify_implements():
    assert KNOWN_VERIFICATION_POLICIES == frozenset(
        {"non_empty", "schema_keys", "write_verified", "append_verified"}
    )


# ---------------------------------------------------------------------------
# D5 rehydration half: absent verification is distinguishable
# ---------------------------------------------------------------------------


def test_rehydrated_step_records_absent_verification():
    step = PlanStep.from_dict(
        {"index": 0, "capability": "c", "action": "a"}  # no verification key
    )
    assert step.verification.policy == "non_empty"  # historical default applied
    assert step.verification_absent is True  # ...but we still know it was absent


def test_rehydrated_step_with_explicit_policy_is_not_absent():
    step = PlanStep.from_dict({
        "index": 0, "capability": "c", "action": "a",
        "verification": {"policy": "non_empty", "args": {}},
    })
    assert step.verification_absent is False


def test_verification_absent_is_not_serialized():
    """No schema change: the marker is a property of one rehydration."""
    step = PlanStep.from_dict({"index": 0, "capability": "c", "action": "a"})
    assert "verification_absent" not in step.to_dict()


def test_round_trip_preserves_explicit_verification():
    original = PlanStep(
        index=0, intent="i", capability="c", action="a", scope="s",
        verification=VerificationPolicy("schema_keys", {"keys": ["content"]}),
    )
    revived = PlanStep.from_dict(original.to_dict())
    assert revived.verification.policy == "schema_keys"
    assert revived.verification.args == {"keys": ["content"]}
    assert revived.verification_absent is False


# ---------------------------------------------------------------------------
# Existing capabilities keep working unchanged
# ---------------------------------------------------------------------------


def test_all_registered_mutating_actions_declare_a_default():
    """Every shipped mutation must resolve without relying on the proposer."""
    from arion.capabilities.append import FilesystemAppendCapability
    from arion.capabilities.filesystem import FilesystemReadCapability
    from arion.capabilities.git import GitLogCapability
    from arion.capabilities.http import HttpGetCapability
    from arion.capabilities.write import FilesystemWriteCapability

    registry = CapabilityRegistry()
    for cap in (
        FilesystemReadCapability("."),
        FilesystemWriteCapability("."),
        FilesystemAppendCapability("."),
        GitLogCapability("."),
        HttpGetCapability(),
    ):
        registry.register(cap)

    for name in registry.list():
        for action in registry.get(name).actions:
            policy, _, authority = resolve_verification_policy(action, None)
            assert policy in KNOWN_VERIFICATION_POLICIES
            if is_mutating(action):
                assert authority == "registry", (
                    f"{name}.{action.name} is mutating but has no registry default"
                )
