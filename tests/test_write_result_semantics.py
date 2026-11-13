"""F06 fifth pass item F: write result semantics vs. no-follow leaf
publication (issue #23 work order).

Publication replaces the leaf ENTRY itself (no-follow): an in-root leaf
symlink with ``overwrite=true`` has the SYMLINK entry replaced - its
target is never opened or written - while ``_resolve_inside`` resolves
through the symlink.  The result's ``canonical_path`` used to report the
RESOLVED target, which could identify a different file from the one
changed.  It now reports the lexical destination that publication
actually changed, and the no-follow leaf semantics are documented.
Resource/approval identity remains the requested relative path.
"""

from __future__ import annotations

import os

from arion.capabilities.write import FilesystemWriteCapability


def test_leaf_symlink_replaced_as_name_result_matches_changed_entry(tmp_path):
    """Minimal repro of the canonical-path mismatch, now pinned: the
    symlink ENTRY is what changes, the target file is untouched, and the
    reported canonical_path is the lexical entry - not the resolved
    target."""
    sb = tmp_path / "sandbox"
    sb.mkdir()
    victim = sb / "victim.txt"
    victim.write_text("VICTIM", encoding="utf-8")
    os.symlink(victim, sb / "link.txt")

    cap = FilesystemWriteCapability(sb)
    result = cap.execute("write", {
        "path": "link.txt", "content": "PAYLOAD", "overwrite": True,
    })

    # the entry that changed is the lexical leaf (symlink replaced)
    assert result["canonical_path"] == str(sb / "link.txt")
    assert result["canonical_path"] != str(victim)
    assert not (sb / "link.txt").is_symlink()
    assert (sb / "link.txt").read_text(encoding="utf-8") == "PAYLOAD"
    # the resolved target was never written
    assert victim.read_text(encoding="utf-8") == "VICTIM"


def test_regular_write_canonical_path_is_lexical_destination(tmp_path):
    """Without symlinks the lexical destination and resolved path agree -
    behavior unchanged for the normal case."""
    sb = tmp_path / "sandbox"
    sb.mkdir()
    cap = FilesystemWriteCapability(sb)
    result = cap.execute("write", {
        "path": "docs/a.txt", "content": "X", "overwrite": False,
    })
    assert result["canonical_path"] == str(sb / "docs" / "a.txt")
    assert (sb / "docs" / "a.txt").read_text(encoding="utf-8") == "X"
