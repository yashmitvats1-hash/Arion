"""F06 fourth pass: staging-vs-publication race, deterministic interposition
tests (issue #23 work order item 3).

The attacker substitutes the staging entry AFTER identity verification
succeeds but BEFORE the publish syscall.  The interposition seam is
``dirfs.identity_matches``: the hook runs the REAL verification, performs
the substitution (regular file or symlink-to-victim, moved in by unlink or
by rename), then returns True (stale verification).  Matrix: both
``overwrite=false/true`` x regular-file/symlink substitution.

Required invariants, asserted in every case:

- the attacker entry is NEVER published (the destination never holds
  attacker or victim content);
- an existing destination is never unexpectedly replaced (it keeps its
  content, or - under requested overwrite - receives OUR content only);
- the substituted/unowned entry is never destroyed (never unlinked by
  cleanup; the victim file a symlink points at stays intact).

Identity-bound publication (Linux ``/proc/self/fd`` + held staging
descriptor) publishes the EXACT pinned inode or fails closed; the
name-based fallback is documented as not race-proof and is not claimed.
"""

from __future__ import annotations

import os

import pytest

from arion.capabilities import dirfs
from arion.capabilities.registry import CapabilityError
from arion.capabilities.write import FilesystemWriteCapability

PAYLOAD = b"PAYLOAD"
ATTACKER = b"ATTACKER_CONTENT"
DEST = b"DEST_CONTENT"
VICTIM = b"VICTIM_CONTENT"


def _sandbox(tmp_path):
    sb = tmp_path / "sandbox"
    sb.mkdir()
    return sb


def _interpose(monkeypatch, *, move: str, entry: str, victim_path, create_dest):
    """Wrap ``dirfs.identity_matches``: verify for real, then - AFTER
    verification succeeds and BEFORE publish - substitute the staging
    entry and (optionally) create the destination concurrently."""
    real = dirfs.identity_matches
    state = {"done": False, "staging": None}

    def interposed(parent_fd, name, identity):
        result = real(parent_fd, name, identity)
        if result and not state["done"] and name.endswith(".arion-write-tmp"):
            state["done"] = True
            state["staging"] = name
            if create_dest:
                fd = os.open(
                    "target.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=parent_fd,
                )
                os.write(fd, DEST)
                os.close(fd)
            if move == "unlink":
                os.unlink(name, dir_fd=parent_fd)
            else:  # rename: our pinned inode stays linked at a hidden name
                os.rename(
                    name, name + ".moved",
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                )
            if entry == "file":
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=parent_fd,
                )
                os.write(fd, ATTACKER)
                os.close(fd)
            else:  # symlink to the victim file
                os.symlink(victim_path, name, dir_fd=parent_fd)
        return result

    monkeypatch.setattr(dirfs, "identity_matches", interposed)
    return state


def _attack_left_intact(sb, state, entry, victim_path):
    """The substituted entry survives at the staging name; a symlink's
    victim is intact; the attacker content is never destroyed."""
    staging_path = sb / state["staging"]
    if entry == "file":
        assert staging_path.read_bytes() == ATTACKER
    else:
        assert staging_path.is_symlink()
        assert os.readlink(staging_path) == str(victim_path)
        assert victim_path.read_bytes() == VICTIM


@pytest.mark.parametrize("overwrite", [False, True], ids=["no-overwrite", "overwrite"])
@pytest.mark.parametrize("entry", ["file", "symlink"], ids=["file-substitution", "symlink-substitution"])
@pytest.mark.parametrize("move", ["unlink", "rename"], ids=["entry-replaced", "inode-renamed-away"])
def test_substituted_staging_never_publishes_attacker_entry(
    tmp_path, monkeypatch, overwrite, entry, move,
):
    sb = _sandbox(tmp_path)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    state = _interpose(
        monkeypatch, move=move, entry=entry, victim_path=victim,
        create_dest=True,
    )

    if move == "unlink":
        # The pinned inode was unlinked by the substitution: publication
        # fails closed (identity-bound link resolves nothing) - for
        # overwrite=true the previous destination is rolled back.
        with pytest.raises(CapabilityError):
            cap.execute("write", {
                "path": "target.txt",
                "content": PAYLOAD.decode(),
                "overwrite": overwrite,
            })
        assert (sb / "target.txt").read_bytes() == DEST
    elif not overwrite:
        # Concurrent destination created in the window wins via EEXIST;
        # it is never clobbered.
        with pytest.raises(CapabilityError, match="exists"):
            cap.execute("write", {
                "path": "target.txt",
                "content": PAYLOAD.decode(),
                "overwrite": False,
            })
        assert (sb / "target.txt").read_bytes() == DEST
    else:
        # Identity-bound replace: the EXACT pinned inode (renamed away but
        # still linked) becomes the destination - OUR content only.
        cap.execute("write", {
            "path": "target.txt",
            "content": PAYLOAD.decode(),
            "overwrite": True,
        })
        assert (sb / "target.txt").read_bytes() == PAYLOAD

    # Invariants across the whole matrix:
    # 1. attacker entry never published
    assert (sb / "target.txt").read_bytes() not in (ATTACKER, VICTIM)
    # 2. substituted/unowned entry never destroyed
    _attack_left_intact(sb, state, entry, victim)
    monkeypatch.undo()


