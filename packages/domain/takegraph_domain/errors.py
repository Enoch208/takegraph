"""Typed domain errors (PRD §23.1: "Typed domain errors mapped once at API boundary").

Domain code raises these. The API layer translates them into the §9.8 envelope in
one place. Nothing here carries a stack trace, SQL, a secret, a provider body, or
an internal path — §9.8 forbids exposing any of that.
"""

from __future__ import annotations

from typing import Any

from takegraph_domain.enums import ApiErrorCode


class DomainError(Exception):
    """Base for every expected domain failure.

    `error_code` is what the API boundary maps to an HTTP status and the §9.8
    `error.code`. Subclasses override it; the default is deliberately INTERNAL_ERROR
    so an unclassified error can never masquerade as a benign one.
    """

    error_code: ApiErrorCode = ApiErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return self.message


class NotFoundError(DomainError):
    error_code = ApiErrorCode.NOT_FOUND


class ForbiddenError(DomainError):
    """§5.1 FR-AUTH-001: cross-tenant access is denied without revealing existence,
    so the API maps this to 404 for read paths."""

    error_code = ApiErrorCode.FORBIDDEN


class VersionConflictError(DomainError):
    error_code = ApiErrorCode.VERSION_CONFLICT


class ImpactPlanStaleError(DomainError):
    """§11.5: 409 when the revision, graph, policies, compiler version, or plan
    expiry changed since the preview was computed."""

    error_code = ApiErrorCode.IMPACT_PLAN_STALE


class BudgetExceededError(DomainError):
    error_code = ApiErrorCode.BUDGET_EXCEEDED


class ProviderUnavailableError(DomainError):
    error_code = ApiErrorCode.PROVIDER_UNAVAILABLE


class ProviderAuthError(DomainError):
    error_code = ApiErrorCode.PROVIDER_AUTH_FAILED


class ProviderQuotaError(DomainError):
    error_code = ApiErrorCode.PROVIDER_QUOTA


class AssetVerificationError(DomainError):
    """§8.3.6/§8.3.7: stored bytes did not match the recorded or declared hash."""

    error_code = ApiErrorCode.ASSET_VERIFICATION_FAILED


class FeatureNotConfiguredError(DomainError):
    """§24.5: a missing credential disables a capability explicitly. It never
    silently degrades to a fixture."""

    error_code = ApiErrorCode.FEATURE_NOT_CONFIGURED


class HumanReviewRequiredError(DomainError):
    error_code = ApiErrorCode.HUMAN_REVIEW_REQUIRED


class ReleaseNotReadyError(DomainError):
    error_code = ApiErrorCode.RELEASE_NOT_READY
