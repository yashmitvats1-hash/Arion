"""Webhook administration HTTP surface (ADR-059 D15).

Mounted alongside the M6-A approval API and following its style, but with
the Category A hardening ADR-059 D15.2 requires of any NEW surface:

  1. Exact route matching (a tuple-compared path segmentation), never
     `startswith` + `split("/")[-1]`.
  2. PATCH and DELETE implemented, so mutation is not smuggled through POST.
  3. Pagination on every collection, with validated bounds.
  4. Sanitized error responses - never `str(exc)` from an internal error.
  5. One centralized authorization call per request (`api_authz.authorize`).
  6. Validated query parameters; an unknown `status=` is a 400, not silence.
  7. Explicit response projections; never `asdict()` / `to_dict()` passthrough.

Secret material is returned exactly ONCE, in the 201 create response and the
200 rotate response, and is never readable again from any endpoint.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
from typing import Any, Callable

from arion.interfaces.api_authz import AuthContext, Privilege, TokenRegistry, authorize
from arion.notifications.config import WebhookConfig
from arion.notifications.eligibility import (
    WEBHOOK_ELIGIBLE_EVENT_KINDS,
    validate_event_kinds,
)
from arion.notifications.models import (
    DeliveryStatus,
    WebhookStateError,
    WebhookSubscription,
    new_subscription_id,
)
from arion.notifications.transport import validate_webhook_url
from arion.observability.error_boundary import sanitize_error_text

_MAX_BODY_BYTES = 8192
_SECRET_BYTES = 32

API_ROOT = ("api", "v1", "webhooks")


class WebhookAPIError(Exception):
    """Client-visible failure carrying an explicit HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _segments(path: str) -> tuple[str, ...]:
    return tuple(seg for seg in path.split("/") if seg)


def _generate_secret() -> str:
    return secrets.token_hex(_SECRET_BYTES)


def _parse_pagination(qs: dict[str, list[str]], config: WebhookConfig) -> int:
    raw = qs.get("limit", [None])[0]
    if raw is None:
        return config.page_size_default
    try:
        limit = int(raw)
    except ValueError:
        raise WebhookAPIError(400, "limit must be an integer") from None
    if limit < 1 or limit > config.page_size_max:
        raise WebhookAPIError(
            400, f"limit must be between 1 and {config.page_size_max}"
        )
    return limit


def _parse_status(qs: dict[str, list[str]]) -> DeliveryStatus | None:
    raw = qs.get("status", [None])[0]
    if raw is None:
        return None
    try:
        return DeliveryStatus(raw)
    except ValueError:
        raise WebhookAPIError(
            400,
            "status must be one of: "
            + ", ".join(sorted(s.value for s in DeliveryStatus)),
        ) from None


