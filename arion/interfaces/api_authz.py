"""Centralized HTTP API authorization (ADR-059 D13).

The M6-A surface authenticated inline and had exactly one privilege level:
possession of any valid token authorized every action. M6-B adds
administrative endpoints that create network egress destinations and read
delivery metadata, so a single flat token tier would silently promote every
approver into a webhook administrator.

The design deliberately keeps privilege OUT of the token grammar:

  * `ARION_API_TOKENS`   -> APPROVER credentials (grammar UNCHANGED, so
                            existing deployments and `tests/test_approval_api.py`
                            keep working verbatim).
  * `ARION_API_ADMIN_TOKENS` -> ADMIN credentials, a SEPARATE surface.

A role-bearing grammar such as `token:kind:name:role` was rejected (ADR-059
R4): it makes privilege escalation a one-word edit inside an existing
variable, and it silently changes the meaning of tokens already deployed.
Two variables mean granting admin is a distinct, visible act.

Enforcement happens at ONE point (`authorize`), and each route DECLARES the
privilege it requires. Scattering `if actor.is_admin` checks through handler
bodies is how authorization gaps appear.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from arion.orchestration.authz import Actor

ENV_API_TOKENS = "ARION_API_TOKENS"
ENV_API_ADMIN_TOKENS = "ARION_API_ADMIN_TOKENS"


class APIConfigError(Exception):
    """Malformed API credential configuration (fail closed)."""


class Privilege(str, Enum):
    """Privilege levels, ordered: ADMIN strictly implies APPROVER."""

    APPROVER = "approver"
    ADMIN = "admin"


_IMPLIES: dict[Privilege, frozenset[Privilege]] = {
    # An admin can do anything an approver can (ADR-059 D13): operators
    # should not need two tokens to operate one system.
    Privilege.ADMIN: frozenset({Privilege.ADMIN, Privilege.APPROVER}),
    Privilege.APPROVER: frozenset({Privilege.APPROVER}),
}


@dataclass(frozen=True)
class AuthContext:
    """Who the caller is and what they are allowed to do."""

    actor: Actor
    privilege: Privilege

    def has(self, required: Privilege) -> bool:
        return required in _IMPLIES[self.privilege]


def _parse_token_map(raw: str, *, variable: str) -> dict[str, Actor]:
    """Parse `token:kind:name,...`. Grammar is identical for both variables."""
    tokens: dict[str, Actor] = {}
    if not raw.strip():
        return tokens
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        segments = part.split(":", 2)
        if len(segments) != 3:
            raise APIConfigError(
                f"Malformed token config in {variable} (expected token:kind:name)"
            )
        token, kind, name = segments
        if not token or not kind or not name:
            raise APIConfigError(
                f"Token, kind, and name must be non-empty in {variable}"
            )
        if token in tokens:
            raise APIConfigError(f"Duplicate token defined in {variable}")
        tokens[token] = Actor(kind=kind, name=name)
    return tokens


class TokenRegistry:
    """Both credential surfaces plus the single authentication routine."""

    def __init__(
        self,
        approver_tokens: Mapping[str, Actor] | None = None,
        admin_tokens: Mapping[str, Actor] | None = None,
    ) -> None:
        self._approvers = dict(approver_tokens or {})
        self._admins = dict(admin_tokens or {})
        overlap = set(self._approvers) & set(self._admins)
        if overlap:
            # Ambiguous privilege is a configuration ERROR, not something to
            # resolve by silently picking the higher (or lower) level.
            raise APIConfigError(
                f"token(s) present in both {ENV_API_TOKENS} and "
                f"{ENV_API_ADMIN_TOKENS}; a credential must have exactly one "
                f"privilege level"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TokenRegistry":
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            _parse_token_map(source.get(ENV_API_TOKENS, ""), variable=ENV_API_TOKENS),
            _parse_token_map(
                source.get(ENV_API_ADMIN_TOKENS, ""), variable=ENV_API_ADMIN_TOKENS
            ),
        )

    @property
    def has_admin_credentials(self) -> bool:
        return bool(self._admins)

    def authenticate(self, authorization_header: str | None) -> AuthContext | None:
        """Resolve a Bearer header to an AuthContext, or None.

        Both maps are always scanned with `compare_digest` and no early
        return on the first map, so response timing does not reveal which
        credential surface a token belongs to.
        """
        if not authorization_header:
            return None
        parts = authorization_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        provided = parts[1]

        found: AuthContext | None = None
        for token, actor in self._admins.items():
            if hmac.compare_digest(provided, token):
                found = AuthContext(actor=actor, privilege=Privilege.ADMIN)
        for token, actor in self._approvers.items():
            if hmac.compare_digest(provided, token):
                if found is None:
                    found = AuthContext(actor=actor, privilege=Privilege.APPROVER)
        return found


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of the single enforcement point."""

    context: AuthContext | None
    status: int  # 200 authorized, 401 unauthenticated, 403 unauthorized
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200


def authorize(
    registry: TokenRegistry,
    authorization_header: str | None,
    required: Privilege,
) -> AuthDecision:
    """The ONE place an HTTP request's privilege is decided.

    401 and 403 are kept distinct (ADR-059 D13): 401 means "we do not know
    who you are", 403 means "we do, and you may not do this". Collapsing
    them makes a misconfigured privilege level indistinguishable from a bad
    token during an incident.
    """
    context = registry.authenticate(authorization_header)
    if context is None:
        return AuthDecision(None, 401, "unauthorized")
    if required is Privilege.ADMIN and not registry.has_admin_credentials:
        # Fail closed: an empty admin map authorizes nobody, rather than
        # degrading to "any valid token" (ADR-059 D13).
        return AuthDecision(context, 403, "forbidden")
    if not context.has(required):
        return AuthDecision(context, 403, "forbidden")
    return AuthDecision(context, 200)
