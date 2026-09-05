"""M6-B webhook administration API tests (ADR-059 D13/D15)."""

from __future__ import annotations

import json
import secrets

import pytest

from arion.interfaces.api_authz import (
    APIConfigError,
    Privilege,
    TokenRegistry,
    authorize,
)
from arion.interfaces.webhook_api import WebhookAPI
from arion.notifications.config import load_webhook_config
from arion.notifications.models import DeliveryStatus
from arion.notifications.outbox import WebhookOutboxSink
from arion.observability.events import AuditEvent
from arion.state.store import SQLiteStorage

ORIGIN = "https://hooks.example.com"
URL = f"{ORIGIN}/endpoint"
ENV = {"ARION_WEBHOOK_ENABLED": "1", "ARION_WEBHOOK_ALLOWED_ORIGINS": ORIGIN}

ADMIN = "Bearer admintok"
APPROVER = "Bearer usertok"


@pytest.fixture()
def storage(tmp_path):
    store = SQLiteStorage(str(tmp_path / "api.db"))
    yield store
    store.close()


@pytest.fixture()
def registry():
    return TokenRegistry.from_env(
        {
            "ARION_API_TOKENS": "usertok:user:alice",
            "ARION_API_ADMIN_TOKENS": "admintok:user:root",
        }
    )


@pytest.fixture()
def api(storage, registry):
    return WebhookAPI(storage, load_webhook_config(ENV), registry,
                      secret_factory=lambda: "s" * 64)


def create(api, **overrides):
    body = {"url": URL, "event_kinds": ["approval.requested"]}
    body.update(overrides)
    return api.handle("POST", "/api/v1/webhooks", ADMIN,
                      json.dumps(body).encode())


# --------------------------------------------------------------------------- #
# authorization
# --------------------------------------------------------------------------- #


def test_admin_implies_approver(registry):
    assert authorize(registry, ADMIN, Privilege.APPROVER).ok
    assert authorize(registry, ADMIN, Privilege.ADMIN).ok


def test_approver_is_not_admin(registry):
    assert authorize(registry, APPROVER, Privilege.APPROVER).ok
    decision = authorize(registry, APPROVER, Privilege.ADMIN)
    assert decision.status == 403  # authenticated but not permitted


def test_unknown_token_is_401(registry):
    assert authorize(registry, "Bearer nope", Privilege.APPROVER).status == 401
    assert authorize(registry, None, Privilege.ADMIN).status == 401
    assert authorize(registry, "Basic abc", Privilege.ADMIN).status == 401


def test_empty_admin_map_fails_closed():
    registry = TokenRegistry.from_env({"ARION_API_TOKENS": "usertok:user:alice"})
    assert authorize(registry, APPROVER, Privilege.ADMIN).status == 403


def test_token_in_both_surfaces_is_a_configuration_error():
    with pytest.raises(APIConfigError, match="exactly one privilege"):
        TokenRegistry.from_env(
            {"ARION_API_TOKENS": "t:user:a", "ARION_API_ADMIN_TOKENS": "t:user:b"}
        )


def test_approver_token_grammar_is_unchanged():
    reg = TokenRegistry.from_env({"ARION_API_TOKENS": "t1:user:alice,t2:agent:bot"})
    ctx = reg.authenticate("Bearer t2")
    assert ctx.actor.kind == "agent" and ctx.actor.name == "bot"
    assert ctx.privilege is Privilege.APPROVER


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/webhooks"),
    ("POST", "/api/v1/webhooks"),
    ("GET", "/api/v1/webhooks/whs_x"),
    ("PATCH", "/api/v1/webhooks/whs_x"),
    ("DELETE", "/api/v1/webhooks/whs_x"),
    ("POST", "/api/v1/webhooks/whs_x/secret"),
    ("GET", "/api/v1/webhooks/whs_x/deliveries"),
    ("GET", "/api/v1/deliveries/whd_x"),
    ("POST", "/api/v1/deliveries/whd_x/retry"),
])
def test_every_route_requires_admin(api, method, path):
    status, _ = api.handle(method, path, APPROVER, b"{}")
    assert status == 403


