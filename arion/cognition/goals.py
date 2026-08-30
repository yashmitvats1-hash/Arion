"""Durable goal management and long-horizon execution (ADR-016).

GoalManager is the AUTHORITATIVE state machine for long-lived goals:

  Goal -> Goal State -> Strategy -> Plan -> Execute -> Observe -> Learn
  -> Replan

It owns:
  - goal lifecycle state transitions (validated; invalid transitions FAIL
    CLOSED via GoalStateError), persisted and restart-safe;
  - goal versioning (goal.version is the CAS token; increments on every
    committed authoritative write);
  - plan versioning (monotonic, immutable previous plans, replay-safe);
  - progress evaluation (via the ProgressEvaluator seam);
  - strategy selection (via StrategySelector, explainable + provenance).

It NEVER infers goal completion from a single successful task: completion
requires a plan whose steps are covered and no blockers/outstanding work.

INFORMATIONAL ONLY: goals, strategies, and progress can influence planning;
only the live authorization layer authorizes execution.
"""

from __future__ import annotations

from typing import Any

from arion.cognition.progress import (
    DeterministicProgressEvaluator,
    ProgressEvaluator,
    ProgressResult,
    canonical_exact_task,
)
from arion.cognition.strategy import STRATEGY_NAMES, StrategySelector
from arion.state.models import (
    GOAL_TRANSITIONS,
    Goal,
    GoalStateError,
    GoalStatus,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)

# Bounded CAS retries. A writer that cannot commit after this many
# independent reloads fails closed rather than spinning.
_GOAL_CAS_MAX_ATTEMPTS = 8


class GoalPlanLineageError(GoalStateError):
    """A transition's expected immutable plan lineage no longer matches the
    authoritative latest plan (ADR-054; fail closed, nothing mutated).

    Raised when a terminal completion request carries the plan version its
    evaluation assessed but a NEWER immutable plan version has become the
    authoritative latest before the transition commits. Goal-row CAS cannot
    detect this: plan commits intentionally never bump ``goal.version``.
    """


