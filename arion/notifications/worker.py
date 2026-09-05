"""Durable webhook delivery worker (ADR-059 D9/D10).

A single daemon thread that repeatedly claims one due delivery, performs one
HTTP attempt OUTSIDE any database lock, and records a fenced outcome.

The three structural rules that make this safe:

  1. Never hold `_sql_lock` across network I/O (invariant 11). The claim is
     one short transaction; the attempt happens with no lock held; the
     outcome is a second short transaction. A blocked endpoint therefore
     cannot stall the orchestrator's storage.
  2. Every state change is FENCED on (owner, live lease). A worker whose
     lease expired mid-attempt loses the row silently instead of
     overwriting whoever legitimately reclaimed it (invariant 10).
  3. Claiming is atomic (BEGIN IMMEDIATE + conditional UPDATE + rowcount),
     never check-then-act.

The worker follows the `scheduler_work` lease/claim/fence PRECEDENT but does
not reuse that machinery: `scheduler_work` schedules agent work that has a
Task and an Actor and feeds capacity accounting, while these rows are
infrastructure egress. Sharing the table would put third-party endpoint
latency into the agent's scheduling substrate.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from arion.notifications.config import WebhookConfig
from arion.notifications.models import (
    DeliveryStatus,
    WebhookDelivery,
    iso_plus,
)
from arion.notifications.transport import (
    WebhookTransport,
    WebhookTransportError,
    build_headers,
    validate_webhook_url,
)
from arion.observability.error_boundary import sanitize_error_text
from arion.state.models import new_id, utcnow


class WebhookDeliveryWorker:
    """Background delivery loop with an injectable clock and sleeper."""

    def __init__(
        self,
        storage: Any,
        config: WebhookConfig,
        transport: WebhookTransport,
        *,
        worker_id: str | None = None,
        clock: Callable[[], str] = utcnow,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._storage = storage
        self._config = config
        self._transport = transport
        self._worker_id = worker_id or new_id("whw")
        self._clock = clock
        self._stop = threading.Event()
        self._sleeper = sleeper or self._stop.wait
        self._thread: threading.Thread | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread. Disabled configuration starts nothing."""
        if not self._config.enabled or self._thread is not None:
            return
        # ADR-059 D14: concurrency is 1. Higher values are accepted by config
        # but a single loop is what this milestone implements; ordering is
        # not guaranteed either way (D8), so this is a throughput limit only.
        thread = threading.Thread(
            target=self._run, name="arion-webhook-worker", daemon=True
        )
        self._thread = thread
        thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Signal stop and join. Idempotent; never raises."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # `ResourceLifecycle` closes resources via close(); ADR-059 D3 keeps
    # `ResourceLifecycle` itself unmodified (no start hooks), so the worker
    # is started by the composition root after storage exists.
    close = shutdown

    def health(self) -> dict[str, Any]:
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "enabled": self._config.enabled,
            "running": alive,
            "worker_id": self._worker_id,
        }

    # -- loop --------------------------------------------------------------

    def _run(self) -> None:  # pragma: no cover - exercised via run_once()
        while not self._stop.is_set():
            try:
                progressed = self.run_once()
            except Exception:
                # A delivery-subsystem defect must never kill the loop and
                # must never propagate into orchestration.
                progressed = False
            if not progressed:
                self._sleeper(self._config.poll_interval_seconds)

    def run_once(self) -> bool:
        """Reclaim, claim and process at most one delivery.

        Returns True if a delivery was processed, so the caller (or loop)
        can poll immediately instead of sleeping while work remains. Made
        public and synchronous specifically so tests can drive the whole
        state machine deterministically without threads.
        """
        now = self._clock()
        self._storage.reclaim_stale_webhook_deliveries(now=now)

        delivery = self._storage.claim_next_webhook_delivery(
            self._worker_id, self._config.lease_seconds, now=now
        )
        if delivery is None:
            return False
        self._process(delivery)
        return True

    # -- one attempt -------------------------------------------------------

    def _process(self, delivery: WebhookDelivery) -> None:
        # Re-validate the destination on EVERY attempt (ADR-059 D12.3): the
        # allowlist may have been tightened since the subscription was
        # created, and a long-retrying delivery must not keep egressing to
        # an origin an operator has since revoked.
        try:
            validate_webhook_url(delivery.url, self._config.allowed_origins)
        except ValueError as exc:
            self._finalize(
                delivery, DeliveryStatus.DEAD_LETTER,
                error=f"destination rejected: {sanitize_error_text(str(exc))}",
            )
            return

        secret_version = self._storage.get_webhook_secret_version(
            delivery.subscription_id, delivery.secret_version
        )
        if secret_version is None or not secret_version.material_present:
            # Invariant 23 says this must not happen while the delivery is
            # retry-capable. If it does, the correct action is to stop and
            # record it, never to sign with a different version: that would
            # silently break the receiver's signature verification.
            self._finalize(
                delivery, DeliveryStatus.DEAD_LETTER,
                error="signing secret version unavailable",
            )
            return

        headers = build_headers(
            delivery_id=delivery.delivery_id,
            event_kind=delivery.event_kind,
            secret=secret_version.secret or "",
            secret_version=delivery.secret_version,
            body=delivery.body_bytes,
            timestamp=self._clock(),
        )

        try:
            response = self._transport.post(
                delivery.url,
                delivery.body_bytes,
                headers,
                self._config.timeout_seconds,
                self._config.max_response_bytes,
            )
        except WebhookTransportError as exc:
            if exc.retryable:
                self._retry_or_give_up(delivery, sanitize_error_text(str(exc)))
            else:
                self._finalize(
                    delivery, DeliveryStatus.DEAD_LETTER,
                    error=sanitize_error_text(str(exc)),
                )
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._retry_or_give_up(delivery, sanitize_error_text(str(exc)))
            return

        self._classify(delivery, response)

    def _classify(self, delivery: WebhookDelivery, response: Any) -> None:
        """ADR-059 D10 retry classification."""
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            self._finalize(delivery, DeliveryStatus.DELIVERED)
            return
        if status_code == 429 or status_code >= 500:
            # Transient by contract: the receiver is asking us to back off.
            self._retry_or_give_up(delivery, f"http {status_code}")
            return
        if 300 <= status_code < 400:
            # Redirects are never followed (ADR-059 D12.1): a signed body
            # must not be replayed to an origin we never authorized.
            self._finalize(
                delivery, DeliveryStatus.DEAD_LETTER,
                error=f"http {status_code}: redirects are not followed",
            )
            return
        # Remaining 4xx are permanent: retrying an identical signed body
        # against a rejecting endpoint cannot succeed.
        self._finalize(
            delivery, DeliveryStatus.DEAD_LETTER, error=f"http {status_code}"
        )

    def _retry_or_give_up(self, delivery: WebhookDelivery, error: str) -> None:
        if delivery.attempts >= self._config.max_attempts:
            # Retry budget exhausted -> FAILED (not dead_letter): the cause
            # was transient, so manual retry remains a meaningful remedy
            # within the retention horizon (ADR-059 D10/D11.3).
            self._finalize(delivery, DeliveryStatus.FAILED, error=error)
            return
        delay = self._config.backoff_for_attempt(delivery.attempts)
        now = self._clock()
        self._storage.reschedule_webhook_delivery(
            delivery.delivery_id,
            self._worker_id,
            next_attempt_at=iso_plus(now, delay),
            error=error,
            now=now,
        )

    def _finalize(
        self, delivery: WebhookDelivery, status: DeliveryStatus,
        *, error: str | None = None,
    ) -> None:
        self._storage.finalize_webhook_delivery(
            delivery.delivery_id,
            self._worker_id,
            status,
            error=error,
            retry_window_days=self._config.retention_failed_days,
            now=self._clock(),
        )