def test_disabled_config_returns_503(storage, registry):
    api = WebhookAPI(storage, load_webhook_config({}), registry)
    status, body = api.handle("GET", "/api/v1/webhooks", ADMIN, None)
    assert status == 503
    assert "disabled" in body["error"]


# --------------------------------------------------------------------------- #
# subscription CRUD
# --------------------------------------------------------------------------- #


def test_create_returns_secret_exactly_once(api):
    status, body = create(api)
    assert status == 201
    assert body["secret"] == "s" * 64
    assert body["secret_version"] == 1

    status, fetched = api.handle(
        "GET", f"/api/v1/webhooks/{body['subscription_id']}", ADMIN, None
    )
    assert status == 200
    assert "secret" not in fetched
    assert "s" * 64 not in json.dumps(fetched)  # material, not metadata

    _, listed = api.handle("GET", "/api/v1/webhooks", ADMIN, None)
    assert "s" * 64 not in json.dumps(listed)


def test_create_records_the_admin_actor(api):
    _, body = create(api)
    assert body["created_by"] == "user:root"


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",
    "https://evil.example.com/x",
    "https://10.0.0.1/x",
])
def test_create_rejects_disallowed_destination(api, url):
    status, body = create(api, url=url)
    assert status == 400
    assert "error" in body


def test_create_rejects_ineligible_kind(api):
    status, body = create(api, event_kinds=["task.completed"])
    assert status == 400
    assert "task.completed" in body["error"]


def test_create_rejects_wildcard(api):
    status, body = create(api, event_kinds=["*"])
    assert status == 400 and "wildcard" in body["error"]


def test_create_rejects_empty_kinds(api):
    assert create(api, event_kinds=[])[0] == 400


def test_create_rejects_malformed_json(api):
    assert api.handle("POST", "/api/v1/webhooks", ADMIN, b"{not json")[0] == 400
    assert api.handle("POST", "/api/v1/webhooks", ADMIN, b"[1,2]")[0] == 400


def test_create_rejects_oversized_body(api):
    assert api.handle("POST", "/api/v1/webhooks", ADMIN, b"x" * 9000)[0] == 413


def test_patch_updates_and_validates(api):
    _, created = create(api)
    sid = created["subscription_id"]

    status, body = api.handle("PATCH", f"/api/v1/webhooks/{sid}", ADMIN,
                              json.dumps({"enabled": False}).encode())
    assert status == 200 and body["enabled"] is False

    assert api.handle("PATCH", f"/api/v1/webhooks/{sid}", ADMIN,
                      json.dumps({"url": "http://x.example.com"}).encode())[0] == 400
    assert api.handle("PATCH", f"/api/v1/webhooks/{sid}", ADMIN,
                      json.dumps({"event_kinds": ["task.failed"]}).encode())[0] == 400
    assert api.handle("PATCH", f"/api/v1/webhooks/{sid}", ADMIN,
                      json.dumps({}).encode())[0] == 400


def test_patch_cannot_widen_security_bounds(api):
    """Bounds are configuration-only (ADR-059 D14, invariant 14)."""
    _, created = create(api)
    sid = created["subscription_id"]
    for field in ("timeout_seconds", "max_attempts", "max_response_bytes",
                  "allowed_origins", "secret", "active_secret_version"):
        status, body = api.handle(
            "PATCH", f"/api/v1/webhooks/{sid}", ADMIN,
            json.dumps({field: 9999}).encode(),
        )
        assert status == 400
        assert field in body["error"]