@pytest.mark.parametrize("entry", ["file", "symlink"], ids=["file-substitution", "symlink-substitution"])
def test_late_substitution_cannot_redirect_identity_bound_publish(
    tmp_path, monkeypatch, entry,
):
    """With no pre-existing destination and the attacker substituting the
    staging name by rename (the pinned inode stays linked elsewhere), the
    identity-bound publish still yields OUR content - the substituted
    entry is never published and never destroyed."""
    sb = _sandbox(tmp_path)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    state = _interpose(
        monkeypatch, move="rename", entry=entry, victim_path=victim,
        create_dest=False,
    )

    cap.execute("write", {
        "path": "target.txt", "content": PAYLOAD.decode(), "overwrite": False,
    })

    assert (sb / "target.txt").read_bytes() == PAYLOAD
    _attack_left_intact(sb, state, entry, victim)
    monkeypatch.undo()


def test_unlink_move_leaves_no_destination_when_absent(tmp_path, monkeypatch):
    """overwrite=false, no destination, entry replaced outright: the write
    fails closed and NO destination appears."""
    sb = _sandbox(tmp_path)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    state = _interpose(
        monkeypatch, move="unlink", entry="file", victim_path=victim,
        create_dest=False,
    )

    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "target.txt", "content": PAYLOAD.decode(),
            "overwrite": False,
        })

    assert not (sb / "target.txt").exists()
    _attack_left_intact(sb, state, "file", victim)
    monkeypatch.undo()


def test_cleanup_never_unlinks_substituted_entry(tmp_path, monkeypatch):
    """After a failed publish the cleanup's identity gate must leave the
    substituted entry alone - never unlink what we did not create."""
    sb = _sandbox(tmp_path)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    state = _interpose(
        monkeypatch, move="unlink", entry="symlink", victim_path=victim,
        create_dest=False,
    )

    with pytest.raises(CapabilityError):
        cap.execute("write", {
            "path": "target.txt", "content": PAYLOAD.decode(),
            "overwrite": True,
        })

    assert (sb / state["staging"]).is_symlink()
    assert victim.read_bytes() == VICTIM
    monkeypatch.undo()


# --------------------------------------------------------------------- #
# fifth pass item A: destination rollback must never clobber a
# concurrent creator (window: after trash rename, before fd link)
# --------------------------------------------------------------------- #

OLD_DEST = b"OLD_DESTINATION"
CONCURRENT = b"CONCURRENT_ENTRY"


def _racing_link_at_publish(monkeypatch, *, leaf, occupant, victim_path):
    """One-shot interposition at the publication link: a concurrent actor
    lands ``leaf`` (regular file or symlink) AFTER the old destination was
    moved to trash and BEFORE the descriptor-bound link, forcing EEXIST."""
    real_link = os.link
    state = {"fired": False}

    def racing_link(src, dst, **kwargs):
        if (
            not state["fired"]
            and dst == leaf
            and str(src).startswith("/proc/self/fd/")
        ):
            state["fired"] = True
            parent_fd = kwargs.get("dst_dir_fd")
            if occupant == "file":
                fd = os.open(
                    leaf,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=parent_fd,
                )
                os.write(fd, CONCURRENT)
                os.close(fd)
            else:
                os.symlink(victim_path, leaf, dir_fd=parent_fd)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    return state


