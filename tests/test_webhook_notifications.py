"""M6-B webhook notification tests (ADR-059).

Organised by the architectural claim each group defends, not by module.
"""

from __future__ import annotations

import json
import secrets

import pytest

from arion.notifications.config import (
    WebhookConfig,
    WebhookConfigError,
    load_webhook_config,
)
from arion.notifications.eligibility import (
    WEBHOOK_ELIGIBLE_EVENT_KINDS,
    is_eligible_kind,
    is_reserved_kind,
    validate_event_kinds,
)
from arion.notifications.models import (
    DeliveryStatus,
    SecretVersionStatus,
    WebhookStateError,
    WebhookSubscription,
    iso_plus,
    iso_plus_days,
    new_subscription_id,
)
from arion.notifications.outbox import WebhookOutboxSink
from arion.notifications.payload import (
    WEBHOOK_SCHEMA_VERSION,
    build_envelope,
    serialize_envelope,
)
from arion.notifications.transport import (
    FakeWebhookTransport,
    WebhookTransportError,
    compute_signature,
    validate_webhook_url,
)
from arion.notifications.worker import WebhookDeliveryWorker
from arion.observability.events import EVENT_KINDS, AuditEvent, EventLogger
from arion.state.models import utcnow
from arion.state.store import SQLiteStorage

ORIGIN = "https://hooks.example.com"
URL = f"{ORIGIN}/endpoint"

BASE_ENV = {
    "ARION_WEBHOOK_ENABLED": "1",
    "ARION_WEBHOOK_ALLOWED_ORIGINS": ORIGIN,
}


@pytest.fixture()
def config() -> WebhookConfig:
    return load_webhook_config(BASE_ENV)


@pytest.fixture()
def storage(tmp_path):
    store = SQLiteStorage(str(tmp_path / "wh.db"))
    yield store
    store.close()


def make_subscription(storage, *, kinds=("approval.requested",), secret=None, url=URL):
    sub = WebhookSubscription(
        subscription_id=new_subscription_id(),
        url=url,
        event_kinds=list(kinds),
        created_by="user:admin",
    )
    secret = secret or secrets.token_hex(16)
    storage.create_webhook_subscription(sub, secret)
    return sub, secret


def approval_event(**kw):
    detail = {
        "request_id": "req_1",
        "capability": "filesystem.write",
        "risk": "high",
        "expires_at": "2026-01-01T00:00:00+00:00",
        "internal_note": "should never be published",
    }
    detail.update(kw.pop("detail", {}))
    return AuditEvent(kind=kw.pop("kind", "approval.requested"), task_id="task_1", detail=detail, **kw)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_disabled_by_default():
    cfg = load_webhook_config({})
    assert cfg.enabled is False
    assert cfg.allowed_origins == frozenset()


def test_enabled_without_allowlist_is_rejected():
    with pytest.raises(WebhookConfigError, match="ALLOWED_ORIGINS"):
        load_webhook_config({"ARION_WEBHOOK_ENABLED": "1"})


def test_lease_must_exceed_timeout_by_margin():
    env = dict(BASE_ENV, ARION_WEBHOOK_TIMEOUT_SECONDS="30",
               ARION_WEBHOOK_LEASE_SECONDS="34")
    with pytest.raises(WebhookConfigError, match="LEASE_SECONDS"):
        load_webhook_config(env)
    ok = load_webhook_config(dict(env, ARION_WEBHOOK_LEASE_SECONDS="35"))
    assert ok.lease_seconds == 35


def test_unsafe_values_are_rejected_not_clamped():
    for var, value in [
        ("ARION_WEBHOOK_TIMEOUT_SECONDS", "0"),
        ("ARION_WEBHOOK_TIMEOUT_SECONDS", "-5"),
        ("ARION_WEBHOOK_MAX_ATTEMPTS", "0"),
        ("ARION_WEBHOOK_MAX_RESPONSE_BYTES", "abc"),
        ("ARION_WEBHOOK_MAX_SECRET_VERSIONS", "1"),
    ]:
        with pytest.raises(WebhookConfigError):
            load_webhook_config(dict(BASE_ENV, **{var: value}))


