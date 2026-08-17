"""filesystem.write capability tests (ADR-019).

- smallest real mutating capability: bounded text write, no shell/subprocess;
- complete ActionSpec (scope filesystem:write, risk high, side_effects
  mutating, retry_safe=False, resource_kind filesystem:path, param_schema,
  default verification, security_relevant_params=["overwrite"]);
- registry-discoverable;
- bounded input size;
- overwrite semantics (never clobber without explicit overwrite=True);
- traversal / symlink escapes fail closed at the capability boundary.
"""

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability


def _sandbox(tmp_path):
    sb = tmp_path / "repo"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def test_write_capability_declares_full_metadata(tmp_path):
    cap = FilesystemWriteCapability(_sandbox(tmp_path))
    spec = cap.actions[0]
    assert spec.name == "write"
    assert spec.required_scope == "filesystem:write"
    assert spec.risk == "high"
    assert spec.side_effects == "mutating"
    assert spec.retry_safe is False
    assert spec.reversible is False and spec.idempotent is False
    assert spec.resource_kind == "filesystem:path"
    assert spec.resource_param == "path"
    assert spec.param_schema["path"]["required"] is True
    assert spec.param_schema["content"]["required"] is True
    assert spec.security_relevant_params == ["overwrite"]
    assert spec.default_verification["policy"] == "write_verified"


def test_write_capability_discoverable_via_registry(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemWriteCapability(_sandbox(tmp_path)))
    summary = {c["name"]: c for c in registry.capabilities_summary()}
    assert "filesystem.write" in summary
    actions = {a["name"]: a for a in summary["filesystem.write"]["actions"]}
    assert actions["write"]["required_scope"] == "filesystem:write"
    assert actions["write"]["risk"] == "high"
    assert actions["write"]["side_effects"] == "mutating"
    assert actions["write"]["retry_safe"] is False
    assert actions["write"]["resource_kind"] == "filesystem:path"
    assert actions["write"]["security_relevant_params"] == ["overwrite"]


def test_write_creates_file(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    out = cap.execute("write", {"path": "notes.txt", "content": "hello world"})
    assert out["written"] is True
    assert out["path"] == "notes.txt"
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello world"


def test_write_no_shell_subprocess(tmp_path):
    """The capability must never spawn a process; inspect the module for
    subprocess/os.system usage at the class level is not needed - the
    implementation only uses Path I/O. This test proves behavior: a filename
    containing shell metacharacters is treated as a plain filename."""
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    name = "a; touch /tmp/pwned;.txt"
    out = cap.execute("write", {"path": name, "content": "x"})
    assert out["written"] is True
    assert (sb / name).read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "pwned").exists()


def test_write_overwrite_refused_by_default(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "notes.txt").write_text("original", encoding="utf-8")
    cap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError, match="exists"):
        cap.execute("write", {"path": "notes.txt", "content": "new"})
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "original"  # unchanged


def test_write_overwrite_allowed_when_explicit(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "notes.txt").write_text("original", encoding="utf-8")
    cap = FilesystemWriteCapability(sb)
    out = cap.execute("write", {"path": "notes.txt", "content": "new", "overwrite": True})
    assert out["written"] is True
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "new"


def test_write_bounded_input_size(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb, max_bytes=100)
    with pytest.raises(CapabilityError, match="too large"):
        cap.execute("write", {"path": "big.txt", "content": "x" * 101})
    assert not (sb / "big.txt").exists()


def test_write_path_traversal_rejected(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("write", {"path": "../outside.txt", "content": "x"})
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("write", {"path": "/etc/passwd", "content": "x"})
    assert not (tmp_path / "outside.txt").exists()


def test_write_symlink_escape_rejected(tmp_path):
    """A symlink pointing outside the sandbox cannot be written through."""
    sb = _sandbox(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    (sb / "link.txt").symlink_to(outside)
    cap = FilesystemWriteCapability(sb)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("write", {"path": "link.txt", "content": "pwned", "overwrite": True})
    assert outside.read_text(encoding="utf-8") == "original"  # untouched


def test_write_unknown_action(tmp_path):
    cap = FilesystemWriteCapability(_sandbox(tmp_path))
    with pytest.raises(CapabilityError):
        cap.execute("delete", {"path": "notes.txt"})
