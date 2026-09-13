"""ADR-062 D1 — the capability declaration contract is enforced at registration.

M9-B.1 shipped `filesystem.search` with `actions` declared as raw dicts rather
than `ActionSpec` objects. The registry accepted it (the `Capability` protocol is
structural, so nothing checked), and the fault surfaced three layers away as an
unhandled `AttributeError`:

  * `capabilities_summary()` — the catalog handed to `RealModelPlanner` and
    printed by `arion capabilities` — raised for EVERY capability, because
    `build_engine` registers search unconditionally. The whole M9-A model path
    was dead in any default-built engine.
  * `action_spec()` raised, so a step targeting `filesystem.search` could never
    be authorized or executed; on the deterministic planner path the exception
    escaped `run_goal()` entirely.

These tests pin the refusal at the boundary instead: a capability the registry
cannot READ is never admitted, and the registry is never left half-populated by
a refused registration.
"""

from __future__ import annotations

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.http import HttpGetCapability
from arion.capabilities.registry import (
    ActionSpec,
    CapabilityDeclarationError,
    CapabilityRegistry,
    ResourceDeclarationError,
)
from arion.capabilities.search import FilesystemSearchCapability
from arion.capabilities.write import FilesystemWriteCapability


def _spec(name: str = "act", scope: str = "demo:read") -> ActionSpec:
    return ActionSpec(name=name, description="d", required_scope=scope)


class _ValidCapability:
    """Positive control: the smallest capability the contract accepts."""

    name = "demo.valid"
    description = "A minimal, well-declared capability."
    actions = [_spec()]

    def execute(self, action, params):
        return {"ok": True}


class _DictActionCapability:
    """The M9-B.1 shape: a raw mapping where an ActionSpec belongs."""

    name = "demo.dict"
    description = "Declares its action as a dict."
    actions = [{"name": "act", "required_scope": "demo:read"}]

    def execute(self, action, params):
        return {"ok": True}


# --------------------------------------------------------------------------
# The refusal itself
# --------------------------------------------------------------------------


def test_dict_declared_action_is_refused_at_registration():
    reg = CapabilityRegistry()
    with pytest.raises(CapabilityDeclarationError, match="must be an ActionSpec"):
        reg.register(_DictActionCapability())


def test_refusal_diagnostic_names_the_offending_capability_and_index():
    reg = CapabilityRegistry()
    with pytest.raises(CapabilityDeclarationError) as exc:
        reg.register(_DictActionCapability())
    message = str(exc.value)
    # actionable: which capability, which slot, what was found, what is required
    assert "demo.dict" in message
    assert "action #0" in message
    assert "dict" in message


def test_refused_capability_is_never_visible_to_the_registry():
    """A failed registration must not half-populate the boundary."""
    reg = CapabilityRegistry()
    with pytest.raises(CapabilityDeclarationError):
        reg.register(_DictActionCapability())
    assert reg.has("demo.dict") is False
    assert reg.get("demo.dict") is None
    assert reg.list() == []
    assert reg.action_spec("demo.dict", "act") is None
    assert reg.capabilities_summary() == []


def test_refused_registration_leaves_existing_capabilities_intact():
    reg = CapabilityRegistry()
    reg.register(_ValidCapability())
    with pytest.raises(CapabilityDeclarationError):
        reg.register(_DictActionCapability())
    assert reg.list() == ["demo.valid"]
    assert reg.capabilities_summary()[0]["name"] == "demo.valid"


def test_declaration_error_is_a_value_error_like_resource_declaration_error():
    """ADR-061 D2 and ADR-062 D1 are the same fault class: a malformed
    declaration, raised at construction, never a runtime capability failure."""
    assert issubclass(CapabilityDeclarationError, ValueError)
    assert issubclass(ResourceDeclarationError, ValueError)


# --------------------------------------------------------------------------
# Every clause of the contract fails closed
# --------------------------------------------------------------------------


def test_none_is_refused():
    with pytest.raises(CapabilityDeclarationError, match="None"):
        CapabilityRegistry().register(None)


