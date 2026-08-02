"""Bearer identity adapter for authenticated and scoped demo sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict, ValidationError
from takegraph_domain.auth import Permission, Principal
from takegraph_domain.enums import Role
from takegraph_domain.errors import FeatureNotConfiguredError, UnauthenticatedError

TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    subject: str
    actor_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    project_scope_id: uuid.UUID | None = None
    issued_at: int
    expires_at: int
    nonce: str


class IdentityProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


@dataclass(frozen=True, slots=True)
class HmacSessionProvider:
    secret: str = field(repr=False)
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise FeatureNotConfiguredError("SESSION_SECRET must contain at least 32 characters.")

    def issue(self, claims: SessionClaims) -> str:
        payload = _encode(
            json.dumps(
                claims.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        signature = _encode(
            hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).digest()
        )
        return f"{payload}.{signature}"

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer_token(authorization)
        parts = token.split(".")
        if len(parts) != 2 or any(TOKEN_PART.fullmatch(part) is None for part in parts):
            raise UnauthenticatedError("Session token is invalid.")
        payload, supplied_signature = parts
        expected_signature = _encode(
            hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise UnauthenticatedError("Session token is invalid.")
        try:
            claims = SessionClaims.model_validate_json(_decode(payload))
        except (ValueError, ValidationError) as exc:
            raise UnauthenticatedError("Session token is invalid.") from exc
        now = int(self.clock())
        if claims.issued_at > now + 30 or claims.expires_at <= now:
            raise UnauthenticatedError("Session token has expired or is not active.")
        return Principal(
            actor_id=claims.actor_id,
            subject=claims.subject,
            organization_id=claims.organization_id,
            role=claims.role,
            project_scope_id=claims.project_scope_id,
        )


def session_provider_from_env() -> HmacSessionProvider:
    return HmacSessionProvider(os.environ.get("SESSION_SECRET", ""))


def get_principal(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Principal:
    return session_provider_from_env().authenticate(authorization)


def require_permission(permission: Permission) -> Callable[[Principal], Principal]:
    """FastAPI dependency for role-only checks; project scope is checked in services."""

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        from takegraph_domain.auth import ROLE_PERMISSIONS

        if permission not in ROLE_PERMISSIONS[principal.role]:
            from takegraph_domain.errors import ForbiddenError

            raise ForbiddenError("Principal is not allowed to perform this action.")
        return principal

    return dependency


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise UnauthenticatedError("Bearer session is required.")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise UnauthenticatedError("Bearer session is invalid.")
    return token


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