def test_retention_asymmetry_enforced():
    with pytest.raises(WebhookConfigError, match="RETENTION_FAILED_DAYS"):
        load_webhook_config(dict(BASE_ENV,
                                 ARION_WEBHOOK_RETENTION_DELIVERED_DAYS="30",
                                 ARION_WEBHOOK_RETENTION_FAILED_DAYS="7"))


def test_backoff_schedule_matches_adr_arithmetic(config):
    delays = [config.backoff_for_attempt(n) for n in range(1, 8)]
    assert delays == [5, 10, 20, 40, 80, 160, 320]
    assert sum(delays) == 635  # ADR-059 D10.1


def test_backoff_cap_binds_when_operator_raises_attempts():
    cfg = load_webhook_config(dict(BASE_ENV, ARION_WEBHOOK_MAX_ATTEMPTS="20"))
    assert cfg.backoff_for_attempt(11) == 3600.0
    assert cfg.backoff_for_attempt(500) == 3600.0


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #


def test_eligibility_is_a_strict_subset_of_event_kinds():
    assert WEBHOOK_ELIGIBLE_EVENT_KINDS < set(EVENT_KINDS)


def test_no_webhook_kinds_in_event_kinds():
    assert not [k for k in EVENT_KINDS if k.startswith("webhook.")]
    assert is_reserved_kind("webhook.delivered")
    assert not is_eligible_kind("webhook.delivered")


def test_deferred_kinds_are_not_eligible():
    for kind in ("task.completed", "task.failed", "recovery.required"):
        assert not is_eligible_kind(kind)


def test_wildcards_rejected():
    with pytest.raises(ValueError, match="wildcard"):
        validate_event_kinds(["*"])


def test_ineligible_kind_named_in_error():
    with pytest.raises(ValueError, match="task.completed"):
        validate_event_kinds(["approval.requested", "task.completed"])


def test_validate_normalizes_and_dedupes():
    assert validate_event_kinds(
        ["approval.queued", "approval.requested", "approval.queued"]
    ) == ["approval.queued", "approval.requested"]


# --------------------------------------------------------------------------- #
# payload
# --------------------------------------------------------------------------- #


def test_envelope_shape_and_absent_subscription_id():
    env = build_envelope(
        delivery_id="d1", event_id="e1", event_kind="approval.requested",
        occurred_at="2026-01-01T00:00:00+00:00", sequence=7,
        task_id="t1", detail={"request_id": "r1", "secret": "nope"},
    )
    assert set(env) == {
        "schema_version", "delivery_id", "event_id", "event_kind",
        "occurred_at", "sequence", "payload",
    }
    assert env["schema_version"] == WEBHOOK_SCHEMA_VERSION
    assert "subscription_id" not in json.dumps(env)


def test_unlisted_detail_keys_are_dropped():
    env = build_envelope(
        delivery_id="d", event_id="e", event_kind="approval.requested",
        occurred_at="t", sequence=1,
        detail={"request_id": "r1", "internal_note": "leak", "secret": "leak"},
    )
    assert env["payload"]["request_id"] == "r1"
    assert "internal_note" not in env["payload"]
    assert "secret" not in env["payload"]


def test_serialization_is_deterministic():
    env = build_envelope(delivery_id="d", event_id="e",
                         event_kind="goal.blocked", occurred_at="t",
                         sequence=2, detail={"goal_id": "g", "state": "blocked"})
    assert serialize_envelope(env) == serialize_envelope(dict(reversed(list(env.items()))))


def test_every_eligible_kind_has_a_projection():
    from arion.notifications.payload import DETAIL_PROJECTIONS

    assert WEBHOOK_ELIGIBLE_EVENT_KINDS <= set(DETAIL_PROJECTIONS)


# --------------------------------------------------------------------------- #
# capture / outbox
# --------------------------------------------------------------------------- #


def test_capture_creates_one_row_per_matching_subscription(storage, config):
    a, _ = make_subscription(storage)
    b, _ = make_subscription(storage)
    make_subscription(storage, kinds=("goal.blocked",))

    WebhookOutboxSink(storage, config).handle(approval_event())

    rows = storage.list_webhook_deliveries()
    assert {r.subscription_id for r in rows} == {a.subscription_id, b.subscription_id}
    assert all(r.status is DeliveryStatus.PENDING for r in rows)