def test_unknown_subscription_is_404(api):
    assert api.handle("GET", "/api/v1/webhooks/whs_missing", ADMIN, None)[0] == 404
    assert api.handle("PATCH", "/api/v1/webhooks/whs_missing", ADMIN,
                      json.dumps({"enabled": True}).encode())[0] == 404
    assert api.handle("DELETE", "/api/v1/webhooks/whs_missing", ADMIN, None)[0] == 404


def test_method_not_allowed(api):
    _, created = create(api)
    sid = created["subscription_id"]
    assert api.handle("DELETE", "/api/v1/webhooks", ADMIN, None)[0] == 405
    assert api.handle("GET", f"/api/v1/webhooks/{sid}/secret", ADMIN, None)[0] == 405


def test_unknown_route_is_404(api):
    assert api.handle("GET", "/api/v1/webhooks/a/b/c/d", ADMIN, None)[0] == 404
    assert api.handle("GET", "/api/v2/webhooks", ADMIN, None)[0] == 404


def test_exact_route_matching_not_prefix_matching(api):
    """M6-A used startswith + split('/')[-1]; this surface must not."""
    _, created = create(api)
    sid = created["subscription_id"]
    # A trailing segment must not be mistaken for the subscription id.
    assert api.handle("GET", f"/api/v1/webhooks/{sid}/bogus", ADMIN, None)[0] == 404
    # A trailing slash must resolve to the same resource, not an empty id.
    assert api.handle("GET", f"/api/v1/webhooks/{sid}/", ADMIN, None)[0] == 200


# --------------------------------------------------------------------------- #
# pagination and query validation
# --------------------------------------------------------------------------- #


def test_listing_is_paginated(api):
    for _ in range(5):
        create(api)
    status, body = api.handle("GET", "/api/v1/webhooks?limit=2", ADMIN, None)
    assert status == 200
    assert len(body["subscriptions"]) == 2
    assert body["next_after"] is not None

    _, page2 = api.handle(
        f"GET", f"/api/v1/webhooks?limit=2&after={body['next_after']}", ADMIN, None
    )
    ids1 = {s["subscription_id"] for s in body["subscriptions"]}
    ids2 = {s["subscription_id"] for s in page2["subscriptions"]}
    assert not (ids1 & ids2)


def test_invalid_pagination_is_rejected(api):
    for query in ("limit=0", "limit=-1", "limit=abc", "limit=100000"):
        status, body = api.handle(f"GET", f"/api/v1/webhooks?{query}", ADMIN, None)
        assert status == 400, query


def test_invalid_status_filter_is_rejected(api):
    _, created = create(api)
    sid = created["subscription_id"]
    status, body = api.handle(
        "GET", f"/api/v1/webhooks/{sid}/deliveries?status=bogus", ADMIN, None
    )
    assert status == 400
    assert "dead_letter" in body["error"]


def test_listing_advertises_eligible_kinds(api):
    _, body = api.handle("GET", "/api/v1/webhooks", ADMIN, None)
    assert "approval.requested" in body["eligible_event_kinds"]
    assert "task.completed" not in body["eligible_event_kinds"]


# --------------------------------------------------------------------------- #
# secret rotation
# --------------------------------------------------------------------------- #


def test_rotate_returns_new_secret_and_version(storage, registry):
    versions = iter(["a" * 64, "b" * 64])
    api = WebhookAPI(storage, load_webhook_config(ENV), registry,
                     secret_factory=lambda: next(versions))
    _, created = create(api)
    sid = created["subscription_id"]

    status, body = api.handle("POST", f"/api/v1/webhooks/{sid}/secret", ADMIN, None)
    assert status == 200
    assert body["secret"] == "b" * 64
    assert body["secret_version"] == 2


def test_rotate_unknown_subscription_is_409(api):
    status, _ = api.handle("POST", "/api/v1/webhooks/whs_missing/secret", ADMIN, None)
    assert status == 409


# --------------------------------------------------------------------------- #
# deliveries, deletion semantics (ADR-059 D15.4)
# --------------------------------------------------------------------------- #


