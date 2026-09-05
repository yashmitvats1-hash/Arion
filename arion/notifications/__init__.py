"""Durable webhook notifications and event delivery (ADR-059, M6-B).

Infrastructure-originated outbound notification. This package is NEVER an
agent capability: nothing here is registered in the CapabilityRegistry, is
plannable by a model, or passes through the authorization seam. Webhook
delivery has no Task, no PlanStep and no Actor.

Layering (ADR-059 D2):

    EventLogger.emit()
      -> WebhookOutboxSink        (required=False, enqueue only, no I/O)
      -> webhook_deliveries       (durable SQLite rows)
      -> WebhookDeliveryWorker    (daemon thread, leased claim/fence/reclaim)
      -> WebhookTransport         (dedicated infrastructure HTTPS POST)
      -> external endpoint

Guarantee (ADR-059 D1): best-effort event capture, then AT-LEAST-ONCE
delivery for durably captured deliveries. Duplicates are expected; ordering
is NOT guaranteed.
"""

from __future__ import annotations

from arion.notifications.config import (
    WebhookConfig,
    WebhookConfigError,
    load_webhook_config,
)
from arion.notifications.eligibility import (
    WEBHOOK_ELIGIBLE_EVENT_KINDS,
    WEBHOOK_RESERVED_KIND_PREFIX,
    is_eligible_kind,
    is_reserved_kind,
)
from arion.notifications.models import (
    DeliveryStatus,
    SecretVersionStatus,
    WebhookDelivery,
    WebhookSecretVersion,
    WebhookStateError,
    WebhookSubscription,
    legal_delivery_transition,
)
from arion.notifications.outbox import WebhookOutboxSink
from arion.notifications.payload import (
    WEBHOOK_SCHEMA_VERSION,
    build_envelope,
    serialize_envelope,
)
from arion.notifications.transport import (
    FakeWebhookTransport,
    StdlibWebhookTransport,
    WebhookResponse,
    WebhookTransport,
    WebhookTransportError,
)
from arion.notifications.worker import WebhookDeliveryWorker

__all__ = [
    "DeliveryStatus",
    "FakeWebhookTransport",
    "SecretVersionStatus",
    "StdlibWebhookTransport",
    "WEBHOOK_ELIGIBLE_EVENT_KINDS",
    "WEBHOOK_RESERVED_KIND_PREFIX",
    "WEBHOOK_SCHEMA_VERSION",
    "WebhookConfig",
    "WebhookConfigError",
    "WebhookDelivery",
    "WebhookDeliveryWorker",
    "WebhookOutboxSink",
    "WebhookResponse",
    "WebhookSecretVersion",
    "WebhookStateError",
    "WebhookSubscription",
    "WebhookTransport",
    "WebhookTransportError",
    "build_envelope",
    "is_eligible_kind",
    "is_reserved_kind",
    "legal_delivery_transition",
    "load_webhook_config",
    "serialize_envelope",
]
