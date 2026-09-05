"""ADR-061 C1: ActionSpec resource-role declaration + compatibility sugar.

Covers ADR-061 D1, D2 (validation), D9 and invariants 1, 21 (and 16 by
demonstrating the singular spelling is unchanged).

C1 is INERT: nothing consumes `resources` yet. These tests pin the declaration
contract only.
"""

import pytest

from arion.capabilities.registry import (
    ActionSpec,
    ResourceDeclarationError,
    ResourceRole,
)


# --------------------------------------------------------------------------
# invariant 21 / D9 - the two spellings are mutually exclusive
# --------------------------------------------------------------------------

def test_declaring_both_spellings_is_rejected():
    """A precedence rule would allow lock target != approval target (D1)."""
    with pytest.raises(ResourceDeclarationError) as exc:
        ActionSpec(
            name="move", description="d", required_scope="filesystem:write",
            resource_kind="filesystem:path", resource_param="source",
            resources=[ResourceRole("source", "filesystem:path"),
                       ResourceRole("dest", "filesystem:path")],
        )
    assert "mutually exclusive" in str(exc.value)


def test_half_declared_singular_resource_is_rejected():
    """A half-declared resource is ambiguous, not a default."""
    with pytest.raises(ResourceDeclarationError):
        ActionSpec(name="read", description="d", required_scope="s",
                   resource_kind="filesystem:path")
    with pytest.raises(ResourceDeclarationError):
        ActionSpec(name="read", description="d", required_scope="s",
                   resource_param="path")


# --------------------------------------------------------------------------
# D9 - sugar normalizes into exactly one runtime representation
# --------------------------------------------------------------------------

def test_singular_spelling_normalizes_to_one_element_declaration():
    spec = ActionSpec(
        name="read", description="d", required_scope="filesystem:read",
        resource_kind="filesystem:path", resource_param="path",
    )
    assert spec.resources == [ResourceRole("path", "filesystem:path")]
    # singular fields still readable and unchanged (invariant 16)
    assert spec.resource_kind == "filesystem:path"
    assert spec.resource_param == "path"


def test_multi_resource_mirrors_primary_role_into_singular_fields():
    """Existing single-resource readers keep working (invariant 16)."""
    spec = ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )
    assert spec.resource_param == "source"
    assert spec.resource_kind == "filesystem:path"


def test_no_resource_declaration_stays_empty():
    spec = ActionSpec(name="now", description="d", required_scope="clock:read")
    assert spec.resources == []
    assert spec.resource_kind is None
    assert spec.resource_param is None


# --------------------------------------------------------------------------
# D1 - declaration order is preserved (role identity is ordered)
# --------------------------------------------------------------------------

def test_declaration_order_is_preserved():
    spec = ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )
    assert [r.role for r in spec.resources] == ["source", "dest"]


def test_role_is_the_params_key():
    """One naming axis only: role IS the param key (D1)."""
    role = ResourceRole("dest", "filesystem:path")
    assert role.param == "dest"


# --------------------------------------------------------------------------
# D2 - ambiguous declarations are rejected at construction (fail closed)
# --------------------------------------------------------------------------

def test_duplicate_role_name_is_rejected():
    with pytest.raises(ResourceDeclarationError) as exc:
        ActionSpec(
            name="weird", description="d", required_scope="s",
            resources=[ResourceRole("path", "filesystem:path"),
                       ResourceRole("path", "filesystem:path")],
        )
    assert "duplicate" in str(exc.value)


def test_role_absent_from_param_schema_is_rejected():
    with pytest.raises(ResourceDeclarationError) as exc:
        ActionSpec(
            name="move", description="d", required_scope="s",
            param_schema={"source": {"type": "string", "required": True}},
            resources=[ResourceRole("source", "filesystem:path"),
                       ResourceRole("dest", "filesystem:path")],
        )
    assert "param_schema" in str(exc.value)


def test_role_present_in_param_schema_is_accepted():
    spec = ActionSpec(
        name="move", description="d", required_scope="s",
        param_schema={"source": {"type": "string", "required": True},
                      "dest": {"type": "string", "required": True}},
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )
    assert len(spec.resources) == 2


def test_empty_role_or_kind_is_rejected():
    with pytest.raises(ResourceDeclarationError):
        ActionSpec(name="x", description="d", required_scope="s",
                   resources=[ResourceRole("", "filesystem:path")])
    with pytest.raises(ResourceDeclarationError):
        ActionSpec(name="x", description="d", required_scope="s",
                   resources=[ResourceRole("path", "")])


def test_non_resourcerole_entry_is_rejected():
    with pytest.raises(ResourceDeclarationError):
        ActionSpec(name="x", description="d", required_scope="s",
                   resources=[("path", "filesystem:path")])  # type: ignore[list-item]


# --------------------------------------------------------------------------
# D9 - serialization is additive
# --------------------------------------------------------------------------

def test_to_dict_is_additive_and_keeps_singular_keys():
    spec = ActionSpec(
        name="read", description="d", required_scope="filesystem:read",
        resource_kind="filesystem:path", resource_param="path",
    )
    d = spec.to_dict()
    # every historical key still present and unchanged
    assert d["resource_kind"] == "filesystem:path"
    assert d["resource_param"] == "path"
    # new key is additive
    assert d["resources"] == [{"role": "path", "kind": "filesystem:path"}]


def test_to_dict_multi_resource_shape():
    spec = ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        resources=[ResourceRole("source", "filesystem:path"),
                   ResourceRole("dest", "filesystem:path")],
    )
    assert spec.to_dict()["resources"] == [
        {"role": "source", "kind": "filesystem:path"},
        {"role": "dest", "kind": "filesystem:path"},
    ]


# --------------------------------------------------------------------------
# invariant 16 - shipped capabilities are unaffected
# --------------------------------------------------------------------------

def test_shipped_capabilities_normalize_without_error():
    """Every registered action must still construct and expose one role."""
    from arion.capabilities.append import FilesystemAppendCapability
    from arion.capabilities.filesystem import FilesystemReadCapability
    from arion.capabilities.git import GitLogCapability
    from arion.capabilities.http import HttpGetCapability
    from arion.capabilities.write import FilesystemWriteCapability

    caps = [
        FilesystemReadCapability("."),
        FilesystemWriteCapability("."),
        FilesystemAppendCapability("."),
        GitLogCapability("."),
        HttpGetCapability(),
    ]
    for cap in caps:
        for action in cap.actions:
            if action.resource_kind is None:
                assert action.resources == []
            else:
                assert len(action.resources) == 1
                assert action.resources[0].kind == action.resource_kind
                assert action.resources[0].param == action.resource_param
