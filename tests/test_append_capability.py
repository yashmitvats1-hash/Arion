"""filesystem.append capability tests (ADR-020, Phase B).

- second write-like capability: bounded plain-text append, no shell/subprocess;
- complete ActionSpec (scope filesystem:write, risk high, side_effects
  mutating, retry_safe=False, resource_kind filesystem:path, param_schema,
  append_verified default verification, security_relevant_params=["create"]);
- registry-discoverable;
- append NEVER clobbers existing content; creation only via explicit
  create=true (security-relevant);
- bounded input size;
- traversal / symlink escapes fail closed at the capability boundary.
"""

import pytest

from arion.capabilities.append import FilesystemAppendCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry


def _sandbox(tmp_path):
    sb = tmp_path / "asandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def test_append_capability_declares_full_metadata(tmp_path):
    cap = FilesystemAppendCapability(_sandbox(tmp_path))
    spec = cap.actions[0]
    assert spec.name == "append"
    assert spec.required_scope == "filesystem:write"
    assert spec.risk == "high"
    assert spec.side_effects == "mutating"
    assert spec.retry_safe is False
    assert spec.reversible is False and spec.idempotent is False
    assert spec.resource_kind == "filesystem:path"
    assert spec.resource_param == "path"
    assert spec.param_schema["path"]["required"] is True
    assert spec.param_schema["content"]["required"] is True
    assert spec.security_relevant_params == ["create"]
    assert spec.default_verification["policy"] == "append_verified"


def test_append_capability_discoverable_via_registry(tmp_path):
    registry = CapabilityRegistry()
    registry.register(FilesystemAppendCapability(_sandbox(tmp_path)))
    summary = {c["name"]: c for c in registry.capabilities_summary()}
    assert "filesystem.append" in summary
    actions = {a["name"]: a for a in summary["filesystem.append"]["actions"]}
    assert actions["append"]["required_scope"] == "filesystem:write"
    assert actions["append"]["risk"] == "high"
    assert actions["append"]["side_effects"] == "mutating"
    assert actions["append"]["retry_safe"] is False
    assert actions["append"]["security_relevant_params"] == ["create"]


def test_append_success_deterministic(tmp_path):
    """Existing 'hello' + append 'world' -> exactly 'hello world'."""
    sb = _sandbox(tmp_path)
    (sb / "log.txt").write_text("hello", encoding="utf-8")
    cap = FilesystemAppendCapability(sb)
    out = cap.execute("append", {"path": "log.txt", "content": " world"})
    assert out["appended"] is True
    assert out["prior_size"] == 5
    assert out["appended_bytes"] == 6
    assert out["size"] == 11
    assert (sb / "log.txt").read_text(encoding="utf-8") == "hello world"


def test_append_never_clobbers_existing_content(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "log.txt").write_text("a\n", encoding="utf-8")
    cap = FilesystemAppendCapability(sb)
    cap.execute("append", {"path": "log.txt", "content": "b\n"})
    cap.execute("append", {"path": "log.txt", "content": "c\n"})
    assert (sb / "log.txt").read_text(encoding="utf-8") == "a\nb\nc\n"


def test_append_refuses_to_create_unless_explicit(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError, match="does not exist"):
        cap.execute("append", {"path": "new.txt", "content": "x"})
    assert not (sb / "new.txt").exists()
    # create=true is required and is SECURITY-RELEVANT (fingerprinted)
    out = cap.execute("append", {"path": "new.txt", "content": "x", "create": True})
    assert out["appended"] is True and out["prior_size"] == 0
    assert (sb / "new.txt").read_text(encoding="utf-8") == "x"


def test_append_bounded_input_size(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "log.txt").write_text("start", encoding="utf-8")
    cap = FilesystemAppendCapability(sb, max_bytes=100)
    with pytest.raises(CapabilityError, match="too large"):
        cap.execute("append", {"path": "log.txt", "content": "x" * 101})
    assert (sb / "log.txt").read_text(encoding="utf-8") == "start"  # unchanged


def test_append_no_shell_subprocess(tmp_path):
    """A path with shell metacharacters is a plain filename - no shell."""
    sb = _sandbox(tmp_path)
    (sb / "log.txt").write_text("s", encoding="utf-8")
    cap = FilesystemAppendCapability(sb)
    name = "a; touch /tmp/pwned2;.txt"
    out = cap.execute("append", {"path": name, "content": "x", "create": True})
    assert out["appended"] is True
    assert (sb / name).read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "pwned2").exists()


def test_append_path_traversal_rejected(tmp_path):
    sb = _sandbox(tmp_path)
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("append", {"path": "../outside.txt", "content": "x"})
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("append", {"path": "/etc/passwd", "content": "x"})
    assert not (tmp_path / "outside.txt").exists()


def test_append_symlink_escape_rejected(tmp_path):
    sb = _sandbox(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    (sb / "link.txt").symlink_to(outside)
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError, match="escapes sandbox"):
        cap.execute("append", {"path": "link.txt", "content": "pwned"})
    assert outside.read_text(encoding="utf-8") == "original"  # untouched


def test_append_to_directory_rejected(tmp_path):
    sb = _sandbox(tmp_path)
    (sb / "subdir").mkdir()
    cap = FilesystemAppendCapability(sb)
    with pytest.raises(CapabilityError):
        cap.execute("append", {"path": "subdir", "content": "x"})


def test_append_unknown_action(tmp_path):
    cap = FilesystemAppendCapability(_sandbox(tmp_path))
    with pytest.raises(CapabilityError):
        cap.execute("prepend", {"path": "log.txt", "content": "x"})
