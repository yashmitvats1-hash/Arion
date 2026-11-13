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
