"""Durable capability-observation contract (ADR-035).

Capabilities may keep returning ordinary dictionaries.  The engine calls this
boundary immediately after execution so verification and persistence operate on
a detached, canonical, finite JSON object rather than capability-owned state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from arion.capabilities.registry import CapabilityError

# Accommodates the existing 1 MB filesystem/HTTP content contracts even under
# worst-case JSON string escaping, while making future/injected observations
# finite. Per-action/streaming budgets are intentionally deferred.
MAX_DURABLE_OBSERVATION_BYTES = 8_000_000


class ObservationContractError(CapabilityError):
    """A successful capability returned an invalid durable observation."""


def _validate_mapping_keys(value: Any, path: str = "observation") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ObservationContractError(
                    f"{path} must use string keys at every mapping level"
                )
            _validate_mapping_keys(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _validate_mapping_keys(nested, f"{path}[{index}]")


def normalize_observation(
    value: Mapping[str, Any],
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Validate and snapshot one observation for verification/persistence.

    JSON encoding followed by decoding provides the same canonical container
    types that a SQLite restart would return (for example tuples become lists)
    and detaches all nested mutable values from the capability implementation.
    """
    if not isinstance(value, Mapping):
        raise ObservationContractError(
            "capability observation must be a mapping/JSON object"
        )
    try:
        _validate_mapping_keys(value)
    except RecursionError as exc:
        raise ObservationContractError(
            "capability observation must be acyclic and JSON serializable"
        ) from exc
    limit = MAX_DURABLE_OBSERVATION_BYTES if max_bytes is None else max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("max_bytes must be a positive integer")
    try:
        encoded = json.dumps(dict(value))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ObservationContractError(
            f"capability observation must be JSON serializable: {exc}"
        ) from exc
    size = len(encoded.encode("utf-8"))
    if size > limit:
        raise ObservationContractError(
            f"capability observation size {size} bytes exceeds durable "
            f"limit {limit} bytes"
        )
    # json.loads cannot alias caller-owned nested structures.
    snapshot = json.loads(encoded)
    if not isinstance(snapshot, dict):  # defensive: top-level mapping encoded
        raise ObservationContractError(
            "capability observation must normalize to a JSON object"
        )
    return snapshot