@pytest.mark.parametrize("name", [None, "", "   ", 42])
def test_bad_name_is_refused(name):
    cap = type("C", (), {"name": name, "description": "d",
                         "actions": [_spec()], "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="non-empty string 'name'"):
        CapabilityRegistry().register(cap)


def test_missing_name_attribute_is_refused():
    cap = type("C", (), {"description": "d", "actions": [_spec()],
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="'name'"):
        CapabilityRegistry().register(cap)


@pytest.mark.parametrize("description", [None, "", "   ", 7])
def test_bad_description_is_refused(description):
    cap = type("C", (), {"name": "demo.x", "description": description,
                         "actions": [_spec()], "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="non-empty string 'description'"):
        CapabilityRegistry().register(cap)


@pytest.mark.parametrize("execute", [None, "not callable", 42])
def test_non_callable_execute_is_refused(execute):
    cap = type("C", (), {"name": "demo.x", "description": "d", "actions": [_spec()],
                         "execute": execute})()
    with pytest.raises(CapabilityDeclarationError, match="must implement execute"):
        CapabilityRegistry().register(cap)


def test_missing_execute_is_refused():
    cap = type("C", (), {"name": "demo.x", "description": "d", "actions": [_spec()]})()
    with pytest.raises(CapabilityDeclarationError, match="must implement execute"):
        CapabilityRegistry().register(cap)


@pytest.mark.parametrize("actions", [None, {}, "act", 42])
def test_actions_must_be_a_sequence(actions):
    cap = type("C", (), {"name": "demo.x", "description": "d", "actions": actions,
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="list of ActionSpec"):
        CapabilityRegistry().register(cap)


def test_empty_actions_is_refused():
    cap = type("C", (), {"name": "demo.x", "description": "d", "actions": [],
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="empty 'actions'"):
        CapabilityRegistry().register(cap)


def test_mixed_valid_and_dict_actions_is_refused():
    """One bad entry poisons the whole catalog: refuse, do not skip it."""
    cap = type("C", (), {"name": "demo.x", "description": "d",
                         "actions": [_spec("good"), {"name": "bad"}],
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="action #1"):
        CapabilityRegistry().register(cap)


def test_duplicate_action_names_are_refused():
    cap = type("C", (), {"name": "demo.x", "description": "d",
                         "actions": [_spec("same"), _spec("same")],
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="twice"):
        CapabilityRegistry().register(cap)


@pytest.mark.parametrize("scope", [None, "", "   ", 42])
def test_empty_required_scope_is_refused(scope):
    """The scope is the authorization source of truth; without one the action
    cannot be decided, so it must never be admitted (fail closed)."""
    cap = type("C", (), {"name": "demo.x", "description": "d",
                         "actions": [_spec("act", scope)],
                         "execute": lambda self, a, p: {}})()
    with pytest.raises(CapabilityDeclarationError, match="non-empty 'required_scope'"):
        CapabilityRegistry().register(cap)


def test_valid_capability_is_accepted():
    """Positive control: the contract must not refuse honest declarations."""
    reg = CapabilityRegistry()
    reg.register(_ValidCapability())
    assert reg.has("demo.valid")
    assert isinstance(reg.action_spec("demo.valid", "act"), ActionSpec)
    summary = reg.capabilities_summary()
    assert summary[0]["name"] == "demo.valid"


def test_tuple_of_actions_is_accepted():
    reg = CapabilityRegistry()
    cap = type("C", (), {"name": "demo.t", "description": "d",
                         "actions": (_spec("a"), _spec("b")),
                         "execute": lambda self, a, p: {}})()
    reg.register(cap)
    assert isinstance(reg.action_spec("demo.t", "b"), ActionSpec)


# --------------------------------------------------------------------------
# The shipped vocabulary satisfies the contract — the M9-B.1 regression gate
# --------------------------------------------------------------------------

SHIPPED = [
    ("filesystem.read", FilesystemReadCapability),
    ("filesystem.write", FilesystemWriteCapability),
    ("filesystem.append", FilesystemAppendCapability),
    ("filesystem.search", FilesystemSearchCapability),
    ("git.log", GitLogCapability),
]


@pytest.mark.parametrize("expected_name,factory", SHIPPED)
def test_shipped_sandboxed_capability_registers_and_is_readable(
        tmp_path, expected_name, factory):
    reg = CapabilityRegistry()
    reg.register(factory(tmp_path))
    cap = reg.get(expected_name)
    assert cap is not None
    assert cap.actions, f"{expected_name} declares no actions"
    for action in cap.actions:
        assert isinstance(action, ActionSpec), (
            f"{expected_name}.{getattr(action, 'name', '?')} is "
            f"{type(action).__name__}, not ActionSpec"
        )
        assert isinstance(reg.action_spec(expected_name, action.name), ActionSpec)


def test_shipped_unsandboxed_capability_registers_and_is_readable():
    reg = CapabilityRegistry()
    reg.register(HttpGetCapability())
    for action in reg.get("http.get").actions:
        assert isinstance(action, ActionSpec)
        assert isinstance(reg.action_spec("http.get", action.name), ActionSpec)


def test_full_shipped_catalog_survives_capabilities_summary(tmp_path):
    """The exact call `RealModelPlanner.plan()` and `arion capabilities` make,
    against the exact registry `build_engine` constructs. This is the assertion
    whose absence let M9-B.1 land."""
    reg = CapabilityRegistry()
    reg.register(FilesystemReadCapability(tmp_path))
    reg.register(FilesystemWriteCapability(tmp_path))
    reg.register(FilesystemAppendCapability(tmp_path))
    reg.register(FilesystemSearchCapability(tmp_path))
    reg.register(GitLogCapability(tmp_path))
    reg.register(HttpGetCapability())

    summary = reg.capabilities_summary()  # must not raise
    assert {c["name"] for c in summary} == {
        "filesystem.read", "filesystem.write", "filesystem.append",
        "filesystem.search", "git.log", "http.get",
    }
    actions = {a["name"] for c in summary for a in c["actions"]}
    assert "search" in actions


def test_search_action_spec_carries_the_m9b1_declaration(tmp_path):
    """Converting the dict to an ActionSpec must preserve the declared contract
    exactly — scope, resource role, bounds and verification authority."""
    reg = CapabilityRegistry()
    reg.register(FilesystemSearchCapability(tmp_path))
    spec = reg.action_spec("filesystem.search", "search")
    assert spec.required_scope == "filesystem:read"
    assert spec.risk == "low"
    assert spec.side_effects == "read_only"
    assert spec.resource_kind == "filesystem:path"
    assert spec.resource_param == "directory"
    assert spec.default_verification == {
        "policy": "schema_keys", "args": {"keys": ["results", "count"]}}
    assert set(spec.param_schema) == {"pattern", "directory", "max_results"}
    # ADR-061 D9: the singular spelling is normalized into one representation
    assert [r.role for r in spec.resources] == ["directory"]
    assert spec.resources[0].kind == "filesystem:path"