class GoalManager:
    """Authoritative, persistent goal state machine (ADR-016)."""

    def __init__(
        self,
        storage: Any,                       # SQLiteStorage (goals + tasks authoritative)
        cognitive_store: Any | None = None,  # SQLiteCognitiveStore (goal_plans + beliefs)
        events: Any | None = None,          # EventLogger (audit events)
        strategy_selector: Any | None = None,
        progress_evaluator: ProgressEvaluator | None = None,
        world_monitor: Any | None = None,
        lock_contention_resolver: Any | None = None,  # ADR-021: callable(blocker)->bool;
                                                      # True when a lock_contention blocker
                                                      # may clear (lock no longer active)
        recovery_required_resolver: Any | None = None,  # ADR-041: callable(blocker)->bool;
                                                        # True when no REQUIRED row remains
    ):
        self.storage = storage
        self.cognitive_store = cognitive_store
        self.events = events
        self.strategy_selector = strategy_selector or StrategySelector()
        self.progress_evaluator = progress_evaluator or DeterministicProgressEvaluator()
        self.world_monitor = world_monitor
        self.lock_contention_resolver = lock_contention_resolver
        self.recovery_required_resolver = recovery_required_resolver

    # ------------------------------------------------------------------ #
    # Goal lifecycle
    # ------------------------------------------------------------------ #

    def create_goal(self, description: str, source: str = "cli") -> Goal:
        goal = Goal(id=new_id("goal"), description=description, source=source)
        self.storage.save_goal(goal)
        self._emit("goal.created", goal_id=goal.id, detail={
            "goal_id": goal.id, "description": description[:200], "source": source,
        })
        return goal

    def get_goal(self, goal_id: str) -> Goal | None:
        return self.storage.load_goal(goal_id)

    def list_goals(self, status: str | None = None) -> list[Goal]:
        return self.storage.list_goals(status=status)

    def _commit_goal(self, goal: Goal, expected_version: int,
                     extra: dict | None = None) -> bool:
        """CAS lifecycle columns only (status, blockers, version).

        Strategy / progress / last_evaluated_at are left untouched so a
        lifecycle writer cannot clobber a concurrent metadata patch, and
        a metadata patch cannot resurrect a superseded status. Optional
        ``extra`` (e.g. last_replan_reason on fail_goal) is written in
        the same UPDATE.
        """
        payload = {
            "status": goal.status_value,
            "blockers": list(goal.blockers or []),
            "version": goal.version,
            "updated_at": goal.updated_at,
        }
        if extra:
            payload.update(extra)
        casf = getattr(self.storage, "cas_goal_fields", None)
        if casf is not None:
            return bool(casf(goal.id, expected_version, payload))
        cas = getattr(self.storage, "cas_goal", None)
        if cas is None:
            self.storage.save_goal(goal)
            return True
        return bool(cas(goal, expected_version))

    def _contention_error(self, goal_id: str, op: str) -> GoalStateError:
        return GoalStateError(
            f"goal {goal_id} {op} failed under persistent contention (fail closed)"
        )

    def _patch_goal(self, goal_id: str, apply, *, columns: tuple[str, ...],
                    op: str = "update") -> Goal:
        """Reload-apply-CAS for informational field updates.

        ``apply(goal)`` mutates only informational columns named in
        ``columns``. The write is column-scoped and does NOT increment
        ``goal.version``, so a strategy/progress/replan-reason patch
        cannot clobber status/blockers and is not itself a lifecycle
        transition. On a version miss the latest row is reloaded.
        """
        casf = getattr(self.storage, "cas_goal_fields", None)
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            expected = goal.version
            apply(goal)
            goal.updated_at = utcnow()
            payload = {col: getattr(goal, col) for col in columns}
            payload["updated_at"] = goal.updated_at
            if casf is not None:
                if casf(goal.id, expected, payload):
                    return goal
                continue
            # Fallback for test doubles: full-row CAS still increments so
            # the write cannot silently replace a newer lifecycle row.
            goal.version = expected + 1
            if self._commit_goal(goal, expected):
                return goal
        raise self._contention_error(goal_id, op)

    def set_replan_reason(self, goal_id: str, reason: str) -> Goal:
        """Persist ``last_replan_reason`` without touching lifecycle state."""
        return self._patch_goal(
            goal_id, lambda g: setattr(g, "last_replan_reason", reason),
            columns=("last_replan_reason",), op="set_replan_reason")

    def transition(self, goal_id: str, to_state: str, reason: str, actor: str = "system",
                   fields: dict[str, Any] | None = None,
                   expect_plan_version: int | None = None) -> Goal:
        """Validate + persist a goal state transition (fail closed).

        On a CAS miss the canonical row is reloaded and the transition
        is revalidated against the latest status. Events are emitted
        only after the write commits. ``fields`` (e.g. last_replan_reason
        on fail_goal) are applied in the SAME successful write so the
        version increments exactly once.

        ADR-054 plan-lineage fence: when ``expect_plan_version`` is given,
        EVERY CAS attempt re-reads the authoritative latest immutable plan
        and the transition is refused (``GoalPlanLineageError``, no state
        mutated) unless that version is still latest. ``goal.version`` CAS
        proves only row concurrency - plan commits never bump it - so this
        is the boundary where evaluated plan authority is revalidated.

        ADR-056 atomic fence: when ``expect_plan_version`` is given and the
        storage backend supplies ``cas_goal_terminal_fenced``, the lineage
        read and the goal CAS execute inside ONE ``BEGIN IMMEDIATE``
        transaction on the storage connection, eliminating the
        cross-connection timing gap documented in ADR-054/055.  The
        validated plan version is used directly for strategy-outcome
        attribution, replacing the post-commit ``latest_plan`` re-read.
        For storage backends that do not supply the method (test doubles,
        legacy) the original two-step behaviour is preserved unchanged.
        """
        to_state = to_state.value if isinstance(to_state, GoalStatus) else to_state
        if to_state not in GOAL_TRANSITIONS:
            raise GoalStateError(f"unknown goal state {to_state!r}")
        if expect_plan_version is not None and (
                isinstance(expect_plan_version, bool)
                or not isinstance(expect_plan_version, int)
                or expect_plan_version < 1):
            raise ValueError(
                f"expect_plan_version must be a positive integer, got "
                f"{expect_plan_version!r} (fail closed)")

        # ADR-056: if the storage backend exposes the atomic fenced CAS,
        # use it for every attempt when a plan-version expectation is set.
        # This collapses the lineage read + goal UPDATE into one BEGIN
        # IMMEDIATE, closing the cross-connection gap accepted in ADR-054/055.
        _fenced_cas = (
            getattr(self.storage, "cas_goal_terminal_fenced", None)
            if expect_plan_version is not None else None
        )

        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            allowed = GOAL_TRANSITIONS[goal.status.value]
            if to_state not in allowed:
                raise GoalStateError(
                    f"invalid goal transition {goal.status.value!r} -> {to_state!r} for goal {goal_id}"
                )

            old_state = goal.status.value
            expected = goal.version
            goal.status = GoalStatus(to_state)
            goal.version = expected + 1
            goal.updated_at = utcnow()
            if to_state == GoalStatus.ACTIVE.value and goal.blockers:
                # resuming/unblocking clears resolved blockers
                goal.blockers = []
            extra: dict[str, Any] = {}
            if fields:
                for key, value in fields.items():
                    setattr(goal, key, value)
                    extra[key] = value

            validated_plan_version: int | None = None

            if _fenced_cas is not None:
                # ── ADR-056 atomic path ────────────────────────────────────
                # Build the fields payload exactly as _commit_goal / cas_goal_fields
                # would, then delegate to the one-transaction storage method.
                import json as _json
                payload: dict[str, Any] = {
                    "status": goal.status_value,
                    "blockers": list(goal.blockers or []),
                    "version": goal.version,
                    "updated_at": goal.updated_at,
                }
                if extra:
                    payload.update(extra)
                outcome, result_version = _fenced_cas(
                    goal_id, expected, expect_plan_version, payload
                )
                if outcome == "lineage_mismatch":
                    raise GoalPlanLineageError(
                        f"goal {goal_id} transition {to_state!r} denied: "
                        f"evaluated plan {expect_plan_version} is no longer "
                        f"the latest immutable plan (latest={result_version}); "
                        f"fail closed"
                    )
                if outcome == "cas_miss":
                    # Goal row was concurrently modified; retry from fresh state.
                    continue
                # outcome == "ok": committed successfully.
                validated_plan_version = result_version  # authoritative at commit
            else:
                # ── Original two-step path (ADR-054/055) ──────────────────
                # Used when storage does not supply cas_goal_terminal_fenced
                # (test doubles, legacy stores).  Retains the prior behaviour
                # exactly, including the accepted cross-connection timing gap.
                if expect_plan_version is not None:
                    # ADR-054: checked INSIDE the retry loop so a CAS retry can
                    # never reuse stale plan authority from an earlier attempt.
                    latest = self.latest_plan(goal_id)
                    latest_version = latest["plan_version"] if latest else None
                    if latest_version != expect_plan_version:
                        raise GoalPlanLineageError(
                            f"goal {goal_id} transition {to_state!r} denied: "
                            f"evaluated plan {expect_plan_version} is no longer "
                            f"the latest immutable plan (latest={latest_version}); "
                            f"fail closed"
                        )
                if not self._commit_goal(goal, expected, extra=extra or None):
                    continue

            self._emit("goal.state.changed", goal_id=goal_id, detail={
                "goal_id": goal_id,
                "from": old_state,
                "to": to_state,
                "reason": reason[:200],
                "goal_version": goal.version,
                "actor": actor,
            })
            # ADR-015 addendum (Phase A): TERMINAL transitions mark the active
            # (latest) plan version's outcome - succeeded on completion, failed
            # on failure. Informational, best-effort, idempotent (UNIQUE
            # goal_id+plan_version); never breaks the state machine.
            #
            # ADR-056: when the atomic path ran, ``validated_plan_version`` is
            # the plan version that was authoritative at the moment the goal CAS
            # committed (returned by cas_goal_terminal_fenced).  Use it directly
            # instead of a second post-commit latest_plan() re-read so that the
            # strategy outcome correctly records the version that was authoritative
            # at transition time, not any newer version committed in the interim.
            if to_state in (GoalStatus.COMPLETED.value, GoalStatus.FAILED.value):
                if validated_plan_version is not None:
                    # ADR-056 path: use the authoritative plan version from the
                    # atomic commit.  Read strategy from cognitive store for the
                    # outcome record (informational, best-effort, idempotent).
                    latest = self.latest_plan(goal_id)
                    strategy = (latest.get("strategy", "") if latest else "") or ""
                    outcome_str = ("succeeded" if to_state == GoalStatus.COMPLETED.value
                                   else "failed")
                    self._record_strategy_outcome(
                        goal_id, validated_plan_version, strategy, outcome_str, reason)
                else:
                    # Original path: re-read latest_plan post-commit.
                    latest = self.latest_plan(goal_id)
                    if latest is not None:
                        outcome_str = ("succeeded" if to_state == GoalStatus.COMPLETED.value
                                       else "failed")
                        self._record_strategy_outcome(
                            goal_id, latest["plan_version"],
                            latest.get("strategy", ""), outcome_str, reason)
            return goal
        raise self._contention_error(goal_id, "transition")

    def pause(self, goal_id: str, reason: str = "explicit_pause") -> Goal:
        return self.transition(goal_id, GoalStatus.PAUSED.value, reason)

    def resume(self, goal_id: str, reason: str = "explicit_resume") -> Goal:
        return self.transition(goal_id, GoalStatus.ACTIVE.value, reason)

    def cancel(self, goal_id: str, reason: str = "explicit_cancel") -> Goal:
        return self.transition(goal_id, GoalStatus.CANCELLED.value, reason)

    def fail_goal(self, goal_id: str, reason: str = "goal_failed",
                  expect_plan_version: int | None = None) -> Goal:
        """Terminal failure; ADR-055: pass the immutable plan version the
        failure decision evaluated (``evidence["latest_plan_version"]``) so
        the ADR-054 transition-level lineage fence can refuse stale plan
        authority (same mechanism as completion fencing)."""
        return self.transition(
            goal_id, GoalStatus.FAILED.value, reason,
            fields={"last_replan_reason": reason},
            expect_plan_version=expect_plan_version,
        )

    def complete_goal(self, goal_id: str, reason: str = "all_work_complete",
                      expect_plan_version: int | None = None) -> Goal:
        """Terminal completion; ADR-054: pass the immutable plan version the
        completion decision evaluated (``evidence["latest_plan_version"]``)
        so the transition can refuse stale plan authority."""
        return self.transition(goal_id, GoalStatus.COMPLETED.value, reason,
                               expect_plan_version=expect_plan_version)

    def set_blocked(self, goal_id: str, blocker: dict[str, Any], reason: str = "blocker") -> Goal:
        """Attach a blocker (upsert by key) and move ACTIVE -> BLOCKED.

        Distinct keys merge across concurrent writers. The same key
        upserts in place and preserves the original ``added_at``.
        ACTIVE -> BLOCKED is applied in the same CAS as the blocker
        write when still legal after a reload; PAUSED / terminal
        statuses keep their lifecycle and only merge the blocker.
        """
        key = blocker.get("key") or blocker.get("type") or blocker.get("reason") or "blocker"
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            expected = goal.version
            old_state = goal.status.value
            blockers = list(goal.blockers or [])
            existing_idx = next(
                (i for i, b in enumerate(blockers)
                 if (b.get("key") or b.get("type")) == key),
                None,
            )
            changed = False
            if existing_idx is None:
                blockers.append({**blocker, "key": key, "added_at": utcnow()})
                changed = True
            else:
                kept = blockers[existing_idx]
                updated = {**kept, **blocker, "key": key,
                           "added_at": kept.get("added_at") or utcnow()}
                if updated != kept:
                    blockers[existing_idx] = updated
                    changed = True
            transitioned = False
            if goal.status == GoalStatus.ACTIVE:
                allowed = GOAL_TRANSITIONS[goal.status.value]
                if GoalStatus.BLOCKED.value not in allowed:
                    raise GoalStateError(
                        f"invalid goal transition {goal.status.value!r} -> "
                        f"{GoalStatus.BLOCKED.value!r} for goal {goal_id}"
                    )
                goal.status = GoalStatus.BLOCKED
                transitioned = True
                changed = True
            if not changed:
                self._emit("goal.blocked", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "blocker_key": key,
                    "blocker_type": blocker.get("type", key),
                    "reason": reason[:200],
                })
                return goal
            goal.blockers = blockers
            goal.version = expected + 1
            goal.updated_at = utcnow()
            if not self._commit_goal(goal, expected):
                continue
            if transitioned:
                self._emit("goal.state.changed", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "from": old_state,
                    "to": GoalStatus.BLOCKED.value,
                    "reason": reason[:200],
                    "goal_version": goal.version,
                    "actor": "system",
                })
            self._emit("goal.blocked", goal_id=goal_id, detail={
                "goal_id": goal_id,
                "blocker_key": key,
                "blocker_type": blocker.get("type", key),
                "reason": reason[:200],
            })
            return goal
        raise self._contention_error(goal_id, "set_blocked")

    def clear_blocker(self, goal_id: str, key: str, reason: str = "blocker_resolved") -> Goal:
        """Remove ONE blocker by key; unblocks the goal when none remain."""
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            kept = [b for b in (goal.blockers or []) if (b.get("key") or b.get("type")) != key]
            if len(kept) == len(goal.blockers or []):
                return goal  # nothing to clear
            expected = goal.version
            old_state = goal.status.value
            goal.blockers = kept
            transitioned = False
            if goal.status == GoalStatus.BLOCKED and not kept:
                allowed = GOAL_TRANSITIONS[goal.status.value]
                if GoalStatus.ACTIVE.value not in allowed:
                    raise GoalStateError(
                        f"invalid goal transition {goal.status.value!r} -> "
                        f"{GoalStatus.ACTIVE.value!r} for goal {goal_id}"
                    )
                goal.status = GoalStatus.ACTIVE
                transitioned = True
            goal.version = expected + 1
            goal.updated_at = utcnow()
            if not self._commit_goal(goal, expected):
                continue
            self._emit("goal.unblocked", goal_id=goal_id, detail={
                "goal_id": goal_id, "blocker_key": key, "reason": reason[:200],
            })
            if transitioned:
                self._emit("goal.state.changed", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "from": old_state,
                    "to": GoalStatus.ACTIVE.value,
                    "reason": reason[:200],
                    "goal_version": goal.version,
                    "actor": "system",
                })
            return goal
        raise self._contention_error(goal_id, "clear_blocker")

    def clear_blockers(self, goal_id: str, reason: str = "blocker_resolved") -> Goal:
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            if not goal.blockers:
                return goal
            expected = goal.version
            old_state = goal.status.value
            goal.blockers = []
            transitioned = False
            if goal.status == GoalStatus.BLOCKED:
                allowed = GOAL_TRANSITIONS[goal.status.value]
                if GoalStatus.ACTIVE.value not in allowed:
                    raise GoalStateError(
                        f"invalid goal transition {goal.status.value!r} -> "
                        f"{GoalStatus.ACTIVE.value!r} for goal {goal_id}"
                    )
                goal.status = GoalStatus.ACTIVE
                transitioned = True
            goal.version = expected + 1
            goal.updated_at = utcnow()
            if not self._commit_goal(goal, expected):
                continue
            self._emit("goal.unblocked", goal_id=goal_id, detail={
                "goal_id": goal_id, "blocker_key": "*", "reason": reason[:200],
            })
            if transitioned:
                self._emit("goal.state.changed", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "from": old_state,
                    "to": GoalStatus.ACTIVE.value,
                    "reason": reason[:200],
                    "goal_version": goal.version,
                    "actor": "system",
                })
            return goal
        raise self._contention_error(goal_id, "clear_blockers")

    def recheck_blockers(self, goal_id: str) -> bool:
        """Re-evaluate the goal's blockers against the CURRENT world state.

        Drops a `missing_capability` blocker whose required capabilities are
        now registered, and an `approval_pending` blocker whose task is no
        longer awaiting approval. Returns True when blockers were cleared
        (the goal may need re-evaluation/replanning); False otherwise.
        """
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None or goal.status != GoalStatus.BLOCKED or not goal.blockers:
                return False
            world = self.world_monitor.current_state() if self.world_monitor else {}
            reg = world.get("registered_capabilities") or {}
            caps = list(reg.get("value", [])) if isinstance(reg, dict) else []
            dropped_keys: set[str] = set()
            newly_available: set[str] = set()
            for b in list(goal.blockers):
                key = b.get("key") or b.get("type")
                if key == "missing_capability":
                    need = list(b.get("capabilities") or [])
                    if need and all(c in caps for c in need):
                        dropped_keys.add(key)
                        newly_available.update(c for c in need if c in caps)
                elif key == "approval_pending":
                    tid = b.get("task_id")
                    task = self.storage.load_task(tid) if tid else None
                    if task is None or task.status != TaskStatus.AWAITING_APPROVAL:
                        dropped_keys.add(key)
                elif key == "lock_contention":
                    # ADR-021: the blocker clears when the mutation resource is no
                    # longer actively locked (resolved via the engine's live lock
                    # store - the lock store is the only lock authority).
                    if self.lock_contention_resolver is not None and self.lock_contention_resolver(b):
                        dropped_keys.add(key)
                elif key == "recovery_required":
                    # ADR-041: acknowledgement may commit before goal-blocker
                    # cleanup. The recovery registry remains authoritative and
                    # restart reconciliation drops only a stale mirror.
                    if (self.recovery_required_resolver is not None
                            and self.recovery_required_resolver(b)):
                        dropped_keys.add(key)
            if not dropped_keys:
                return False
            expected = goal.version
            old_state = goal.status.value
            goal.blockers = [
                b for b in (goal.blockers or [])
                if (b.get("key") or b.get("type")) not in dropped_keys
            ]
            transitioned = False
            if not goal.blockers:
                allowed = GOAL_TRANSITIONS[goal.status.value]
                if GoalStatus.ACTIVE.value not in allowed:
                    raise GoalStateError(
                        f"invalid goal transition {goal.status.value!r} -> "
                        f"{GoalStatus.ACTIVE.value!r} for goal {goal_id}"
                    )
                goal.status = GoalStatus.ACTIVE
                transitioned = True
            goal.version = expected + 1
            goal.updated_at = utcnow()
            if not self._commit_goal(goal, expected):
                continue
            for cap in sorted(newly_available):
                self._emit("capability.available", goal_id=goal_id, detail={
                    "goal_id": goal_id, "capability": cap, "source": "world_state",
                })
            if transitioned:
                self._emit("goal.unblocked", goal_id=goal_id, detail={
                    "goal_id": goal_id, "blocker_key": ",".join(sorted(dropped_keys)),
                    "reason": "blockers_resolved",
                })
                self._emit("goal.state.changed", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "from": old_state,
                    "to": GoalStatus.ACTIVE.value,
                    "reason": "blockers_resolved",
                    "goal_version": goal.version,
                    "actor": "system",
                })
            return True
        raise self._contention_error(goal_id, "recheck_blockers")

    # ------------------------------------------------------------------ #
    # Plan versioning (immutable, monotonic, replay-safe)
    # ------------------------------------------------------------------ #

    def next_plan_version(self, goal_id: str) -> int:
        latest = self.cognitive_store.latest_goal_plan(goal_id) if self.cognitive_store else None
        return (latest["plan_version"] + 1) if latest else 1

    def plan_history(self, goal_id: str) -> list[dict[str, Any]]:
        if self.cognitive_store is None:
            return []
        return self.cognitive_store.list_goal_plans(goal_id)

    def latest_plan(self, goal_id: str) -> dict[str, Any] | None:
        if self.cognitive_store is None:
            return None
        return self.cognitive_store.latest_goal_plan(goal_id)

    def record_plan_version(
        self,
        goal_id: str,
        strategy: str,
        plan_summary: list[dict],
        reason: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Record a NEW (immutable) plan version for a goal.

        `source` (ADR-057 D3, additive): "model" | "deterministic" | "stored"
        provenance for the plan.versioned event. None omits the key.

        Delegates version allocation to the authoritative store funnel
        ``claim_goal_plan`` (one ``BEGIN IMMEDIATE`` transaction,
        ``MAX(plan_version)+1``, plain INSERT — never
        ``INSERT OR REPLACE``). Replay-safe: if the LATEST plan version
        already matches (strategy, plan_summary, reason) AND no task
        implements it yet, the existing version is adopted instead of
        creating a duplicate. A task that already references the latest
        equivalent plan triggers a genuinely NEW version. Previous plans
        are never mutated. Only the creator of a new version emits
        ``plan.versioned``.

        Returns the plan record {goal_id, plan_version, strategy, plan_summary,
        reason, created_at}.
        """
        # Version allocation is NOT owned here. The store claim is the
        # single authoritative write path (BEGIN IMMEDIATE + MAX+1 +
        # plain INSERT). This method only supplies the implementing-task
        # snapshot, then applies informational follow-up (strategy
        # outcome, goal.strategy, plan.versioned) when a NEW version was
        # actually created.
        implemented_versions = {
            t.plan_version
            for t in self.storage.list_tasks()
            if t.goal_id == goal_id and t.plan_version is not None
        }
        latest = self.latest_plan(goal_id)
        result = self.cognitive_store.claim_goal_plan(
            goal_id, strategy, plan_summary, reason,
            implemented_versions=implemented_versions,
        )
        record = result.plan
        if not result.created:
            return record  # replay / identical concurrent adopt: no event
        # ADR-015 addendum (Phase A): the new plan version SUPERSEDES the
        # previous one. Informational, best-effort, idempotent - never
        # breaks the authoritative plan-versioning path. A crash between
        # the plan insert and this outcome write is healed by
        # repair_strategy_outcomes (the plan lineage is already durable).
        if latest is not None:
            self._record_strategy_outcome(
                goal_id, latest["plan_version"],
                latest.get("strategy", ""), "superseded", reason)
        # The goal's CURRENT strategy follows the latest plan version
        # (persisted, restart-safe, still purely informational).
        goal = self.get_goal(goal_id)
        if goal is not None and goal.strategy != strategy:
            try:
                self._patch_goal(
                    goal_id, lambda g: setattr(g, "strategy", strategy),
                    columns=("strategy",), op="record_plan_version")
            except GoalStateError:
                pass  # informational follow-up; plan lineage already committed
        detail: dict[str, Any] = {
            "goal_id": goal_id,
            "plan_version": record["plan_version"],
            "strategy": strategy,
            "reason": reason[:200],
            "steps": len(plan_summary),
        }
        if source is not None:  # ADR-057 D3 additive source marker
            detail["source"] = source
        self._emit("plan.versioned", goal_id=goal_id, detail=detail)
        return record

    def _any_task_for_plan(self, goal_id: str, plan_version: int) -> bool:
        for task in self.storage.list_tasks():
            if task.goal_id == goal_id and task.plan_version == plan_version:
                return True
        return False

    def readopt_plan(self, goal_id: str, from_version: int) -> dict[str, Any]:
        """Re-adopt a stored historical plan version (ADR-016 addendum).

        Creates a NEW immutable plan version whose (strategy, plan_summary)
        are copied from the stored historical version `from_version`,
        through the EXISTING record_plan_version funnel - so replay
        idempotency, the monotonic version counter, the goal.strategy
        follow, the plan.versioned event, and the ADR-015 strategy-outcome
        supersede (previous latest -> 'superseded' with reason
        'replan_rollback_v<N>') all behave exactly as for any replan.
        History is NEVER mutated; the new version's reason is exactly
        `replan_rollback_v<from_version>` (provenance-carrying).

        Fail closed (ValueError) when:
        - the goal does not exist;
        - the goal is terminal (COMPLETED or CANCELLED - no outgoing
          transitions); FAILED stays eligible (GOAL_TRANSITIONS allows
          FAILED -> ACTIVE);
        - from_version is not a positive integer;
        - the goal has no plan history;
        - from_version does not exist in THIS goal's plan history (never
          existed, or pruned by ADR-014 - pruned versions are
          indistinguishable from never-existing and are NOT resurrected);
        - from_version is the latest plan version (nothing to re-adopt);
        - the stored plan's strategy is not a known STRATEGY_NAME or its
          plan_summary is malformed/oversized (raw-SQL-forged rows fail
          closed).

        Idempotent: repeating the identical re-adoption (no task yet)
        returns the existing rollback version via the replay guard; if a
        task implements that version, a genuinely NEW version is created
        (existing record_plan_version semantics). Re-adoption only RECORDS
        - stored-plan execution is a separate phase.
        """
        if (isinstance(from_version, bool) or not isinstance(from_version, int)
                or from_version < 1):
            raise ValueError(
                f"from_version must be a positive integer, got "
                f"{from_version!r} (fail closed)")
        goal = self.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"goal not found: {goal_id!r} (fail closed)")
        if goal.status_value in (GoalStatus.COMPLETED.value,
                                 GoalStatus.CANCELLED.value):
            raise ValueError(
                f"goal {goal_id} is terminal ({goal.status_value}); "
                f"re-adoption is not allowed (fail closed)")
        history = self.plan_history(goal_id)
        if not history:
            raise ValueError(
                f"goal {goal_id} has no plan history to re-adopt (fail closed)")
        source = next((p for p in history
                       if p["plan_version"] == from_version), None)
        if source is None:
            raise ValueError(
                f"plan version {from_version} does not exist for goal "
                f"{goal_id} (never existed or pruned; fail closed)")
        latest = history[-1]
        if from_version == latest["plan_version"]:
            raise ValueError(
                f"plan version {from_version} is already the active plan "
                f"for goal {goal_id} (fail closed)")
        strategy = source.get("strategy", "") or ""
        if strategy not in STRATEGY_NAMES:
            raise ValueError(
                f"stored plan {from_version} has unknown strategy "
                f"{strategy!r}; cannot re-adopt (fail closed)")
        summary = source.get("plan_summary")
        if (not isinstance(summary, list)
                or not all(isinstance(s, dict) for s in summary)
                or len(summary) > 500):
            raise ValueError(
                f"stored plan {from_version} has a malformed or oversized "
                f"plan_summary; cannot re-adopt (fail closed)")
        return self.record_plan_version(
            goal_id, strategy, [dict(s) for s in summary],
            reason=f"replan_rollback_v{from_version}")

    def task_history(self, goal_id: str) -> list[Task]:
        return [t for t in self.storage.list_tasks() if t.goal_id == goal_id]

    def progress(self, goal_id: str) -> dict[str, Any]:
        """Per-goal task progress counts (completion is NOT inferred here -
        see ProgressEvaluator for the authoritative evaluation)."""
        tasks = self.task_history(goal_id)
        return {
            "goal_id": goal_id,
            "tasks": len(tasks),
            "completed": sum(1 for t in tasks if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
            "pending": sum(1 for t in tasks if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)),
        }

    def pending_task(self, goal_id: str) -> Task | None:
        """The non-terminal CANONICAL task implementing the LATEST plan version.

        Resume it on restart (replay safety) instead of duplicating work. A
        stale pending task for an older plan version is NOT resumed - after a
        replan, a fresh task for the new version is created. ADR-053: when
        retained historical duplicates of the exact version exist, only the
        deterministic canonical task (ADR-051 selection: ordered by
        (created_at, task_id)) is current; noncanonical duplicates are never
        selected as the pending task."""
        latest = self.latest_plan(goal_id)
        latest_version = latest["plan_version"] if latest else None
        history = self.task_history(goal_id)
        if latest_version is None:
            for t in history:
                if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    return t
            return None
        canonical = canonical_exact_task(history, latest_version)
        if canonical is not None and canonical.status not in (
                TaskStatus.COMPLETED, TaskStatus.FAILED):
            return canonical
        return None

    # ------------------------------------------------------------------ #
    # Progress evaluation
    # ------------------------------------------------------------------ #

    def evaluate(self, goal_id: str) -> tuple[ProgressResult, Goal]:
        """Deterministic progress evaluation; updates goal.last_evaluated_at.

        Emits progress.evaluated + goal.evaluated with bounded metadata.
        """
        # Persist the snapshot via CAS so a concurrent lifecycle transition
        # cannot be rolled back by this metadata write. On a miss we reload
        # and re-evaluate against the latest status/blockers.
        for _ in range(_GOAL_CAS_MAX_ATTEMPTS):
            goal = self.get_goal(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            tasks = self.task_history(goal_id)
            latest_plan = self.latest_plan(goal_id)
            world_changes = self._relevant_world_changes(goal)
            world_state = self.world_monitor.current_state() if self.world_monitor else None
            last_result = self.progress_evaluator.evaluate(
                goal, tasks, latest_plan, world_changes, world_state)
            expected = goal.version
            goal.progress_metadata = last_result.to_dict()
            goal.last_evaluated_at = utcnow()
            goal.updated_at = utcnow()
            casf = getattr(self.storage, "cas_goal_fields", None)
            if casf is not None:
                ok = casf(goal.id, expected, {
                    "progress_metadata": goal.progress_metadata,
                    "last_evaluated_at": goal.last_evaluated_at,
                    "updated_at": goal.updated_at,
                })
            else:
                goal.version = expected + 1
                ok = self._commit_goal(goal, expected)
            if not ok:
                continue
            self._emit("progress.evaluated", goal_id=goal_id, detail=last_result.to_dict())
            self._emit("goal.evaluated", goal_id=goal_id, detail={
                "goal_id": goal_id,
                "next_action": last_result.next_action,
                "status": last_result.status,
                "progress": round(last_result.progress, 3),
                "evidence_reason": last_result.evidence.get("reason"),
            })
            return last_result, self.get_goal(goal_id)
        raise self._contention_error(goal_id, "evaluate")

    def _relevant_world_changes(self, goal: Goal) -> list:
        """Deterministic relevance filter: only changes to facts the goal's
        plan depends on (capabilities, or keys mentioned in the plan summary)
        are treated as material. Unrelated changes do NOT trigger replan."""
        if self.world_monitor is None:
            return []
        changes = self.world_monitor.changed_since(goal.last_evaluated_at or goal.created_at)
        if not changes:
            return []
        latest = self.latest_plan(goal.id)
        plan_text = ""
        if latest is not None:
            try:
                plan_text = str(latest.get("plan_summary", [])).lower()
            except Exception:
                plan_text = ""
        relevant = []
        for change in changes:
            key = change.key
            if key == "registered_capabilities":
                relevant.append(change)
            elif key.lower() in plan_text or key in goal.description.lower():
                relevant.append(change)
        return relevant

    def strategy_for(self, goal_id: str, goal_description: str, beliefs: list,
                     environment: dict, guidance: list) -> Any:
        """Select (and persist) the goal's current strategy with provenance."""
        previous = [p.get("strategy", "") for p in self.plan_history(goal_id)]
        outcome_history = self.strategy_outcomes(limit=50)
        strategy = self.strategy_selector.select(
            goal_description, beliefs, environment, guidance,
            previous_strategies=[s for s in previous if s],
            outcome_history=outcome_history,
        )
        goal = self.get_goal(goal_id)
        if goal is not None and goal.strategy != strategy.name:
            self._patch_goal(
                goal_id, lambda g: setattr(g, "strategy", strategy.name),
                columns=("strategy",), op="strategy_for")
        return strategy

    def summarize(self, goal_id: str) -> dict[str, Any]:
        goal = self.get_goal(goal_id)
        if goal is None:
            return {"goal_id": goal_id, "exists": False}
        latest = self.latest_plan(goal_id)
        return {
            "goal_id": goal_id,
            "exists": True,
            "description": goal.description[:300],
            "status": goal.status_value,
            "goal_version": goal.version,
            "strategy": goal.strategy,
            "blockers": goal.blockers,
            "plan_versions": len(self.plan_history(goal_id)),
            "latest_plan_version": latest["plan_version"] if latest else None,
            "latest_strategy": latest["strategy"] if latest else None,
            "latest_reason": latest["reason"] if latest else None,
            "progress": goal.progress_metadata,
            "tasks": len(self.task_history(goal_id)),
            "created_at": goal.created_at,
            "updated_at": goal.updated_at,
        }

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    # ---- strategy outcomes (ADR-015 addendum, Phase A) ----

    def peek_evaluate(self, goal_id: str) -> ProgressResult | None:
        """READ-ONLY progress evaluation (ADR-016 addendum Phase D).

        Computes the same deterministic ProgressResult as evaluate()
        WITHOUT persisting progress_metadata / last_evaluated_at /
        updated_at and WITHOUT emitting progress.evaluated /
        goal.evaluated. The authoritative lifecycle (engine run_goal)
        keeps using the mutating evaluate(); this public seam serves
        read-only CLI inspection. Returns None when the goal is missing.
        """
        goal = self.get_goal(goal_id)
        if goal is None:
            return None
        tasks = self.task_history(goal_id)
        latest_plan = self.latest_plan(goal_id)
        try:
            world_changes = self._relevant_world_changes(goal)
        except Exception:
            world_changes = []
        world_state = (self.world_monitor.current_state()
                       if self.world_monitor is not None else None)
        return self.progress_evaluator.evaluate(
            goal, tasks, latest_plan, world_changes, world_state)

    def diff_plans(self, goal_id: str, version_a: int,
                   version_b: int) -> dict[str, Any]:
        """Deterministic, read-only structural diff of two stored plan
        versions (ADR-016 addendum Phase C).

        Compares strategy, reason, and step structure (index/capability/
        action only - NEVER params/intent/free-text content). Stable JSON
        schema: {goal_id, version_a, version_b, strategy_a, strategy_b,
        reason_a, reason_b, steps_a, steps_b, added, removed, kept,
        identical}. Purely observational: no DB writes, no events, no
        planner, no execution. Fail closed (ValueError) on nonexistent
        goal, nonexistent/pruned/malformed versions, or invalid version
        argument types. Identical versions -> explicit empty diff.
        """
        if (isinstance(version_a, bool) or not isinstance(version_a, int)
                or version_a < 1 or isinstance(version_b, bool)
                or not isinstance(version_b, int) or version_b < 1):
            raise ValueError(
                f"versions must be positive integers, got {version_a!r} and "
                f"{version_b!r} (fail closed)")
        if self.get_goal(goal_id) is None:
            raise ValueError(f"goal not found: {goal_id!r} (fail closed)")
        history = self.plan_history(goal_id)
        pa = next((p for p in history if p["plan_version"] == version_a), None)
        pb = next((p for p in history if p["plan_version"] == version_b), None)
        if pa is None or pb is None:
            raise ValueError(
                f"plan version(s) {version_a}/{version_b} do not exist for "
                f"goal {goal_id} (never existed or pruned; fail closed)")

        def _sig(step: dict[str, Any]) -> tuple:
            # structural identity: index + capability + action only
            return (step.get("index"), step.get("capability"),
                    step.get("action"))

        sa = [_sig(s) for s in (pa.get("plan_summary") or [])]
        sb = [_sig(s) for s in (pb.get("plan_summary") or [])]
        ka = {s: i for i, s in enumerate(sa)}
        kb = {s: i for i, s in enumerate(sb)}
        kept = sorted(i for i, s in enumerate(sa) if s in kb)
        added = sorted(i for i, s in enumerate(sb) if s not in ka)
        removed = sorted(i for i, s in enumerate(sa) if s not in kb)
        return {
            "goal_id": goal_id,
            "version_a": version_a,
            "version_b": version_b,
            "strategy_a": pa.get("strategy", ""),
            "strategy_b": pb.get("strategy", ""),
            "reason_a": (pa.get("reason") or "")[:100],
            "reason_b": (pb.get("reason") or "")[:100],
            "steps_a": len(sa),
            "steps_b": len(sb),
            "added": added,
            "removed": removed,
            "kept": kept,
            "identical": (sa == sb and pa.get("strategy") == pb.get("strategy")),
        }

    def _record_strategy_outcome(self, goal_id: str, plan_version: int,
                                 strategy: str, outcome: str,
                                 reason: str) -> bool:
        """Write one informational strategy-outcome row, best-effort.

        Called ONLY from the authoritative GoalManager lifecycle funnels
        (record_plan_version / transition) and the repair pass. Returns
        True when the write was a durable change (new row or value
        change); emits the bounded `strategy.outcome` audit event then.
        Idempotent replays return False and emit NOTHING. A failure here
        must never break the authoritative state machine, so exceptions
        are swallowed (returning False) - the deterministic repair pass
        re-derives missing rows from authoritative goal/plan state.
        """
        if self.cognitive_store is None:
            return False
        try:
            goal = self.get_goal(goal_id)
            description = goal.description if goal is not None else ""
            changed = self.cognitive_store.record_strategy_outcome(
                goal_id, description, strategy, plan_version, outcome,
                reason=reason)
            if changed:
                self._emit("strategy.outcome", goal_id=goal_id, detail={
                    "goal_id": goal_id,
                    "plan_version": plan_version,
                    "strategy": strategy,
                    "outcome": outcome,
                    "reason": reason[:200],
                })
            return changed
        except Exception:
            return False  # informational: never breaks the goal lifecycle

    def strategy_outcomes(self, goal_id: str | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
        """Bounded, read-only strategy-outcome history (deterministic order:
        goal_id, plan_version). Informational only."""
        if self.cognitive_store is None:
            return []
        return self.cognitive_store.list_strategy_outcomes(goal_id=goal_id,
                                                           limit=limit)

    def repair_strategy_outcomes(self) -> int:
        """Backfill MISSING strategy-outcome rows from AUTHORITATIVE state.

        Sources of truth: goals.status (terminal) + goal_plans (version
        order). For every plan version with a HIGHER version number the row
        is `superseded` (reason = the higher version's reason); the LATEST
        version of a COMPLETED goal is `succeeded`; of a FAILED goal is
        `failed`. Active/paused/blocked/cancelled latest versions and goals
        without plans get NO row. Existing rows are NEVER overwritten.
        Idempotent; deterministic; informational only.
        """
        if self.cognitive_store is None:
            return 0
        written = 0
        for goal in self.list_goals():
            plans = self.plan_history(goal.id)
            if not plans:
                continue
            for i, plan in enumerate(plans):
                version = int(plan["plan_version"])
                if self.cognitive_store.get_strategy_outcome(goal.id, version) \
                        is not None:
                    continue  # existing rows are never overwritten
                strategy = plan.get("strategy", "") or ""
                if strategy not in STRATEGY_NAMES:
                    continue  # no valid strategy to record (fail closed)
                if i < len(plans) - 1:
                    next_reason = plans[i + 1].get("reason", "") or ""
                    if self._record_strategy_outcome(
                            goal.id, version, strategy, "superseded",
                            next_reason):
                        written += 1
                elif goal.status_value == GoalStatus.COMPLETED.value:
                    if self._record_strategy_outcome(
                            goal.id, version, strategy, "succeeded",
                            "all_work_complete"):
                        written += 1
                elif goal.status_value == GoalStatus.FAILED.value:
                    if self._record_strategy_outcome(
                            goal.id, version, strategy, "failed",
                            goal.last_replan_reason or "goal_failed"):
                        written += 1
                else:
                    continue
        return written

    def _emit(self, kind: str, goal_id: str | None, detail: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            from arion.observability.events import AuditEvent

            self.events.emit(AuditEvent(kind=kind, task_id=None, success=True, detail=detail))
        except Exception:
            pass
