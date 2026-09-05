"""Outbox capture sink (ADR-059 D2/D3).

`WebhookOutboxSink` is an `EventSink` registered with `required=False`. That
single word carries the central guarantee of ADR-059 D1: notification is
downstream of orchestration and can never fail it. If capture raises, the
EventLogger isolates the failure and records a `SinkFailure` diagnostic;
the task proceeds. We accept losing a notification; we do not accept a
webhook subscriber breaking the agent.

Consequences that are deliberate, not oversights:

  * Capture is BEST-EFFORT. There is no transactional coupling to the
    caller's transaction (ADR-059 D2) - `append_event` already commits
    outside caller transactions, so pretending otherwise would be a lie.
  * Once a row IS committed here, delivery becomes AT-LEAST-ONCE.
  * `handle()` does no network I/O and never blocks on the delivery worker
    (invariant 3). It performs one bounded INSERT and returns.
"""

from __future__ import annotations

from arion.notifications.eligibility import is_eligible_kind, is_reserved_kind
from arion.notifications.models import (
    DeliveryStatus,
    WebhookDelivery,
    iso_plus_days,
    new_delivery_id,
)
from arion.notifications.payload import build_envelope, serialize_envelope
from arion.observability.events import AuditEvent
from arion.state.models import utcnow


class WebhookOutboxSink:
    """Fan an eligible AuditEvent out to one durable row per subscription."""

    def __init__(self, storage: Any, config: Any) -> None:
        self._storage = storage
        self._config = config

    # -- EventSink ---------------------------------------------------------

    def handle(self, event: AuditEvent) -> None:
        """Capture `event` as zero or more durable delivery intents."""
        if not self._config.enabled:
            return
        kind = event.kind
        # Structural guard first: a webhook.* kind must never be deliverable
        # even if it somehow reached eligibility (ADR-059 D4/D7).
        if is_reserved_kind(kind) or not is_eligible_kind(kind):
            return

        subscriptions = [
            sub
            for sub in self._storage.list_webhook_subscriptions(
                enabled_only=True, include_deleted=False
            )
            if kind in set(sub.event_kinds)
        ]
        if not subscriptions:
            return

        now = utcnow()
        # The manual-retry horizon is stamped at ENQUEUE so it is a stable
        # persisted fact (ADR-059 D11.3) rather than something recomputed
        # from mutable configuration at read time.
        horizon = iso_plus_days(now, self._config.retention_failed_days)

        deliveries: list[WebhookDelivery] = []
        for sub in subscriptions:
            deliveries.append(
                WebhookDelivery(
                    delivery_id=new_delivery_id(),
                    subscription_id=sub.subscription_id,
                    event_id=event.id,
                    event_kind=kind,
                    occurred_at=event.ts,
                    sequence=0,  # assigned inside the enqueue transaction
                    secret_version=int(sub.active_secret_version),
                    body_bytes=b"",  # frozen inside the enqueue transaction
                    url=sub.url,
                    status=DeliveryStatus.PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    retry_eligible_until=horizon,
                    created_at=now,
                    updated_at=now,
                )
            )

        def freeze(delivery: WebhookDelivery) -> bytes:
            """Serialize the exact bytes that will be stored, signed and sent.

            Built inside the enqueue transaction because the envelope embeds
            `sequence`, which is allocated there (ADR-059 D6). Freezing here
            keeps the stored body, its sequence and its signature mutually
            consistent for the row's entire life (invariant 18).
            """
            return serialize_envelope(
                build_envelope(
                    delivery_id=delivery.delivery_id,
                    event_id=delivery.event_id,
                    event_kind=delivery.event_kind,
                    occurred_at=delivery.occurred_at,
                    sequence=delivery.sequence,
                    task_id=event.task_id,
                    step_id=event.step_id,
                    success=event.success,
                    detail=event.detail,
                )
            )

        self._storage.enqueue_webhook_deliveries(deliveries, freeze)

    def close(self) -> None:  # pragma: no cover - lifecycle symmetry
        """No resources of its own; the worker owns the delivery lifecycle."""
        return None
