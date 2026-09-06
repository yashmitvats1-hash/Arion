"""ADR-061 C2: derived resource views (role-preserving + canonical).

Covers D1, D2 and invariants 1, 2, 3, 4, 13.

C2 is a DERIVATION layer: these tests assert the views are correct and that
the single-resource path is byte-identical to the historical behaviour. No
authorization/approval/locking consumer is rewired yet.
"""

from arion.capabilities.registry import ActionSpec, ResourceRole
from arion.orchestration.engine import ArionEngine
from arion.orchestration.resource_set import (
    ResolvedResource,
    canonical_identities,
    primary_resource,
    resolve_resources,
    unresolved_roles,
)


def _move_spec():
    return ActionSpec(
        name="move", description="d", required_scope="filesystem:write",
        side_effects="mutating",
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


# --------------------------------------------------------------------------
# D1 / invariant 4 - role view preserves order and duplicates
# --------------------------------------------------------------------------

def test_role_view_preserves_declaration_order():
    r = resolve_resources(_move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"})
    assert [x.role for x in r] == ["source", "dest"]
    assert [x.value for x in r] == ["README.md", "ARCHIVE.md"]


def test_role_view_retains_duplicate_values_as_distinct_roles():
    """move a -> a is TWO roles but ONE canonical resource (invariant 4)."""
    r = resolve_resources(_move_spec(), {"source": "a.txt", "dest": "a.txt"})
    assert [x.role for x in r] == ["source", "dest"]
    assert canonical_identities(r) == [("filesystem:path", "a.txt")]


# --------------------------------------------------------------------------
# invariant 20 - canonicalization must not alter the as-declared value
# --------------------------------------------------------------------------

def test_value_is_as_declared_while_canonical_is_normalized():
    r = resolve_resources(_move_spec(), {"source": "./README.md", "dest": "d/../A.md"})
    assert r[0].value == "./README.md"      # what a human will be shown
    assert r[0].canonical == "README.md"    # what will be locked/fingerprinted
    assert r[1].value == "d/../A.md"
    assert r[1].canonical == "A.md"


def test_differently_spelled_paths_share_one_canonical_identity():
    r = resolve_resources(_move_spec(), {"source": "./a.txt", "dest": "a.txt"})
    assert canonical_identities(r) == [("filesystem:path", "a.txt")]


# --------------------------------------------------------------------------
# invariants 2, 3, 13 - canonical view: pairs, deterministic, deduped
# --------------------------------------------------------------------------

def test_canonical_identity_is_a_kind_resource_pair():
    r = resolve_resources(_move_spec(), {"source": "b.txt", "dest": "a.txt"})
    for ident in canonical_identities(r):
        assert isinstance(ident, tuple) and len(ident) == 2
        assert ident[0] == "filesystem:path"


def test_canonical_view_is_order_independent():
    """Same set declared in either order yields the same canonical view."""
    a = canonical_identities(
        resolve_resources(_move_spec(), {"source": "a.txt", "dest": "b.txt"}))
    b = canonical_identities(
        resolve_resources(_move_spec(), {"source": "b.txt", "dest": "a.txt"}))
    assert a == b == [("filesystem:path", "a.txt"), ("filesystem:path", "b.txt")]


def test_canonical_view_is_deterministically_sorted():
    r = resolve_resources(_move_spec(), {"source": "z.txt", "dest": "a.txt"})
    ids = canonical_identities(r)
    assert ids == sorted(ids)


def test_cross_kind_same_string_does_not_collide():
    """Bare-string identity would collide; the pair must not (R6)."""
    spec = ActionSpec(
        name="fetch", description="d", required_scope="net:read",
        param_schema={"src": {"type": "string", "required": True},
                      "out": {"type": "string", "required": True}},
        resources=[ResourceRole("src", "url"),
                   ResourceRole("out", "filesystem:path")],
    )
    ids = canonical_identities(resolve_resources(spec, {"src": "x", "out": "x"}))
    assert len(ids) == 2
    assert set(ids) == {("url", "x"), ("filesystem:path", "x")}


# --------------------------------------------------------------------------
# fail-closed inputs: unresolved roles are reported, never silently dropped
# --------------------------------------------------------------------------

def test_missing_param_is_reported_as_unresolved():
    r = resolve_resources(_move_spec(), {"source": "a.txt"})
    assert unresolved_roles(r) == ["dest"]
    assert canonical_identities(r) == [("filesystem:path", "a.txt")]


def test_non_string_and_empty_values_are_unresolved():
    r = resolve_resources(_move_spec(), {"source": 42, "dest": ""})
    assert unresolved_roles(r) == ["source", "dest"]
    assert canonical_identities(r) == []


def test_spec_without_resources_yields_empty_view():
    spec = ActionSpec(name="now", description="d", required_scope="clock:read")
    r = resolve_resources(spec, {})
    assert r == []
    assert canonical_identities(r) == []
    assert primary_resource(r) is None


# --------------------------------------------------------------------------
# invariant 16 - single-resource behaviour is byte-identical
# --------------------------------------------------------------------------

def test_single_resource_derivation_matches_historical_extraction():
    spec = _read_spec()
    params = {"path": "notes.md"}
    assert primary_resource(resolve_resources(spec, params)) == "notes.md"
    # the engine's public extractor now routes through the same derivation
    assert ArionEngine._extract_resource(spec, params) == "notes.md"


def test_extract_resource_returns_primary_role_for_multi_resource():
    assert ArionEngine._extract_resource(
        _move_spec(), {"source": "README.md", "dest": "ARCHIVE.md"}
    ) == "README.md"


def test_extract_resource_handles_missing_and_non_string_as_before():
    spec = _read_spec()
    assert ArionEngine._extract_resource(spec, {}) is None
    assert ArionEngine._extract_resource(spec, {"path": 7}) is None


def test_extract_resource_none_for_resourceless_spec():
    spec = ActionSpec(name="now", description="d", required_scope="clock:read")
    assert ArionEngine._extract_resource(spec, {}) is None


def test_resolved_resource_identity_and_resolved_flags():
    r = ResolvedResource("source", "filesystem:path", "a.txt", "a.txt")
    assert r.resolved is True
    assert r.identity == ("filesystem:path", "a.txt")
    u = ResolvedResource("dest", "filesystem:path", None, None)
    assert u.resolved is False
    assert u.identity is None
