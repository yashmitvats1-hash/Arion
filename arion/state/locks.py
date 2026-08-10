"""Durable cross-process advisory mutation locks (ADR-021).

A MutationLock is a durable, SQLite-backed coordination record guarding ONE
canonical security-relevant resource (resource_kind + canonical resource).
It is held only around the actual mutation execution window:

    plan -> authorization -> approval if required -> live re-authorization
        -> acquire mutation lock -> mutate -> verify -> release lock

CRITICAL: a mutation lock is COORDINATION, NOT AUTHORIZATION.

- Acquiring a lock never grants permission to mutate; authorization is
  evaluated independently (live policy + approval queue) for EVERY mutation
  attempt, before the lock is even requested.
- The engine never does `lock -> authorize -> mutate`.
- A lock cannot be created, transferred, released, or forged by memory,
  cognition, strategy, model output, or recovery guidance - the lock store is
  the only authority on lock state.
- Locks carry bounded identifiers only (lock/resource/owner/timestamps);
  never file contents, prompts, or secrets.

Leases: every lock has an expires_at (acquired_at + lease_seconds). A stale
(expired) lock can be reclaimed atomically, so a crashed process never
permanently wedges a resource. The clock is injectable for deterministic
tests (defaults to the real clock via utcnow).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from arion.state.models import new_id, utcnow


class MutationLockStatus(str, Enum):
    HELD = "held"      # owned by a live lease
    EXPIRED = "expired"  # lease elapsed; reclaimable (derived from expires_at)


class MutationLockError(Exception):
    """Typed failure for the mutation lock store (fail closed)."""


class MutationLockTimeoutError(MutationLockError):
    """Bounded lock-contention waiting exhausted its deadline (ADR-022).

    Durable, typed, explainable: the task was waiting (coordination only)
    for a resource that stayed locked past the deadline. NOT a mutation
    failure - no recovery record is created, and the capability never ran.
    """


@dataclass
class MutationLock:
    """One durable advisory lock on a canonical mutation resource.

    Metadata is bounded by design: identifiers + timestamps only.
    """

    lock_id: str
    resource_kind: str
    resource: str          # canonical resource identifier (see canonical_resource)
    capability: str
    action: str
    owner_id: str          # explicit, unique owner/process identity
    acquired_at: str
    expires_at: str        # lease expiry (injectable clock)

    @property
    def status(self) -> MutationLockStatus:
        return MutationLockStatus.HELD  # expiry is checked against the clock at query time

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MutationLock":
        return cls(
            lock_id=d["lock_id"],
            resource_kind=d["resource_kind"],
            resource=d["resource"],
            capability=d["capability"],
            action=d["action"],
            owner_id=d["owner_id"],
            acquired_at=d["acquired_at"],
            expires_at=d["expires_at"],
        )


def canonical_resource(resource_kind: str, resource: str) -> str:
    """Canonical identity of a security-relevant mutation resource.

    The lock key is (resource_kind, canonical resource), NOT an arbitrary
    display string: two processes that target the same underlying resource
    must contend even when they spell the path differently. For
    filesystem:path the canonical form is the normalized relative path;
    other kinds use the identifier as-is (kind-specific canonicalizers can be
    added per resource kind).
    """
    if resource_kind == "filesystem:path" and isinstance(resource, str):
        norm = os.path.normpath(resource)
        return norm if norm not in ("", ".") else resource
    return resource


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _add_seconds(iso: str, seconds: float) -> str:
    return (_parse_iso(iso) + timedelta(seconds=seconds)).isoformat()


class MutationLockStore(Protocol):
    """Persistence contract for the durable mutation lock registry.

    The SQLite implementation makes acquire/reclaim ATOMIC across processes
    (BEGIN IMMEDIATE transactions; the database is the coordination
    authority). No in-memory locks, no process-local globals, no lock files.
    """

    def acquire(self, resource_kind: str, resource: str, capability: str,
                action: str, owner_id: str, lease_seconds: float,
                now: str | None = None) -> MutationLock: ...
    def release(self, lock_id: str, owner_id: str) -> bool: ...
    def get(self, lock_id: str) -> MutationLock | None: ...
    def list(self, resource_kind: str | None = None,
             resource: str | None = None) -> list[MutationLock]: ...
    def reclaim_expired(self, now: str | None = None,
                        resource_kind: str | None = None,
                        resource: str | None = None) -> list[str]: ...
