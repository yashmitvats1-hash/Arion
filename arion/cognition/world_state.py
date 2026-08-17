"""World State / change detection (ADR-015).

The next architectural spine begins with the WORLD STATE:

  World State -> Beliefs -> Goals -> Long-Horizon Planning -> Strategy
  Selection -> Authorization -> Execution -> Observation -> Verification
  -> Learning

WorldStateMonitor observes environment facts (versioned per key), detects
CHANGES between observations, flags STALE facts, and exposes the current state
- so planning can rely on the CURRENT world rather than a stale snapshot.

INFORMATIONAL ONLY: world state can never authorize an action. It informs
planning (e.g. "the filesystem capability is registered") and strategy
selection, but PermissionPolicy remains the sole authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from arion.cognition.models import EnvironmentFact
from arion.state.models import new_id, utcnow


@dataclass
class WorldStateChange:
    """A detected change to the world state."""

    key: str
    old_value: Any
    new_value: Any
    version: int
    observed_at: str
    source: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "version": self.version,
            "observed_at": self.observed_at,
            "source": self.source,
        }


class WorldStateMonitor:
    """Observes environment facts and detects changes/staleness."""

    def __init__(self, store, sink: Any | None = None):
        self.store = store
        self.sink = sink  # duck-typed EventLogger: emits world.state.changed

    def observe(self, key: str, value: Any, source: str = "system") -> WorldStateChange | None:
        """Record an observation; returns a WorldStateChange if the value changed."""
        existing = self.store.get_environment_fact(key)
        now = utcnow()
        if existing is not None and existing.value == value:
            # unchanged: refresh observed_at only
            fact = EnvironmentFact(
                fact_id=existing.fact_id, key=key, value=value, source=source,
                version=existing.version, observed_at=existing.observed_at or now,
                created_at=existing.created_at, updated_at=now,
            )
            self.store.record_environment_fact(fact)
            return None
        fact = EnvironmentFact(
            fact_id=existing.fact_id if existing else new_id("fact"),
            key=key, value=value, source=source,
            version=(existing.version + 1) if existing else 1,
            observed_at=now, created_at=existing.created_at if existing else now, updated_at=now,
        )
        self.store.record_environment_fact(fact)
        if existing is not None:
            change = WorldStateChange(
                key=key, old_value=existing.value, new_value=value,
                version=fact.version, observed_at=now, source=source,
            )
            self._emit(key, change)
            return change
        return None  # first observation is not a 'change'

    def current_state(self) -> dict[str, Any]:
        """Current (non-stale-filtered) world state, keyed by fact key."""
        return {
            f.key: {
                "value": f.value,
                "version": f.version,
                "observed_at": f.observed_at,
                "source": f.source,
            }
            for f in self.store.list_environment_facts(limit=1000)
        }

    def changed_since(self, since_ts: str) -> list[WorldStateChange]:
        """Changes observed after `since_ts` (reconstructed from version bumps
        is not possible without history; we return facts whose observed_at is
        newer, flagged with their current version)."""
        out: list[WorldStateChange] = []
        for f in self.store.list_environment_facts(limit=1000):
            if f.observed_at and f.observed_at > since_ts:
                out.append(WorldStateChange(
                    key=f.key, old_value=None, new_value=f.value,
                    version=f.version, observed_at=f.observed_at or "", source=f.source,
                ))
        return out

    def stale_facts(self, max_age_days: float = 7.0) -> list[EnvironmentFact]:
        """Facts not observed within max_age_days - candidates for re-check
        before planning relies on them (stale facts must not mislead)."""
        try:
            now = datetime.now(timezone.utc)
        except Exception:
            now = datetime.now(timezone.utc)
        stale: list[EnvironmentFact] = []
        for f in self.store.list_environment_facts(limit=1000):
            obs = f.observed_at or f.updated_at
            try:
                obs_dt = datetime.fromisoformat(obs.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                stale.append(f)
                continue
            if obs_dt.tzinfo is None:
                obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            if (now - obs_dt).total_seconds() > max_age_days * 86400:
                stale.append(f)
        return stale

    def _emit(self, key: str, change: WorldStateChange) -> None:
        if self.sink is None:
            return
        try:
            from arion.observability.events import AuditEvent

            self.sink.emit(AuditEvent(
                kind="world.state.changed",
                detail={
                    "key": change.key,
                    "old_value": change.old_value,
                    "new_value": change.new_value,
                    "version": change.version,
                },
            ))
        except Exception:
            pass
