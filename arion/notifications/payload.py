"""Versioned external webhook payload envelope (ADR-059 D5).

The single most important rule in this module: an outbound webhook body is
NEVER `AuditEvent.to_dict()`. The audit record is an internal, freely
evolving structure; the webhook body is a published network contract. Coupling
them would (a) turn every internal field addition into an unversioned
external change and (b) leak internal detail keys to third parties by default.

Every eligible event kind therefore has an EXPLICIT projection: an allowlist
of the detail fields that are part of the contract. An unknown detail key is
dropped, not forwarded.

The envelope deliberately does NOT carry `subscription_id` (ADR-059 D5,
Revision 2): the receiver already knows which subscription it registered,
and echoing our internal identifier back over the network would make it a
de-facto public identifier we must then keep stable forever.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from arion.notifications.eligibility import WEBHOOK_ELIGIBLE_EVENT_KINDS

#: External payload contract version. Bump on ANY breaking change to the
#: envelope or to a projection (ADR-059 D5, invariant 7).
WEBHOOK_SCHEMA_VERSION = "1"

#: Envelope keys, fixed by contract.
ENVELOPE_KEYS = (
    "schema_version",
    "delivery_id",
    "event_id",
    "event_kind",
    "occurred_at",
    "sequence",
    "payload",
)

#: Identity fields projected for every eligible kind when present. These are
#: opaque internal identifiers with no embedded semantics, which is what
#: makes them safe to publish.
_COMMON_FIELDS: tuple[str, ...] = ("task_id", "step_id", "success")

#: Per-kind detail allowlists (ADR-059 D5). Keys absent from an emitted
#: detail are simply omitted; keys present but unlisted are DROPPED.
#:
#: Note the consistent omissions: free-text reasons, model output, tool
#: arguments and error strings are not projected. A notification tells a
#: subscriber that something needs attention and how to find it; it is not a
#: replacement for authenticated access to the audit trail.
DETAIL_PROJECTIONS: dict[str, tuple[str, ...]] = {
    # --- Tier 1 -----------------------------------------------------------
    "approval.requested": ("request_id", "capability", "risk", "expires_at"),
    "approval.queued": ("request_id", "capability", "risk", "expires_at"),
    "approval.expired": ("request_id", "capability", "expires_at"),
    "goal.approval.pending": ("goal_id", "request_id", "capability", "expires_at"),
    "goal.approval.expired": ("goal_id", "request_id", "capability", "expires_at"),
    # --- Tier 2 (opt-in) --------------------------------------------------
    "approval.granted": ("request_id", "capability", "decided_by", "decided_at"),
    "approval.denied": ("request_id", "capability", "decided_by", "decided_at"),
    "goal.approval.granted": ("goal_id", "request_id", "capability", "decided_by"),
    "goal.approval.denied": ("goal_id", "request_id", "capability", "decided_by"),
    "goal.blocked": ("goal_id", "state", "blocked_reason"),
    "goal.unblocked": ("goal_id", "state"),
}

_MISSING = WEBHOOK_ELIGIBLE_EVENT_KINDS - set(DETAIL_PROJECTIONS)
if _MISSING:  # pragma: no cover - guards a source-level mistake
    raise RuntimeError(
        "eligible event kinds without an explicit payload projection: "
        + ", ".join(sorted(_MISSING))
    )


def project_detail(event_kind: str, detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project an internal event detail onto its published allowlist."""
    allowed = DETAIL_PROJECTIONS.get(event_kind)
    if not allowed or not detail:
        return {}
    projected: dict[str, Any] = {}
    for key in allowed:
        if key in detail:
            value = detail[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                projected[key] = value
            else:
                # Structured sub-objects are not part of the contract; render
                # them out rather than leaking an internal shape.
                continue
    return projected


def build_envelope(
    *,
    delivery_id: str,
    event_id: str,
    event_kind: str,
    occurred_at: str,
    sequence: int,
    task_id: str | None = None,
    step_id: str | None = None,
    success: bool | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the versioned external envelope for one delivery."""
    payload: dict[str, Any] = {}
    for name, value in (
        ("task_id", task_id),
        ("step_id", step_id),
        ("success", success),
    ):
        if name in _COMMON_FIELDS and value is not None:
            payload[name] = value
    payload.update(project_detail(event_kind, detail))

    return {
        "schema_version": WEBHOOK_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "event_id": event_id,
        "event_kind": event_kind,
        "occurred_at": occurred_at,
        "sequence": int(sequence),
        "payload": payload,
    }


def serialize_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Serialize an envelope to the exact bytes that will be signed and sent.

    Determinism matters: the signature is computed over these bytes and the
    body is frozen at enqueue time, so a retry months later - possibly after
    the projection code has changed - re-sends and re-signs byte-identical
    content (ADR-059 D5, invariant 8).
    """
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
