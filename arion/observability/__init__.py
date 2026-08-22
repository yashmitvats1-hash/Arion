"""Observability layer public surface."""

from arion.observability.error_boundary import (
    ErrorSource,
    ErrorSummary,
    classify_error_source,
    sanitize_error_text,
    summarize_error,
)
from arion.observability.events import (
    EVENT_KINDS,
    AuditEvent,
    AuthorizationEventDetails,
    EventContractError,
    EventDetails,
    EventLogger,
    EventSink,
    JsonlFileSink,
    SinkFailure,
    normalize_event_detail,
)

__all__ = [
    "EVENT_KINDS",
    "AuditEvent",
    "ErrorSource",
    "ErrorSummary",
    "AuthorizationEventDetails",
    "EventContractError",
    "EventDetails",
    "EventLogger",
    "EventSink",
    "JsonlFileSink",
    "SinkFailure",
    "classify_error_source",
    "normalize_event_detail",
    "sanitize_error_text",
    "summarize_error",
]