def test_ineligible_event_captures_nothing(storage, config):
    make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(
        AuditEvent(kind="task.completed", task_id="t")
    )
    assert storage.list_webhook_deliveries() == []


def test_disabled_config_captures_nothing(storage):
    make_subscription(storage)
    WebhookOutboxSink(storage, load_webhook_config({})).handle(approval_event())
    assert storage.list_webhook_deliveries() == []


def test_disabled_subscription_captures_nothing(storage, config):
    sub, _ = make_subscription(storage)
    storage.update_webhook_subscription(sub.subscription_id, {"enabled": False})
    WebhookOutboxSink(storage, config).handle(approval_event())
    assert storage.list_webhook_deliveries() == []


def test_sequence_is_monotonic(storage, config):
    make_subscription(storage)
    sink = WebhookOutboxSink(storage, config)
    for _ in range(3):
        sink.handle(approval_event())
    seqs = [d.sequence for d in storage.list_webhook_deliveries()]
    assert seqs == sorted(seqs) == [1, 2, 3]


def test_body_is_frozen_and_matches_its_own_sequence(storage, config):
    make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    d = storage.list_webhook_deliveries()[0]
    assert json.loads(d.body_bytes)["sequence"] == d.sequence
    assert json.loads(d.body_bytes)["delivery_id"] == d.delivery_id


def test_capture_failure_never_breaks_the_emitter(storage, config):
    """The whole point of required=False (ADR-059 D1)."""

    class Exploding(WebhookOutboxSink):
        def handle(self, event):  # noqa: D102
            raise RuntimeError("subscriber storage exploded")

    logger = EventLogger()
    logger.add_sink(storage)
    logger.add_sink(Exploding(storage, config), required=False)
    logger.emit(approval_event())  # must not raise
    assert logger.last_failures


# --------------------------------------------------------------------------- #
# delivery worker
# --------------------------------------------------------------------------- #


def enqueue_one(storage, config):
    sub, secret = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    return sub, secret, storage.list_webhook_deliveries()[0]


def test_successful_delivery(storage, config):
    _, secret, delivery = enqueue_one(storage, config)
    transport = FakeWebhookTransport(status_code=200)
    worker = WebhookDeliveryWorker(storage, config, transport)

    assert worker.run_once() is True
    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.DELIVERED
    assert after.attempts == 1
    assert after.lease_owner is None


def test_signature_is_over_exactly_the_transmitted_bytes(storage, config):
    _, secret, delivery = enqueue_one(storage, config)
    transport = FakeWebhookTransport()
    WebhookDeliveryWorker(storage, config, transport).run_once()

    call = transport.calls[0]
    assert call["body"] == delivery.body_bytes
    assert call["headers"]["X-Arion-Signature"] == compute_signature(
        secret, call["body"]
    )
    assert call["headers"]["X-Arion-Signature-Version"] == "1"


def test_no_secret_material_in_headers_or_body(storage, config):
    _, secret, _ = enqueue_one(storage, config)
    transport = FakeWebhookTransport()
    WebhookDeliveryWorker(storage, config, transport).run_once()
    call = transport.calls[0]
    assert secret not in json.dumps(call["headers"])
    assert secret.encode() not in call["body"]


@pytest.mark.parametrize("status_code", [500, 502, 503, 429])
def test_transient_status_reschedules(storage, config, status_code):
    _, _, delivery = enqueue_one(storage, config)
    worker = WebhookDeliveryWorker(
        storage, config, FakeWebhookTransport(status_code=status_code)
    )
    worker.run_once()
    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.PENDING
    assert after.attempts == 1
    assert after.next_attempt_at > after.created_at


@pytest.mark.parametrize("status_code", [400, 401, 404, 410, 422])
def test_permanent_4xx_dead_letters_immediately(storage, config, status_code):
    _, _, delivery = enqueue_one(storage, config)
    WebhookDeliveryWorker(
        storage, config, FakeWebhookTransport(status_code=status_code)
    ).run_once()
    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.DEAD_LETTER
    assert after.attempts == 1