def enqueue(storage, config):
    WebhookOutboxSink(storage, config).handle(
        AuditEvent(kind="approval.requested", task_id="t",
                   detail={"request_id": "r", "capability": "c"})
    )


def test_delivery_projection_excludes_body_bytes(api, storage):
    create(api)
    enqueue(storage, load_webhook_config(ENV))
    delivery = storage.list_webhook_deliveries()[0]

    status, body = api.handle(
        "GET", f"/api/v1/deliveries/{delivery.delivery_id}", ADMIN, None
    )
    assert status == 200
    assert "body_bytes" not in body
    assert "body" not in body
    assert body["delivery_id"] == delivery.delivery_id


def test_history_remains_queryable_after_deletion(api, storage):
    _, created = create(api)
    sid = created["subscription_id"]
    enqueue(storage, load_webhook_config(ENV))

    api.handle("DELETE", f"/api/v1/webhooks/{sid}", ADMIN, None)

    status, body = api.handle("GET", f"/api/v1/webhooks/{sid}/deliveries", ADMIN, None)
    assert status == 200
    assert body["subscription_exists"] is False
    assert len(body["deliveries"]) == 1


def test_delete_response_states_what_survived(api, storage):
    _, created = create(api)
    sid = created["subscription_id"]
    enqueue(storage, load_webhook_config(ENV))
    status, body = api.handle("DELETE", f"/api/v1/webhooks/{sid}", ADMIN, None)
    assert status == 200
    assert body["pending_deliveries_cancelled"] is True
    assert body["delivery_history_retained"] is True


def test_never_existed_and_fully_pruned_are_indistinguishable(api):
    status, _ = api.handle("GET", "/api/v1/webhooks/whs_nope/deliveries", ADMIN, None)
    assert status == 404


def test_manual_retry_endpoint(api, storage):
    create(api)
    config = load_webhook_config(ENV)
    enqueue(storage, config)
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)

    status, body = api.handle(
        "POST", f"/api/v1/deliveries/{delivery.delivery_id}/retry", ADMIN, None
    )
    assert status == 200
    assert body["status"] == "pending"
    assert body["attempts"] == 0


def test_retry_of_delivered_is_409(api, storage):
    create(api)
    enqueue(storage, load_webhook_config(ENV))
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.DELIVERED)
    status, _ = api.handle(
        "POST", f"/api/v1/deliveries/{delivery.delivery_id}/retry", ADMIN, None
    )
    assert status == 409


def test_retry_of_unknown_delivery_is_404(api):
    assert api.handle("POST", "/api/v1/deliveries/whd_x/retry", ADMIN, None)[0] == 404


def test_retry_works_after_subscription_deletion(api, storage):
    _, created = create(api)
    sid = created["subscription_id"]
    enqueue(storage, load_webhook_config(ENV))
    delivery = storage.list_webhook_deliveries()[0]
    storage.claim_next_webhook_delivery("w1", 60)
    storage.finalize_webhook_delivery(delivery.delivery_id, "w1",
                                      DeliveryStatus.FAILED,
                                      retry_window_days=30)
    api.handle("DELETE", f"/api/v1/webhooks/{sid}", ADMIN, None)

    status, _ = api.handle(
        "POST", f"/api/v1/deliveries/{delivery.delivery_id}/retry", ADMIN, None
    )
    assert status == 200


# --------------------------------------------------------------------------- #
# error sanitisation (ADR-034)
# --------------------------------------------------------------------------- #


def test_internal_failure_is_not_leaked(storage, registry):
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("/secret/path/to/db.sqlite is corrupt")
            return boom

    api = WebhookAPI(Broken(), load_webhook_config(ENV), registry)
    status, body = api.handle("GET", "/api/v1/webhooks", ADMIN, None)
    assert status == 500
    assert body == {"error": "internal server error"}