@pytest.mark.parametrize("occupant", ["file", "symlink"], ids=["concurrent-file", "concurrent-symlink"])
def test_rollback_never_clobbers_concurrent_creator_dest_present(
    tmp_path, monkeypatch, occupant,
):
    """Old destination present: publication fails EEXIST after the trash
    rename; the rollback must NOT replace the concurrent entry.  The old
    destination is preserved under its recoverable private trash name;
    neither party's data is discarded."""
    sb = _sandbox(tmp_path)
    (sb / "target.txt").write_bytes(OLD_DEST)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    _racing_link_at_publish(
        monkeypatch, leaf="target.txt", occupant=occupant,
        victim_path=victim,
    )

    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("write", {
            "path": "target.txt", "content": "NEW", "overwrite": True,
        })
    message = str(excinfo.value)
    assert "nothing was overwritten" in message
    assert "preserved at" in message

    # The concurrent entry is intact at the destination name.
    if occupant == "file":
        assert (sb / "target.txt").read_bytes() == CONCURRENT
    else:
        assert (sb / "target.txt").is_symlink()
        assert victim.read_bytes() == VICTIM
    # The old destination survives under a recoverable private name.
    trash_entries = [
        p for p in sb.iterdir() if p.name.endswith(".arion-replace-tmp")
    ]
    assert len(trash_entries) == 1
    assert trash_entries[0].read_bytes() == OLD_DEST
    # Our staging file was safely cleaned up (never the substituted entry,
    # which never appeared here).
    staging_left = [
        p for p in sb.iterdir() if p.name.endswith(".arion-write-tmp")
    ]
    assert staging_left == []
    monkeypatch.undo()


@pytest.mark.parametrize("occupant", ["file", "symlink"], ids=["concurrent-file", "concurrent-symlink"])
def test_no_rollback_clobber_dest_absent_concurrent_creator(
    tmp_path, monkeypatch, occupant,
):
    """Destination absent: the concurrent creator lands at the free name
    before the descriptor link.  The link fails EEXIST and there is no
    rollback - the concurrent entry must survive untouched."""
    sb = _sandbox(tmp_path)
    victim = sb / "victim.txt"
    victim.write_bytes(VICTIM)
    cap = FilesystemWriteCapability(sb)
    _racing_link_at_publish(
        monkeypatch, leaf="target.txt", occupant=occupant,
        victim_path=victim,
    )

    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("write", {
            "path": "target.txt", "content": "NEW", "overwrite": True,
        })
    assert "concurrent entry" in str(excinfo.value)
    assert "nothing was overwritten" in str(excinfo.value)

    if occupant == "file":
        assert (sb / "target.txt").read_bytes() == CONCURRENT
    else:
        assert (sb / "target.txt").is_symlink()
        assert victim.read_bytes() == VICTIM
    # No trash existed; nothing of ours was left behind.
    assert [
        p for p in sb.iterdir() if p.name.endswith(".arion-replace-tmp")
    ] == []
    assert [
        p for p in sb.iterdir() if p.name.endswith(".arion-write-tmp")
    ] == []
    monkeypatch.undo()


# --------------------------------------------------------------------- #
# fifth pass item B: platform boundary - no name-based fallback
# --------------------------------------------------------------------- #

def test_unavailable_proc_fails_closed_before_mutation(tmp_path, monkeypatch):
    """When /proc/self/fd is unavailable the write capability refuses
    BEFORE any staging file exists - the name-based fallback (not
    race-proof against staging substitution) is never offered."""
    monkeypatch.setattr(dirfs, "IDENTITY_BOUND_PUBLISH", False)
    sb = _sandbox(tmp_path)
    cap = FilesystemWriteCapability(sb)

    with pytest.raises(CapabilityError) as excinfo:
        cap.execute("write", {"path": "target.txt", "content": "X"})
    assert "identity-bound publication" in str(excinfo.value)
    assert "NOT" in str(excinfo.value) or "not" in str(excinfo.value)

    # refused before mutation: no destination, no staging residue
    assert list(sb.iterdir()) == []


def test_unavailable_proc_publish_functions_fail_closed(tmp_path, monkeypatch):
    """The publish primitives themselves fail closed when the descriptor
    primitive is unavailable - a staging substitution can never be
    published by name."""
    monkeypatch.setattr(dirfs, "IDENTITY_BOUND_PUBLISH", False)
    sb = _sandbox(tmp_path)
    parent_fd = os.open(sb, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(
            "stg.arion-write-tmp",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        os.write(fd, b"X")
        identity = dirfs.file_identity(fd)
        with pytest.raises(CapabilityError) as excinfo:
            dirfs.publish_no_overwrite(
                parent_fd, "stg.arion-write-tmp", "target.txt", identity,
                staging_fd=fd,
            )
        assert "identity-bound publication" in str(excinfo.value)
        with pytest.raises(CapabilityError):
            dirfs.publish_replace(
                parent_fd, "stg.arion-write-tmp", "target.txt", identity,
                staging_fd=fd,
            )
        # nothing was published
        assert dirfs.name_identity(parent_fd, "target.txt") is None
        os.close(fd)
    finally:
        os.close(parent_fd)
    monkeypatch.undo()
