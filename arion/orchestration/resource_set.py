"""Derived resource views for multi-resource actions (ADR-061 D1/D2, M8 C2).

ONE authoritative declaration (`ActionSpec.resources`, ADR-061 D1) yields TWO
derived views, and neither derived view is independently authoritative:

  role view       ordered, duplicates retained, AS-DECLARED values
                  -> approval display, capability execution, recovery metadata

  canonical view  sorted set of (resource_kind, canonical_resource(kind, value))
                  -> fingerprinting, lock acquisition ordering

Both are produced HERE, from the same declaration, so a caller can never
approve one resource while locking another (the "approve A, lock B"
divergence class D1 exists to foreclose).

Invariants implemented: 1 (single authoritative declaration), 2 (canonical
identity is a (kind, resource) PAIR), 3 (deterministic order + dedup),
4 (duplicate roles retained in the role view), 13 (deterministic canonical
ordering for lock acquisition).

C2 is a DERIVATION layer only: nothing here makes an authorization, approval
or locking decision. Those consumers are rewired in C3-C7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arion.state.locks import canonical_resource


@dataclass(frozen=True)
class ResolvedResource:
    """One resource slot resolved against a step's params.

    `value` is the AS-DECLARED string exactly as the plan wrote it - ADR-061
    invariant 20: canonicalization must never alter what a human is shown.
    `canonical` is the lock/fingerprint identity derived from it.
    """

    role: str
    kind: str
    value: str | None
    canonical: str | None

    @property
    def resolved(self) -> bool:
        """True when the step actually supplied a usable value for this role."""
        return self.value is not None

    @property
    def identity(self) -> tuple[str, str] | None:
        """Canonical identity pair, or None when unresolved (invariant 2)."""
        if self.canonical is None:
            return None
        return (self.kind, self.canonical)


def resolve_resources(spec: Any, params: dict[str, Any]) -> list[ResolvedResource]:
    """Derive the ordered, role-preserving view (ADR-061 D1).

    Declaration order is preserved and duplicate VALUES are retained as
    distinct roles (invariant 4): ``move a -> a`` is two roles even though it
    is one canonical resource.

    A role whose param is missing or non-string resolves to value=None rather
    than raising: C2 only derives. Refusing to act on an unresolved resource
    is the job of the boundary check (C4) and the lock layer (C7), which must
    fail closed there.
    """
    roles = getattr(spec, "resources", None) or []
    out: list[ResolvedResource] = []
    for role in roles:
        raw = params.get(role.param)
        value = raw if isinstance(raw, str) and raw else None
        out.append(
            ResolvedResource(
                role=role.role,
                kind=role.kind,
                value=value,
                canonical=canonical_resource(role.kind, value) if value else None,
            )
        )
    return out


def canonical_identities(
    resolved: list[ResolvedResource],
) -> list[tuple[str, str]]:
    """Derive the canonical view: deterministically ordered, deduplicated.

    ADR-061 invariants 2, 3, 13. Identity is the PAIR ``(kind, canonical)`` -
    never a bare string - so ``filesystem:path "x"`` and a future ``url "x"``
    cannot collide (rejected alternative R6).

    Unresolved roles are omitted from the canonical view; callers that must
    not proceed with an unresolved resource check ``unresolved_roles()``.
    """
    ids = {r.identity for r in resolved if r.identity is not None}
    return sorted(ids)


def unresolved_roles(resolved: list[ResolvedResource]) -> list[str]:
    """Roles the step failed to supply a usable value for (fail-closed input).

    Returned so that C4/C7 can REFUSE rather than silently treating a missing
    resource as "nothing to check".
    """
    return [r.role for r in resolved if not r.resolved]


def primary_resource(resolved: list[ResolvedResource]) -> str | None:
    """The as-declared value of the first-declared role, or None.

    This is the single-resource compatibility view (ADR-061 D4/D9): existing
    readers that expect one `resource` keep seeing the primary role.
    """
    return resolved[0].value if resolved else None
