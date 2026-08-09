"""Capability layer tests: sandbox boundary, read/list, registry discovery."""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry


def test_read_file(sandbox):
    cap = FilesystemReadCapability(sandbox)
    obs = cap.execute("read", {"path": "README.md"})
    assert obs["content"].startswith("# Test Repo")
    assert obs["path"] == "README.md"
    assert obs["size"] == len("# Test Repo\n\nA sandboxed repo for Arion tests.\n")


def test_read_nested_file(sandbox):
    cap = FilesystemReadCapability(sandbox)
    obs = cap.execute("read", {"path": "docs/design.md"})
    assert "# Design" in obs["content"]


def test_list_directory(sandbox):
    cap = FilesystemReadCapability(sandbox)
    obs = cap.execute("list", {"path": "."})
    names = {e["name"] for e in obs["entries"]}
    assert {"README.md", "notes.txt", "docs"} <= names
    obs2 = cap.execute("list", {"path": "docs"})
    assert obs2["entries"][0]["name"] == "design.md"


def test_missing_file_raises(sandbox):
    cap = FilesystemReadCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("read", {"path": "nope.txt"})


def test_escape_via_dotdot_blocked(sandbox):
    cap = FilesystemReadCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("read", {"path": "../outside.txt"})
    with pytest.raises(CapabilityError):
        cap.execute("read", {"path": "../../etc/passwd"})


def test_escape_via_symlink_blocked(sandbox, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sandbox / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    cap = FilesystemReadCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("read", {"path": "escape.txt"})


def test_binary_file_rejected(sandbox):
    (sandbox / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    cap = FilesystemReadCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("read", {"path": "blob.bin"})


def test_unknown_action(sandbox):
    cap = FilesystemReadCapability(sandbox)
    with pytest.raises(CapabilityError):
        cap.execute("write", {"path": "README.md", "content": "x"})


def test_registry_discovery(registry):
    assert registry.has("filesystem.read")
    assert not registry.has("shell.exec")
    summary = registry.capabilities_summary()
    assert summary[0]["name"] == "filesystem.read"
    assert summary[0]["actions"][0]["required_scope"] == "filesystem:read"


def test_action_metadata_declared(registry):
    """Scenario 6: capability/action metadata is explicit and introspectable."""
    read = registry.action_spec("filesystem.read", "read")
    assert read is not None
    assert read.required_scope == "filesystem:read"
    assert read.risk == "low"
    assert read.side_effects == "read_only"
    assert read.reversible is True
    assert read.idempotent is True
    assert read.retry_safe is True
    # resource metadata: read/list target a filesystem path (fail-closed policy)
    assert read.resource_kind == "filesystem:path"
    assert read.resource_param == "path"

    listing = registry.action_spec("filesystem.read", "list")
    assert listing is not None
    assert listing.required_scope == "filesystem:read"
    assert listing.risk == "low"

    # unknown action -> no spec
    assert registry.action_spec("filesystem.read", "write") is None

    # summary exposes the metadata for discovery/planning
    summary = registry.capabilities_summary()[0]
    action_dict = summary["actions"][0]
    assert action_dict["risk"] == "low"
    assert action_dict["side_effects"] == "read_only"
    assert action_dict["retry_safe"] is True
