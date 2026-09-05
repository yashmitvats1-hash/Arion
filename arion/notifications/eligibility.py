"""Webhook event eligibility allowlist (ADR-059 D4).

This allowlist is DELIBERATELY a separate, much smaller vocabulary than
`arion.observability.events.EVENT_KINDS`. Auditability and notification are
different concerns: the audit trail records everything, while notification
exposes a small, curated, externally-meaningful subset over the network.
Coupling the two would mean every future audit kind silently becomes an
externally observable, subscribable signal - an information-disclosure
decision made by accident.

Eligibility is therefore an ALLOWLIST: a kind is deliverable only if it is
named here. Wildcards are not supported (ADR-059 D4, invariant 6).
"""

from __future__ import annotations

from arion.observability.events import EVENT_KINDS

#: Structural exclusion (ADR-059 D4/D7): no kind under this prefix may ever
#: be webhook-eligible. Webhook lifecycle signals are not in EVENT_KINDS
#: today; this prefix guard makes the exclusion hold even if a future
#: milestone adds them, so a webhook can never notify about webhooks and
#: build a self-referential delivery loop.
WEBHOOK_RESERVED_KIND_PREFIX = "webhook."

#: Tier 1 - approval-gated human-in-the-loop signals. These are the kinds
#: for which a delayed human response has a real operational cost, which is
#: precisely what makes push notification worth its complexity.
TIER_1_EVENT_KINDS: tuple[str, ...] = (
    "approval.requested",
    "approval.queued",
    "approval.expired",
    "goal.approval.pending",
    "goal.approval.expired",
)

#: Tier 2 - opt-in outcome and liveness signals. Eligible, but a subscriber
#: must name them explicitly; they are never implied by a Tier 1 choice.
TIER_2_EVENT_KINDS: tuple[str, ...] = (
    "approval.granted",
    "approval.denied",
    "goal.approval.granted",
    "goal.approval.denied",
    "goal.blocked",
    "goal.unblocked",
)

#: The complete eligible vocabulary (ADR-059 D4).
WEBHOOK_ELIGIBLE_EVENT_KINDS: frozenset[str] = frozenset(
    TIER_1_EVENT_KINDS + TIER_2_EVENT_KINDS
)

# Fail fast at import time if the allowlist drifts away from the audit
# vocabulary: an eligible kind that no longer exists would be a silently
# undeliverable subscription.
_UNKNOWN = WEBHOOK_ELIGIBLE_EVENT_KINDS - set(EVENT_KINDS)
if _UNKNOWN:  # pragma: no cover - guards a source-level mistake
    raise RuntimeError(
        "webhook-eligible kinds are not valid audit event kinds: "
        + ", ".join(sorted(_UNKNOWN))
    )

_RESERVED_IN_AUDIT = {
    kind for kind in EVENT_KINDS if kind.startswith(WEBHOOK_RESERVED_KIND_PREFIX)
}
if _RESERVED_IN_AUDIT & WEBHOOK_ELIGIBLE_EVENT_KINDS:  # pragma: no cover
    raise RuntimeError("reserved webhook.* kinds may never be eligible")


def is_reserved_kind(kind: str) -> bool:
    """True if `kind` is structurally barred from webhook delivery."""
    return str(kind).startswith(WEBHOOK_RESERVED_KIND_PREFIX)


def is_eligible_kind(kind: str) -> bool:
    """True if `kind` may be subscribed to and delivered (ADR-059 D4)."""
    if is_reserved_kind(kind):
        return False
    return kind in WEBHOOK_ELIGIBLE_EVENT_KINDS


def validate_event_kinds(kinds: object) -> list[str]:
    """Validate a caller-supplied subscription kind list.

    Returns the normalized, de-duplicated, sorted list. Raises ValueError
    with an explicit message on any non-eligible entry - subscriptions must
    fail loudly rather than silently drop kinds a subscriber believes they
    are receiving.
    """
    if isinstance(kinds, (str, bytes)) or not isinstance(kinds, (list, tuple, set, frozenset)):
        raise ValueError("event_kinds must be a list of event kind strings")

    normalized: set[str] = set()
    invalid: list[str] = []
    for entry in kinds:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("event_kinds entries must be non-empty strings")
        candidate = entry.strip()
        if candidate == "*" or "*" in candidate:
            raise ValueError(
                "wildcard subscriptions are not supported; name each event "
                "kind explicitly (ADR-059 D4)"
            )
        if not is_eligible_kind(candidate):
            invalid.append(candidate)
            continue
        normalized.add(candidate)

    if invalid:
        raise ValueError(
            "event kind(s) not eligible for webhook delivery: "
            + ", ".join(sorted(set(invalid)))
            + "; eligible kinds are: "
            + ", ".join(sorted(WEBHOOK_ELIGIBLE_EVENT_KINDS))
        )
    if not normalized:
        raise ValueError("event_kinds must name at least one eligible event kind")

    return sorted(normalized)