class WebhookAPI:
    """Transport-agnostic request handling, so it is testable without sockets."""

    def __init__(
        self,
        storage: Any,
        config: WebhookConfig,
        registry: TokenRegistry,
        *,
        secret_factory: Callable[[], str] = _generate_secret,
    ) -> None:
        self._storage = storage
        self._config = config
        self._registry = registry
        self._secret_factory = secret_factory

    # -- entry point -------------------------------------------------------

    def handle(
        self, method: str, path: str, authorization: str | None, body: bytes | None
    ) -> tuple[int, dict[str, Any]]:
        parsed = urllib.parse.urlparse(path)
        segs = _segments(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if segs[:3] != API_ROOT and segs[:3] != ("api", "v1", "deliveries"):
            return 404, {"error": "not found"}

        # Single enforcement point. EVERY webhook route requires ADMIN
        # (ADR-059 D15.1): creating a subscription creates outbound network
        # egress, which is an operator action, not an approver action.
        decision = authorize(self._registry, authorization, Privilege.ADMIN)
        if not decision.ok:
            return decision.status, {"error": decision.message}
        assert decision.context is not None

        if not self._config.enabled:
            # Disabled by default (ADR-059 D14): the surface exists but
            # refuses to create state that nothing would ever deliver.
            return 503, {"error": "webhook notifications are disabled"}

        try:
            return self._route(method.upper(), segs, qs, body, decision.context)
        except WebhookAPIError as exc:
            return exc.status, {"error": exc.message}
        except WebhookStateError as exc:
            return 409, {"error": sanitize_error_text(str(exc))}
        except ValueError as exc:
            return 400, {"error": sanitize_error_text(str(exc))}
        except Exception:
            # ADR-034: an unexpected internal failure never leaks its text.
            return 500, {"error": "internal server error"}

    # -- routing -----------------------------------------------------------

    def _route(
        self,
        method: str,
        segs: tuple[str, ...],
        qs: dict[str, list[str]],
        body: bytes | None,
        context: AuthContext,
    ) -> tuple[int, dict[str, Any]]:
        # /api/v1/deliveries/<id>
        if segs[:3] == ("api", "v1", "deliveries"):
            if len(segs) == 4:
                if method != "GET":
                    raise WebhookAPIError(405, "method not allowed")
                return self._get_delivery(segs[3])
            if len(segs) == 5 and segs[4] == "retry":
                if method != "POST":
                    raise WebhookAPIError(405, "method not allowed")
                return self._retry_delivery(segs[3])
            raise WebhookAPIError(404, "not found")

        if segs[:3] != API_ROOT:
            raise WebhookAPIError(404, "not found")

        rest = segs[3:]

        if not rest:
            if method == "GET":
                return self._list_subscriptions(qs)
            if method == "POST":
                return self._create_subscription(body, context)
            raise WebhookAPIError(405, "method not allowed")

        subscription_id = rest[0]

        if len(rest) == 1:
            if method == "GET":
                return self._get_subscription(subscription_id)
            if method == "PATCH":
                return self._update_subscription(subscription_id, body)
            if method == "DELETE":
                return self._delete_subscription(subscription_id)
            raise WebhookAPIError(405, "method not allowed")

        if len(rest) == 2 and rest[1] == "secret":
            if method != "POST":
                raise WebhookAPIError(405, "method not allowed")
            return self._rotate_secret(subscription_id)

        if len(rest) == 2 and rest[1] == "deliveries":
            if method != "GET":
                raise WebhookAPIError(405, "method not allowed")
            return self._list_deliveries(subscription_id, qs)

        raise WebhookAPIError(404, "not found")

    # -- body parsing ------------------------------------------------------

    def _json_body(self, body: bytes | None) -> dict[str, Any]:
        if body is None:
            raise WebhookAPIError(411, "length required")
        if len(body) > _MAX_BODY_BYTES:
            raise WebhookAPIError(413, "payload too large")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebhookAPIError(400, "malformed json") from None
        if not isinstance(data, dict):
            raise WebhookAPIError(400, "expected json object")
        return data

    # -- subscriptions -----------------------------------------------------

    def _create_subscription(
        self, body: bytes | None, context: AuthContext
    ) -> tuple[int, dict[str, Any]]:
        data = self._json_body(body)
        url = data.get("url")
        if not isinstance(url, str):
            raise WebhookAPIError(400, "url is required")
        try:
            validate_webhook_url(url, self._config.allowed_origins)
        except ValueError as exc:
            raise WebhookAPIError(400, str(exc)) from None
        kinds = validate_event_kinds(data.get("event_kinds"))

        description = data.get("description", "")
        if not isinstance(description, str):
            raise WebhookAPIError(400, "description must be a string")

        subscription = WebhookSubscription(
            subscription_id=new_subscription_id(),
            url=url.strip(),
            event_kinds=kinds,
            enabled=bool(data.get("enabled", True)),
            description=description[:500],
            created_by=context.actor.id,
        )
        secret = self._secret_factory()
        self._storage.create_webhook_subscription(subscription, secret)

        payload = subscription.public_dict()
        # The ONLY time secret material is ever returned by a read/write API.
        payload["secret"] = secret
        payload["secret_version"] = subscription.active_secret_version
        return 201, payload

    def _get_subscription(self, subscription_id: str) -> tuple[int, dict[str, Any]]:
        sub = self._storage.get_webhook_subscription(subscription_id)
        if sub is None:
            raise WebhookAPIError(404, "subscription not found")
        return 200, sub.public_dict()

    def _list_subscriptions(
        self, qs: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        limit = _parse_pagination(qs, self._config)
        after = qs.get("after", [None])[0]
        subs = self._storage.list_webhook_subscriptions(limit=limit, after_id=after)
        return 200, {
            "subscriptions": [s.public_dict() for s in subs],
            "limit": limit,
            "next_after": subs[-1].subscription_id if len(subs) == limit else None,
            "eligible_event_kinds": sorted(WEBHOOK_ELIGIBLE_EVENT_KINDS),
        }

    def _update_subscription(
        self, subscription_id: str, body: bytes | None
    ) -> tuple[int, dict[str, Any]]:
        data = self._json_body(body)
        fields: dict[str, Any] = {}
        if "url" in data:
            url = data["url"]
            if not isinstance(url, str):
                raise WebhookAPIError(400, "url must be a string")
            try:
                validate_webhook_url(url, self._config.allowed_origins)
            except ValueError as exc:
                raise WebhookAPIError(400, str(exc)) from None
            fields["url"] = url.strip()
        if "event_kinds" in data:
            fields["event_kinds"] = validate_event_kinds(data["event_kinds"])
        if "enabled" in data:
            if not isinstance(data["enabled"], bool):
                raise WebhookAPIError(400, "enabled must be a boolean")
            fields["enabled"] = data["enabled"]
        if "description" in data:
            if not isinstance(data["description"], str):
                raise WebhookAPIError(400, "description must be a string")
            fields["description"] = data["description"][:500]

        unknown = set(data) - {"url", "event_kinds", "enabled", "description"}
        if unknown:
            # Bounds are configuration-only and can never be raised through
            # the API (ADR-059 D14, invariant 14). Rejecting loudly beats
            # accepting a request whose security-relevant part was ignored.
            raise WebhookAPIError(
                400, f"unsupported field(s): {', '.join(sorted(unknown))}"
            )
        if not fields:
            raise WebhookAPIError(400, "no updatable fields provided")

        updated = self._storage.update_webhook_subscription(subscription_id, fields)
        if updated is None:
            raise WebhookAPIError(404, "subscription not found")
        return 200, updated.public_dict()

    def _delete_subscription(self, subscription_id: str) -> tuple[int, dict[str, Any]]:
        if not self._storage.delete_webhook_subscription(subscription_id):
            raise WebhookAPIError(404, "subscription not found")
        # Explicit about what deletion did and did NOT do (ADR-059 D16):
        # history survives and remains queryable (invariant 26).
        return 200, {
            "subscription_id": subscription_id,
            "deleted": True,
            "pending_deliveries_cancelled": True,
            "delivery_history_retained": True,
        }

    def _rotate_secret(self, subscription_id: str) -> tuple[int, dict[str, Any]]:
        secret = self._secret_factory()
        version = self._storage.rotate_webhook_secret(
            subscription_id,
            secret,
            max_live_versions=self._config.max_secret_versions,
        )
        return 200, {
            "subscription_id": subscription_id,
            "secret": secret,
            "secret_version": version,
        }

    # -- deliveries --------------------------------------------------------

    def _list_deliveries(
        self, subscription_id: str, qs: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        limit = _parse_pagination(qs, self._config)
        status = _parse_status(qs)
        after_raw = qs.get("after_sequence", [None])[0]
        after_sequence: int | None = None
        if after_raw is not None:
            try:
                after_sequence = int(after_raw)
            except ValueError:
                raise WebhookAPIError(
                    400, "after_sequence must be an integer"
                ) from None

        deliveries = self._storage.list_webhook_deliveries(
            subscription_id=subscription_id,
            status=status,
            limit=limit,
            after_sequence=after_sequence,
        )
        sub = self._storage.get_webhook_subscription(
            subscription_id, include_deleted=False
        )
        if sub is None and not deliveries:
            # ADR-059 D15.4: "never existed" and "fully pruned" are
            # deliberately indistinguishable - distinguishing them would
            # turn this endpoint into an identifier oracle.
            raise WebhookAPIError(404, "no delivery history for subscription")

        return 200, {
            "subscription_id": subscription_id,
            "subscription_exists": sub is not None,
            "deliveries": [d.public_dict() for d in deliveries],
            "limit": limit,
            "next_after_sequence": (
                deliveries[-1].sequence if len(deliveries) == limit else None
            ),
        }

    def _retry_delivery(self, delivery_id: str) -> tuple[int, dict[str, Any]]:
        """Admin-initiated retry of a failed/dead-lettered delivery.

        Works on deliveries whose subscription has been deleted, for as long
        as the retained signing key is still available (ADR-059 D15.4): the
        remedy a retention window promises must actually be exercisable.
        """
        delivery = self._storage.get_webhook_delivery(delivery_id)
        if delivery is None:
            raise WebhookAPIError(404, "delivery not found")
        if not self._storage.manual_retry_webhook_delivery(delivery_id):
            # Either not in a retryable state, or past its
            # `retry_eligible_until` horizon. 409 rather than 400: the
            # request is well formed, the resource state forbids it.
            raise WebhookAPIError(
                409,
                "delivery is not retryable in its current state or its retry "
                "window has closed",
            )
        refreshed = self._storage.get_webhook_delivery(delivery_id)
        return 200, refreshed.public_dict()

    def _get_delivery(self, delivery_id: str) -> tuple[int, dict[str, Any]]:
        # Liveness-independent by design (ADR-059 D15.4).
        delivery = self._storage.get_webhook_delivery(delivery_id)
        if delivery is None:
            raise WebhookAPIError(404, "delivery not found")
        return 200, delivery.public_dict()


class WebhookAPIHandlerMixin:
    """Mounts `WebhookAPI` onto the existing BaseHTTPRequestHandler surface.

    A MIXIN rather than a second server: one process, one port, one
    credential configuration. Webhook routes are dispatched first; anything
    else falls through to the approval handler unchanged, so
    `tests/test_approval_api.py` behaviour is untouched (ADR-059 D15.3).
    """

    webhook_api: "WebhookAPI | None" = None

    def _webhook_dispatch(self, method: str) -> bool:
        api = getattr(self, "webhook_api", None)
        if api is None:
            return False
        parsed = urllib.parse.urlparse(self.path)
        segs = _segments(parsed.path)
        if segs[:3] != API_ROOT and segs[:3] != ("api", "v1", "deliveries"):
            return False

        body: bytes | None = None
        if method in ("POST", "PATCH"):
            length_str = self.headers.get("Content-Length")
            if length_str is None:
                body = b""
            else:
                try:
                    length = int(length_str)
                except ValueError:
                    self._send_json(400, {"error": "invalid length"})
                    return True
                if length > _MAX_BODY_BYTES:
                    self._send_json(413, {"error": "payload too large"})
                    return True
                body = self.rfile.read(max(0, length))

        status, payload = api.handle(
            method, self.path, self.headers.get("Authorization"), body
        )
        self._send_json(status, payload)
        return True

    def do_GET(self) -> None:  # type: ignore[override]
        if not self._webhook_dispatch("GET"):
            super().do_GET()

    def do_POST(self) -> None:  # type: ignore[override]
        if not self._webhook_dispatch("POST"):
            super().do_POST()

    def do_PATCH(self) -> None:
        if not self._webhook_dispatch("PATCH"):
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._webhook_dispatch("DELETE"):
            self._send_json(404, {"error": "not found"})
