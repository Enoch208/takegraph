"""Signed session-token adapter tests."""

from __future__ import annotations

import uuid

import pytest
from takegraph_api.auth import HmacSessionProvider, SessionClaims
from takegraph_domain.enums import Role
from takegraph_domain.errors import FeatureNotConfiguredError, UnauthenticatedError

NOW = 1_800_000_000
SECRET = "session-test-secret-that-is-long-enough"  # noqa: S105 — synthetic test key


def _claims(**overrides) -> SessionClaims:
    values = {
        "subject": "demo-owner",
        "actor_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "role": Role.OWNER,
        "project_scope_id": None,
        "issued_at": NOW - 10,
        "expires_at": NOW + 600,
        "nonce": "nonce-1",
    }
    values.update(overrides)
    return SessionClaims.model_validate(values)


def test_signed_token_round_trip() -> None:
    provider = HmacSessionProvider(SECRET, clock=lambda: NOW)
    claims = _claims()
    principal = provider.authenticate(f"Bearer {provider.issue(claims)}")

    assert principal.actor_id == claims.actor_id
    assert principal.organization_id == claims.organization_id
    assert principal.role is Role.OWNER


def test_token_is_scoped_to_a_project() -> None:
    provider = HmacSessionProvider(SECRET, clock=lambda: NOW)
    project_id = uuid.uuid4()
    principal = provider.authenticate(
        f"Bearer {provider.issue(_claims(role=Role.GUEST, project_scope_id=project_id))}"
    )
    assert principal.project_scope_id == project_id


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Bearer", "Bearer a b"])
def test_missing_or_malformed_bearer_is_rejected(authorization: str | None) -> None:
    with pytest.raises(UnauthenticatedError):
        HmacSessionProvider(SECRET, clock=lambda: NOW).authenticate(authorization)


def test_tampering_is_rejected() -> None:
    provider = HmacSessionProvider(SECRET, clock=lambda: NOW)
    token = provider.issue(_claims())
    payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    with pytest.raises(UnauthenticatedError):
        provider.authenticate(f"Bearer {payload[:-1]}{replacement}.{signature}")


def test_wrong_signing_secret_is_rejected() -> None:
    token = HmacSessionProvider(SECRET, clock=lambda: NOW).issue(_claims())
    with pytest.raises(UnauthenticatedError):
        HmacSessionProvider(
            "another-session-secret-that-is-long-enough", clock=lambda: NOW
        ).authenticate(f"Bearer {token}")


def test_expired_and_future_tokens_are_rejected() -> None:
    provider = HmacSessionProvider(SECRET, clock=lambda: NOW)
    for claims in (_claims(expires_at=NOW), _claims(issued_at=NOW + 31)):
        with pytest.raises(UnauthenticatedError):
            provider.authenticate(f"Bearer {provider.issue(claims)}")


def test_short_session_secret_fails_configuration() -> None:
    with pytest.raises(FeatureNotConfiguredError):
        HmacSessionProvider("short")


def test_provider_repr_does_not_expose_secret() -> None:
    assert SECRET not in repr(HmacSessionProvider(SECRET))