def test_redirect_is_never_followed(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    transport = FakeWebhookTransport(status_code=302)
    WebhookDeliveryWorker(storage, config, transport).run_once()
    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.DEAD_LETTER
    assert "redirect" in after.last_error


def test_retry_budget_exhaustion_ends_in_failed_not_dead_letter(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    worker = WebhookDeliveryWorker(
        storage, config, FakeWebhookTransport(status_code=503)
    )
    for _ in range(config.max_attempts + 2):
        # next_attempt_at is in the future, so drive it directly.
        storage.reschedule_webhook_delivery  # noqa: B018
        row = storage.get_webhook_delivery(delivery.delivery_id)
        if row.status is DeliveryStatus.FAILED:
            break
        storage._conn.execute(  # force the backoff deadline to be due
            "UPDATE webhook_deliveries SET next_attempt_at=? WHERE delivery_id=?",
            (utcnow(), delivery.delivery_id),
        )
        storage._conn.commit()
        worker.run_once()

    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.FAILED
    assert after.attempts == config.max_attempts
    assert after.retry_eligible_until is not None


def test_transport_error_is_retried(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    transport = FakeWebhookTransport(
        error=WebhookTransportError("connection reset", retryable=True)
    )
    WebhookDeliveryWorker(storage, config, transport).run_once()
    assert storage.get_webhook_delivery(delivery.delivery_id).status is (
        DeliveryStatus.PENDING
    )


def test_non_retryable_transport_error_dead_letters(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    transport = FakeWebhookTransport(
        error=WebhookTransportError("resolves to a non-public address",
                                    retryable=False)
    )
    WebhookDeliveryWorker(storage, config, transport).run_once()
    assert storage.get_webhook_delivery(delivery.delivery_id).status is (
        DeliveryStatus.DEAD_LETTER
    )


def test_worker_idles_when_nothing_is_due(storage, config):
    assert WebhookDeliveryWorker(storage, config, FakeWebhookTransport()).run_once() is False


def test_destination_revoked_after_creation_is_dead_lettered(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    tightened = load_webhook_config(
        dict(BASE_ENV, ARION_WEBHOOK_ALLOWED_ORIGINS="https://other.example.com")
    )
    transport = FakeWebhookTransport()
    WebhookDeliveryWorker(storage, tightened, transport).run_once()
    assert transport.calls == []  # never egressed
    assert storage.get_webhook_delivery(delivery.delivery_id).status is (
        DeliveryStatus.DEAD_LETTER
    )


# --------------------------------------------------------------------------- #
# lease / fencing / concurrency
# --------------------------------------------------------------------------- #


def test_claim_is_exclusive(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    first = storage.claim_next_webhook_delivery("w1", 60)
    second = storage.claim_next_webhook_delivery("w2", 60)
    assert first.delivery_id == delivery.delivery_id
    assert second is None


def test_non_owner_cannot_finalize(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    storage.claim_next_webhook_delivery("w1", 60)
    assert storage.finalize_webhook_delivery(
        delivery.delivery_id, "w2", DeliveryStatus.DELIVERED
    ) is False
    assert storage.get_webhook_delivery(delivery.delivery_id).status is (
        DeliveryStatus.DELIVERING
    )


def test_expired_lease_is_reclaimed_then_finalize_is_fenced_out(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    now = utcnow()
    storage.claim_next_webhook_delivery("w1", 1, now=now)

    later = iso_plus(now, 120)
    assert storage.reclaim_stale_webhook_deliveries(now=later) == [delivery.delivery_id]
    assert storage.get_webhook_delivery(delivery.delivery_id).status is (
        DeliveryStatus.PENDING
    )
    # The zombie worker's late finalize must be rejected (invariant 10).
    assert storage.finalize_webhook_delivery(
        delivery.delivery_id, "w1", DeliveryStatus.DELIVERED, now=later
    ) is False


def test_reclaimed_delivery_is_reclaimable_by_another_worker(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    now = utcnow()
    storage.claim_next_webhook_delivery("w1", 1, now=now)
    later = iso_plus(now, 120)
    storage.reclaim_stale_webhook_deliveries(now=later)
    claimed = storage.claim_next_webhook_delivery("w2", 60, now=later)
    assert claimed is not None and claimed.lease_owner == "w2"


# --------------------------------------------------------------------------- #
# secrets: rotation, retirement, manual retry
# --------------------------------------------------------------------------- #


def test_rotation_retires_previous_version_without_destroying_it(storage, config):
    sub, first = make_subscription(storage)
    version = storage.rotate_webhook_secret(
        sub.subscription_id, secrets.token_hex(16), max_live_versions=8
    )
    assert version == 2
    v1 = storage.get_webhook_secret_version(sub.subscription_id, 1)
    assert v1.status is SecretVersionStatus.RETIRING
    assert v1.secret == first  # material preserved


def test_new_deliveries_use_the_new_active_version(storage, config):
    sub, _ = make_subscription(storage)
    storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                  max_live_versions=8)
    WebhookOutboxSink(storage, config).handle(approval_event())
    assert storage.list_webhook_deliveries()[0].secret_version == 2


def test_in_flight_delivery_keeps_signing_with_its_own_version(storage, config):
    sub, v1_secret = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                  max_live_versions=8)

    transport = FakeWebhookTransport()
    WebhookDeliveryWorker(storage, config, transport).run_once()
    call = transport.calls[0]
    assert call["headers"]["X-Arion-Signature-Version"] == "1"
    assert call["headers"]["X-Arion-Signature"] == compute_signature(
        v1_secret, call["body"]
    )


def test_rotation_blocked_at_version_limit_names_clearing_time(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(
        delivery.delivery_id, "w1", DeliveryStatus.FAILED,
        retry_window_days=config.retention_failed_days,
    )
    for _ in range(2):
        storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                      max_live_versions=3)
    with pytest.raises(WebhookStateError) as exc:
        storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                      max_live_versions=3)
    assert "rotation blocked" in str(exc.value)
    assert "earliest" in str(exc.value)


def test_secret_is_retained_while_a_delivery_can_still_be_retried(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(
        delivery.delivery_id, "w1", DeliveryStatus.FAILED,
        retry_window_days=config.retention_failed_days,
    )
    storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                  max_live_versions=8)

    assert storage.count_retry_capable_deliveries_for_secret(sub.subscription_id, 1) == 1
    assert storage.retire_webhook_secret_versions() == 0
    assert storage.get_webhook_secret_version(sub.subscription_id, 1).secret is not None


def test_secret_material_destroyed_once_no_retryable_reference_remains(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.DELIVERED)
    storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                  max_live_versions=8)

    assert storage.retire_webhook_secret_versions() == 1
    v1 = storage.get_webhook_secret_version(sub.subscription_id, 1)
    assert v1.status is SecretVersionStatus.RETIRED
    assert v1.secret is None
    assert v1.retired_at is not None  # record kept for audit


def test_manual_retry_resets_attempts_and_reuses_frozen_body(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(
        delivery.delivery_id, "w1", DeliveryStatus.FAILED,
        retry_window_days=config.retention_failed_days,
    )

    assert storage.manual_retry_webhook_delivery(delivery.delivery_id) is True
    after = storage.get_webhook_delivery(delivery.delivery_id)
    assert after.status is DeliveryStatus.PENDING
    assert after.attempts == 0
    assert after.body_bytes == delivery.body_bytes
    assert after.secret_version == delivery.secret_version


def test_manual_retry_refused_after_horizon(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(
        delivery.delivery_id, "w1", DeliveryStatus.FAILED, retry_window_days=1,
    )
    beyond = iso_plus_days(utcnow(), 2)
    assert storage.manual_retry_webhook_delivery(delivery.delivery_id,
                                                 now=beyond) is False


def test_delivered_delivery_cannot_be_manually_retried(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.DELIVERED)
    assert storage.manual_retry_webhook_delivery(delivery.delivery_id) is False


def test_secret_never_appears_in_repr_or_public_dict(storage):
    sub, secret = make_subscription(storage)
    version = storage.get_webhook_secret_version(sub.subscription_id, 1)
    assert secret not in repr(version)
    assert "secret" not in version.public_dict()
    assert secret not in json.dumps(sub.public_dict())


# --------------------------------------------------------------------------- #
# deletion semantics and retention
# --------------------------------------------------------------------------- #


def test_deletion_cancels_pending_retains_history_and_keeps_secrets(storage, config):
    sub, _ = make_subscription(storage)
    sink = WebhookOutboxSink(storage, config)
    sink.handle(approval_event())
    pending = storage.list_webhook_deliveries()[0]
    sink.handle(approval_event())
    done = storage.list_webhook_deliveries()[1]
    storage.claim_next_webhook_delivery("w1", 60)  # claims the first
    storage.finalize_webhook_delivery(pending.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)

    assert storage.delete_webhook_subscription(sub.subscription_id) is True

    assert storage.get_webhook_subscription(sub.subscription_id) is None
    assert storage.get_webhook_delivery(done.delivery_id).status is (
        DeliveryStatus.CANCELLED
    )
    # Nothing destroyed (invariant 24).
    v1 = storage.get_webhook_secret_version(sub.subscription_id, 1)
    assert v1.status is SecretVersionStatus.RETIRING
    assert v1.secret is not None
    # History still queryable (invariant 26).
    assert len(storage.list_webhook_deliveries(subscription_id=sub.subscription_id)) == 2


def test_manual_retry_still_possible_after_subscription_deletion(storage, config):
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)
    storage.delete_webhook_subscription(sub.subscription_id)

    assert storage.manual_retry_webhook_delivery(delivery.delivery_id) is True


def test_prune_never_removes_retryable_or_non_terminal_rows(storage, config):
    sub, _ = make_subscription(storage)
    sink = WebhookOutboxSink(storage, config)
    sink.handle(approval_event())
    pending = storage.list_webhook_deliveries()[0]
    sink.handle(approval_event())
    failed = storage.list_webhook_deliveries()[1]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(pending.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)

    removed = storage.prune_webhook_deliveries(
        delivered_retention_days=7, failed_retention_days=30
    )
    assert removed == 0
    assert storage.get_webhook_delivery(pending.delivery_id) is not None
    assert storage.get_webhook_delivery(failed.delivery_id) is not None


def test_prune_removes_expired_delivered_rows(storage, config):
    _, _, delivery = enqueue_one(storage, config)
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.DELIVERED)
    future = iso_plus_days(utcnow(), 40)
    assert storage.prune_webhook_deliveries(
        delivered_retention_days=7, failed_retention_days=30, now=future
    ) == 1
    assert storage.get_webhook_delivery(delivery.delivery_id) is None


def test_prune_then_retire_ordering_releases_the_secret(storage, config):
    """Maintenance order matters (ADR-059 D11.3): prune, THEN retire."""
    sub, _ = make_subscription(storage)
    WebhookOutboxSink(storage, config).handle(approval_event())
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)
    storage.rotate_webhook_secret(sub.subscription_id, secrets.token_hex(16),
                                  max_live_versions=8)

    # Inside the retry window nothing may be pruned and nothing may be
    # retired: the delivery still has retry capability.
    inside = iso_plus_days(utcnow(), 10)
    assert storage.prune_webhook_deliveries(
        delivered_retention_days=7, failed_retention_days=30, now=inside) == 0
    assert storage.retire_webhook_secret_versions(now=inside) == 0

    # Past the horizon: prune first, then retire, and the secret is released.
    future = iso_plus_days(utcnow(), 60)
    assert storage.prune_webhook_deliveries(
        delivered_retention_days=7, failed_retention_days=30, now=future) == 1
    assert storage.retire_webhook_secret_versions(now=future) == 1
    assert storage.get_webhook_secret_version(sub.subscription_id, 1).secret is None


# --------------------------------------------------------------------------- #
# transport policy (SSRF)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",          # plaintext, no exception
    "http://localhost:9000/x",
    "https://192.168.1.10/x",              # literal IP
    "https://127.0.0.1/x",
    "https://evil.example.com/x",          # not allowlisted
    "https://user:pw@hooks.example.com/x",  # embedded credentials
    "ftp://hooks.example.com/x",
    "",
])
def test_rejected_destinations(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url, {ORIGIN})


def test_allowlisted_https_destination_accepted():
    assert validate_webhook_url(URL, {ORIGIN}) == ORIGIN


def test_origin_comparison_includes_port():
    with pytest.raises(ValueError):
        validate_webhook_url("https://hooks.example.com:8443/x", {ORIGIN})
