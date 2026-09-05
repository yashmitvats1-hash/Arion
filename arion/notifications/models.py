"""Durable webhook models: subscriptions, secret versions, deliveries.

ADR-059 D5.1 (internal delivery record), D9 (delivery state model),
D11.2/D11.3 (secret versioning + retry horizon).

Bounded metadata only. A delivery row carries identifiers, timestamps, the
frozen serialized envelope bytes, lease fencing fields and bounded error
text. It NEVER carries capability output, prompts, model output, file
contents, or secret material.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from arion.state.models import new_id, utcnow


class WebhookStateError(Exception):
    """Typed invalid webhook state transition or invariant breach (fail closed)."""


class WebhookConfigurationError(Exception):
    """Typed invalid subscription/secret configuration (fail closed)."""


# --------------------------------------------------------------------------- #
# delivery lifecycle (ADR-059 D9)
# --------------------------------------------------------------------------- #


class DeliveryStatus(str, Enum):
    """Durable lifecycle of one webhook delivery row (ADR-059 D9)."""

    PENDING = "pending"          # durably captured; claimable when due
    DELIVERING = "delivering"    # claimed under a live lease
    DELIVERED = "delivered"      # terminal: success status received
    FAILED = "failed"            # terminal: permanent, non-retryable failure
    DEAD_LETTER = "dead_letter"  # terminal: retryable failures exhausted
    CANCELLED = "cancelled"      # terminal: subscription deleted while non-terminal


TERMINAL_DELIVERY_STATUSES = frozenset({
    DeliveryStatus.DELIVERED,
    DeliveryStatus.FAILED,
    DeliveryStatus.DEAD_LETTER,
    DeliveryStatus.CANCELLED,
})

#: Terminal statuses that a `POST /deliveries/<id>/retry` may resurrect,
#: subject to the `retry_eligible_until` horizon (ADR-059 D10/D11.3).
MANUALLY_RETRYABLE_STATUSES = frozenset({
    DeliveryStatus.FAILED,
    DeliveryStatus.DEAD_LETTER,
})

NON_TERMINAL_DELIVERY_STATUSES = frozenset({
    DeliveryStatus.PENDING,
    DeliveryStatus.DELIVERING,
})


# Legal transitions; terminal states are final EXCEPT for the explicit
# admin-initiated manual retry (FAILED/DEAD_LETTER -> PENDING), which is a
# deliberate, horizon-bounded resurrection (ADR-059 D10).
_LEGAL_TRANSITIONS: dict[DeliveryStatus, set[DeliveryStatus]] = {
    DeliveryStatus.PENDING: {
        DeliveryStatus.DELIVERING,
        DeliveryStatus.CANCELLED,
    },
    DeliveryStatus.DELIVERING: {
        DeliveryStatus.PENDING,       # retryable failure -> backoff
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
        DeliveryStatus.DEAD_LETTER,
        DeliveryStatus.CANCELLED,     # subscription deleted mid-flight
    },
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.FAILED: {DeliveryStatus.PENDING},        # manual retry only
    DeliveryStatus.DEAD_LETTER: {DeliveryStatus.PENDING},   # manual retry only
    DeliveryStatus.CANCELLED: set(),
}


def legal_delivery_transition(current: DeliveryStatus, target: DeliveryStatus) -> bool:
    return target in _LEGAL_TRANSITIONS.get(current, set())


class SecretVersionStatus(str, Enum):
    """Lifecycle of one subscription signing-secret version (ADR-059 D11.2)."""

    ACTIVE = "active"      # signs newly enqueued deliveries
    RETIRING = "retiring"  # material retained only for referencing deliveries
    RETIRED = "retired"    # material destroyed


def iso_plus(iso: str, seconds: float) -> str:
    """iso + seconds (naive/aware-safe; mirrors the store/lock helpers)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def iso_plus_days(iso: str, days: float) -> str:
    return iso_plus(iso, float(days) * 86400.0)


def iso_leq(a: str, b: str) -> bool:
    """Timezone-safe `a <= b` for ISO timestamps produced by utcnow()."""
    da = datetime.fromisoformat(a)
    db = datetime.fromisoformat(b)
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=timezone.utc)
    return da <= db


# --------------------------------------------------------------------------- #
# subscription
# --------------------------------------------------------------------------- #


