"""Webhook configuration surface (ADR-059 D14).

Environment-only, mirroring the ARION_LLM_* pattern: a frozen dataclass, a
loader that raises a typed error NAMING the offending variable, and a
credential-safe repr.

Security-relevant bounds live HERE and only here. They can never be widened
through the webhook API (ADR-059 D14 hard rule, invariant 14). Unsafe,
unbounded, zero, negative or non-numeric values are REJECTED at load, never
silently clamped (invariant 20).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Mapping

# The complete ARION_WEBHOOK_* configuration surface (ADR-059 D14).
ENV_ENABLED = "ARION_WEBHOOK_ENABLED"
ENV_ALLOWED_ORIGINS = "ARION_WEBHOOK_ALLOWED_ORIGINS"
ENV_TIMEOUT = "ARION_WEBHOOK_TIMEOUT_SECONDS"
ENV_MAX_RESPONSE_BYTES = "ARION_WEBHOOK_MAX_RESPONSE_BYTES"
ENV_MAX_ATTEMPTS = "ARION_WEBHOOK_MAX_ATTEMPTS"
ENV_BACKOFF_BASE = "ARION_WEBHOOK_BACKOFF_BASE_SECONDS"
ENV_BACKOFF_CAP = "ARION_WEBHOOK_BACKOFF_CAP_SECONDS"
ENV_LEASE = "ARION_WEBHOOK_LEASE_SECONDS"
ENV_POLL_INTERVAL = "ARION_WEBHOOK_POLL_INTERVAL_SECONDS"
ENV_WORKER_CONCURRENCY = "ARION_WEBHOOK_WORKER_CONCURRENCY"
ENV_PAGE_SIZE_DEFAULT = "ARION_WEBHOOK_PAGE_SIZE_DEFAULT"
ENV_PAGE_SIZE_MAX = "ARION_WEBHOOK_PAGE_SIZE_MAX"
ENV_RETENTION_DELIVERED_DAYS = "ARION_WEBHOOK_RETENTION_DELIVERED_DAYS"
ENV_RETENTION_FAILED_DAYS = "ARION_WEBHOOK_RETENTION_FAILED_DAYS"
ENV_MAX_SECRET_VERSIONS = "ARION_WEBHOOK_MAX_SECRET_VERSIONS"

#: ADR-059 D14.1 rule 1: the lease must strictly outlive one complete
#: attempt plus finalize. This floor is an ARCHITECTURAL INVARIANT, not a
#: tunable: it absorbs the sub-second deadline-enforcement imprecision
#: documented in D12.2 plus signature computation, `_sql_lock`
#: reacquisition, and the finalize transaction.
LEASE_TIMEOUT_MARGIN_SECONDS = 5.0

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


class WebhookConfigError(Exception):
    """Typed webhook configuration failure (fail closed)."""


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise WebhookConfigError(
        f"{name} must be a boolean value (got {raw!r})")


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WebhookConfigError(
            f"{name} must be a number (got {raw!r})") from exc
    if math.isnan(value) or math.isinf(value):
        raise WebhookConfigError(
            f"{name} must be finite (got {raw!r})")
    if value <= 0:
        raise WebhookConfigError(
            f"{name} must be greater than zero (got {raw!r})")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WebhookConfigError(
            f"{name} must be an integer (got {raw!r})") from exc
    if value < 1:
        raise WebhookConfigError(
            f"{name} must be at least 1 (got {raw!r})")
    return value


@dataclass(frozen=True)
class WebhookConfig:
    """Validated webhook configuration (ADR-059 D14).

    Defaults are the conservative values approved in ADR-059 Revision 3.
    """

    enabled: bool = False
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 10.0
    max_response_bytes: int = 8192
    max_attempts: int = 8
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 3600.0
    lease_seconds: float = 60.0
    poll_interval_seconds: float = 5.0
    worker_concurrency: int = 1
    page_size_default: int = 50
    page_size_max: int = 200
    retention_delivered_days: float = 7.0
    retention_failed_days: float = 30.0
    max_secret_versions: int = 8

    def backoff_for_attempt(self, attempts: int) -> float:
        """ADR-059 D10: min(base * 2^(attempts-1), cap).

        `attempts` is the number of attempts already made (>= 1). The cap is
        applied unconditionally and must NOT be optimized away: it is
        unreached at the defaults but binds when an operator raises
        max_attempts (ADR-059 D10.1).
        """
        n = max(1, int(attempts))
        # Bound the exponent before exponentiating so a large operator-set
        # max_attempts cannot produce an overflow instead of the cap.
        if n - 1 > 64:
            return self.backoff_cap_seconds
        raw = self.backoff_base_seconds * (2 ** (n - 1))
        return min(raw, self.backoff_cap_seconds)


def load_webhook_config(env: Mapping[str, str] | None = None) -> WebhookConfig:
    """Load and validate the webhook configuration from the environment.

    Every architectural relationship in ADR-059 D14.1 is enforced here and
    violations are REJECTED with a typed error naming the variable.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    enabled = _bool(source, ENV_ENABLED, False)

    raw_origins = _get(source, ENV_ALLOWED_ORIGINS)
    origins: set[str] = set()
    for part in raw_origins.split(","):
        candidate = part.strip()
        if candidate:
            origins.add(candidate.rstrip("/").lower())

    timeout_seconds = _positive_float(source, ENV_TIMEOUT, 10.0)
    max_response_bytes = _positive_int(source, ENV_MAX_RESPONSE_BYTES, 8192)
    max_attempts = _positive_int(source, ENV_MAX_ATTEMPTS, 8)
    backoff_base = _positive_float(source, ENV_BACKOFF_BASE, 5.0)
    backoff_cap = _positive_float(source, ENV_BACKOFF_CAP, 3600.0)
    lease_seconds = _positive_float(source, ENV_LEASE, 60.0)
    poll_interval = _positive_float(source, ENV_POLL_INTERVAL, 5.0)
    worker_concurrency = _positive_int(source, ENV_WORKER_CONCURRENCY, 1)
    page_size_default = _positive_int(source, ENV_PAGE_SIZE_DEFAULT, 50)
    page_size_max = _positive_int(source, ENV_PAGE_SIZE_MAX, 200)
    retention_delivered = _positive_float(source, ENV_RETENTION_DELIVERED_DAYS, 7.0)
    retention_failed = _positive_float(source, ENV_RETENTION_FAILED_DAYS, 30.0)
    max_secret_versions = _positive_int(source, ENV_MAX_SECRET_VERSIONS, 8)

    # ---- ADR-059 D14.1 architectural relationships (reject, never clamp) ----

    # Rule 9: enabling without an operator allowlist is fail-closed.
    if enabled and not origins:
        raise WebhookConfigError(
            f"{ENV_ALLOWED_ORIGINS} must be a non-empty HTTPS origin "
            f"allowlist when {ENV_ENABLED} is set (fail closed; ADR-059 D12.3)")

    # Rule 1: the exact lease/timeout relationship.
    if lease_seconds < timeout_seconds + LEASE_TIMEOUT_MARGIN_SECONDS:
        raise WebhookConfigError(
            f"{ENV_LEASE} ({lease_seconds}) must be at least "
            f"{ENV_TIMEOUT} + {LEASE_TIMEOUT_MARGIN_SECONDS} "
            f"({timeout_seconds + LEASE_TIMEOUT_MARGIN_SECONDS}) so a lease "
            f"strictly outlives one complete attempt plus finalize "
            f"(ADR-059 D14.1)")

    # Rule 3.
    if backoff_cap < backoff_base:
        raise WebhookConfigError(
            f"{ENV_BACKOFF_CAP} ({backoff_cap}) must be greater than or equal "
            f"to {ENV_BACKOFF_BASE} ({backoff_base})")

    # Rule 4.
    if page_size_default > page_size_max:
        raise WebhookConfigError(
            f"{ENV_PAGE_SIZE_DEFAULT} ({page_size_default}) must be less than "
            f"or equal to {ENV_PAGE_SIZE_MAX} ({page_size_max})")

    # Rule 6: the retention asymmetry that keeps manual retry a real remedy.
    if retention_failed < retention_delivered:
        raise WebhookConfigError(
            f"{ENV_RETENTION_FAILED_DAYS} ({retention_failed}) must be greater "
            f"than or equal to {ENV_RETENTION_DELIVERED_DAYS} "
            f"({retention_delivered}) (ADR-059 D17 asymmetry)")

    # Rule 7: rotation needs at least an active plus one retiring version.
    if max_secret_versions < 2:
        raise WebhookConfigError(
            f"{ENV_MAX_SECRET_VERSIONS} must be at least 2 so rotation can "
            f"retain a retiring version (got {max_secret_versions})")

    return WebhookConfig(
        enabled=enabled,
        allowed_origins=frozenset(origins),
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base,
        backoff_cap_seconds=backoff_cap,
        lease_seconds=lease_seconds,
        poll_interval_seconds=poll_interval,
        worker_concurrency=worker_concurrency,
        page_size_default=page_size_default,
        page_size_max=page_size_max,
        retention_delivered_days=retention_delivered,
        retention_failed_days=retention_failed,
        max_secret_versions=max_secret_versions,
    )