@dataclass
class WebhookSubscription:
    """One durable webhook subscription.

    `event_kinds` is an explicit, non-empty selection intersected with the
    eligibility allowlist (ADR-059 D4.6). There is no wildcard.
    """

    subscription_id: str
    url: str
    event_kinds: list[str]
    enabled: bool = True
    description: str = ""
    created_by: str = "system"
    active_secret_version: int = 1
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    deleted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WebhookSubscription":
        return cls(
            subscription_id=d["subscription_id"],
            url=d["url"],
            event_kinds=list(d.get("event_kinds", []) or []),
            enabled=bool(d.get("enabled", True)),
            description=d.get("description", "") or "",
            created_by=d.get("created_by", "system"),
            active_secret_version=int(d.get("active_secret_version", 1)),
            created_at=d.get("created_at", utcnow()),
            updated_at=d.get("updated_at", utcnow()),
            deleted_at=d.get("deleted_at"),
        )

    def public_dict(self) -> dict[str, Any]:
        """Explicit API projection. NEVER contains secret material."""
        return {
            "subscription_id": self.subscription_id,
            "url": self.url,
            "event_kinds": sorted(self.event_kinds),
            "enabled": self.enabled,
            "description": self.description,
            "created_by": self.created_by,
            "active_secret_version": self.active_secret_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class WebhookSecretVersion:
    """One signing-secret version of a subscription (ADR-059 D11.2).

    `secret` is the raw signing material and is NEVER projected, logged,
    emitted or returned by any read API. It is destroyed (set to None with
    status RETIRED) once no retained delivery referencing this version can
    still be manually retried (ADR-059 D11.3, invariant 23).
    """

    subscription_id: str
    version: int
    secret: str | None
    status: SecretVersionStatus = SecretVersionStatus.ACTIVE
    created_at: str = field(default_factory=utcnow)
    retiring_at: str | None = None
    retired_at: str | None = None

    @property
    def material_present(self) -> bool:
        return self.secret is not None and self.status is not SecretVersionStatus.RETIRED

    def public_dict(self) -> dict[str, Any]:
        """Explicit API projection: metadata only, never the secret."""
        return {
            "subscription_id": self.subscription_id,
            "version": self.version,
            "status": self.status.value,
            "created_at": self.created_at,
            "retiring_at": self.retiring_at,
            "retired_at": self.retired_at,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-relevant
        return (
            f"WebhookSecretVersion(subscription_id={self.subscription_id!r}, "
            f"version={self.version!r}, status={self.status.value!r}, "
            f"secret={'<redacted>' if self.secret else None!r})"
        )


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #


@dataclass
class WebhookDelivery:
    """One durable delivery intent (ADR-059 D5.1).

    `body_bytes` is the exact serialized external envelope, frozen at
    enqueue and transmitted byte-identically on every attempt (invariant 18).
    `secret_version` is immutable for the row's life.
    """

    delivery_id: str
    subscription_id: str
    event_id: str
    event_kind: str
    occurred_at: str
    sequence: int
    secret_version: int
    body_bytes: bytes
    url: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    next_attempt_at: str = field(default_factory=utcnow)
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None
    retry_eligible_until: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    completed_at: str | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_DELIVERY_STATUSES

    def has_retry_capability(self, now: str) -> bool:
        """ADR-059 D11.2 rule 7 — the predicate that governs BOTH manual
        retry eligibility and secret-version retention.

        A delivery has retry capability when it is non-terminal, OR it is
        `failed`/`dead_letter` and `now <= retry_eligible_until`.
        `delivered` and `cancelled` never have retry capability.

        Deliberately independent of subscription liveness (ADR-059 D15.4):
        a deleted subscription's retryable history still pins its secret.
        """
        if self.status in NON_TERMINAL_DELIVERY_STATUSES:
            return True
        if self.status not in MANUALLY_RETRYABLE_STATUSES:
            return False
        if self.retry_eligible_until is None:
            return False
        return iso_leq(now, self.retry_eligible_until)

    def public_dict(self) -> dict[str, Any]:
        """Explicit API projection (ADR-059 D15.2 item 7).

        Never an asdict() passthrough: `body_bytes` is deliberately excluded
        so a future field addition cannot leak it, and no secret material is
        reachable from this shape.
        """
        return {
            "delivery_id": self.delivery_id,
            "subscription_id": self.subscription_id,
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "occurred_at": self.occurred_at,
            "sequence": self.sequence,
            "secret_version": self.secret_version,
            "url": self.url,
            "status": self.status.value,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "retry_eligible_until": self.retry_eligible_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


def new_subscription_id() -> str:
    return new_id("whs")


def new_delivery_id() -> str:
    return new_id("whd")
