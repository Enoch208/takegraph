"""Domain enums, mirrored by PostgreSQL enums per PRD §8.1.

Every status string the system persists or streams is defined once, here. Route
handlers and the worker request transitions through domain services; they never
set these values directly (§10).
"""

from __future__ import annotations

from enum import StrEnum


class NodeType(StrEnum):
    """§4.2 seed graph plus §9.1 node specification."""

    SOURCE_TEXT = "SOURCE_TEXT"
    SOURCE_IMAGE = "SOURCE_IMAGE"
    IMAGE_TRANSFORM = "IMAGE_TRANSFORM"
    STRUCTURED_PLAN = "STRUCTURED_PLAN"
    STRUCTURED_TEXT = "STRUCTURED_TEXT"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    VIDEO_GENERATION = "VIDEO_GENERATION"
    AUDIO_GENERATION = "AUDIO_GENERATION"
    IMAGE_COMPOSITION = "IMAGE_COMPOSITION"
    MEDIA_COMPOSITION = "MEDIA_COMPOSITION"

    @property
    def is_source(self) -> bool:
        return self in (NodeType.SOURCE_TEXT, NodeType.SOURCE_IMAGE)

    @property
    def calls_provider(self) -> bool:
        """Whether executing this node costs a provider call. Composition and
        transforms run locally, so they are excluded from §9.5 provider_calls."""
        return self in (
            NodeType.STRUCTURED_PLAN,
            NodeType.STRUCTURED_TEXT,
            NodeType.IMAGE_GENERATION,
            NodeType.VIDEO_GENERATION,
            NodeType.AUDIO_GENERATION,
        )


class ImpactDecision(StrEnum):
    """§5.3 FR-IMPACT-002. Every non-reuse decision carries a machine code and a
    human-readable reason."""

    REUSE = "REUSE"
    REBUILD = "REBUILD"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


class ReasonCode(StrEnum):
    """§12.5 change reason codes. Exhaustive: no reason is ever a free string."""

    NODE_ADDED = "NODE_ADDED"
    NODE_REMOVED = "NODE_REMOVED"
    SOURCE_CONTENT_CHANGED = "SOURCE_CONTENT_CHANGED"
    NODE_SPEC_CHANGED = "NODE_SPEC_CHANGED"
    UPSTREAM_FINGERPRINT_CHANGED = "UPSTREAM_FINGERPRINT_CHANGED"
    PROVIDER_POLICY_CHANGED = "PROVIDER_POLICY_CHANGED"
    VALIDATION_POLICY_CHANGED = "VALIDATION_POLICY_CHANGED"
    TEMPLATE_VERSION_CHANGED = "TEMPLATE_VERSION_CHANGED"
    GENERATOR_CODE_CHANGED = "GENERATOR_CODE_CHANGED"
    CACHE_MISS = "CACHE_MISS"
    CACHE_ASSET_MISSING = "CACHE_ASSET_MISSING"
    CACHE_ASSET_UNVERIFIED = "CACHE_ASSET_UNVERIFIED"
    CACHE_VALIDATION_STALE = "CACHE_VALIDATION_STALE"
    MANUAL_INVALIDATION = "MANUAL_INVALIDATION"
    CONFIGURATION_BLOCKED = "CONFIGURATION_BLOCKED"
    EXACT_VALIDATED_REUSE = "EXACT_VALIDATED_REUSE"


class BuildStatus(StrEnum):
    """§10.1"""

    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED)


class BuildNodeStatus(StrEnum):
    """§10.2. REUSED and PASSED both satisfy dependencies; they differ in whether
    this build generated the bytes."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    STORING = "STORING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    REUSED = "REUSED"
    WAITING_REVIEW = "WAITING_REVIEW"
    RETRY_PENDING = "RETRY_PENDING"
    FALLBACK_PENDING = "FALLBACK_PENDING"
    RETAKE_PENDING = "RETAKE_PENDING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def satisfies_dependency(self) -> bool:
        """§12.6: a node is ready when predecessors are PASSED or REUSED. Note that
        a provider URL alone never gets a node here — §5.4 FR-BUILD-007 requires
        storage and validation first."""
        return self in (BuildNodeStatus.PASSED, BuildNodeStatus.REUSED)

    @property
    def is_terminal(self) -> bool:
        return self in (
            BuildNodeStatus.PASSED,
            BuildNodeStatus.REUSED,
            BuildNodeStatus.FAILED,
            BuildNodeStatus.CANCELLED,
        )


class AttemptStatus(StrEnum):
    """§10.3. An attempt is not SUCCEEDED until required bytes are stored and hashed."""

    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    POLLING = "POLLING"
    FETCHING = "FETCHING"
    STORED = "STORED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class AttemptMechanism(StrEnum):
    """§5.5 FR-PROV-002: attempt detail must identify the exact mechanism used, so
    the UI can distinguish retry from model fallback from cross-provider fallback."""

    PRIMARY = "PRIMARY"
    SAME_PROVIDER_RETRY = "SAME_PROVIDER_RETRY"
    SAME_PROVIDER_MODEL_FALLBACK = "SAME_PROVIDER_MODEL_FALLBACK"
    CROSS_PROVIDER_FALLBACK = "CROSS_PROVIDER_FALLBACK"
    REPAIR_RETAKE = "REPAIR_RETAKE"
    MANUAL_RETRY = "MANUAL_RETRY"


class ReleaseStatus(StrEnum):
    """§10.4"""

    DRAFT = "DRAFT"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class WorkItemStatus(StrEnum):
    """§10.5"""

    QUEUED = "QUEUED"
    LEASED = "LEASED"
    DONE = "DONE"
    RETRY_WAIT = "RETRY_WAIT"
    DEAD = "DEAD"
    CANCELLED = "CANCELLED"


class ValidationStatus(StrEnum):
    """§5.6 FR-QA-002. ERROR is never treated as PASS."""

    PASS = "PASS"  # noqa: S105 — a gate outcome, not a credential
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    ERROR = "ERROR"


class ErrorClass(StrEnum):
    """§5.5 FR-PROV-003. Retry policy keys off this, never off string matching."""

    TRANSIENT = "TRANSIENT"
    MODEL = "MODEL"
    INPUT = "INPUT"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    POLICY = "POLICY"
    STORAGE = "STORAGE"
    VALIDATION = "VALIDATION"
    INTERNAL = "INTERNAL"

    @property
    def is_retryable_same_provider(self) -> bool:
        """§13.3: never retry invalid input, auth failure, policy denial, or a
        deterministic validation failure as the same attempt.

        INTERNAL belongs here. It is where an unclassified provider failure lands
        — `classify_provider_error` maps ProviderErrorCode.UNKNOWN to it — and an
        unclassified failure is by definition not one we have shown to be
        deterministic, so §13.3 does not forbid retrying it. Excluding it meant a
        single unexplained provider hiccup skipped the cheap rung entirely, and
        on a policy with no fallback model configured that failed the node on its
        first attempt and took the whole build down with it.

        MODEL and QUOTA stay out. A model-specific fault should move to a
        different model rather than re-run the one that just failed, and quota is
        not cleared by trying again on the same account.
        """
        return self in (ErrorClass.TRANSIENT, ErrorClass.STORAGE, ErrorClass.INTERNAL)


class TriggerSource(StrEnum):
    """§15.4. The UI must not claim a B2 event triggered work that the internal
    post-storage path triggered."""

    B2_EVENT = "B2_EVENT"
    APPLICATION_COMMIT = "APPLICATION_COMMIT"
    RECONCILIATION = "RECONCILIATION"


class Role(StrEnum):
    """§5.1 FR-AUTH-003. Enforced in the service layer, never only in the UI."""

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    REVIEWER = "REVIEWER"
    VIEWER = "VIEWER"
    GUEST = "GUEST"


class PricingStatus(StrEnum):
    """§5.3 FR-IMPACT-003: unknown pricing stays unknown, never zero."""

    UNKNOWN = "UNKNOWN"
    ESTIMATE = "ESTIMATE"
    PROVIDER_REPORTED = "PROVIDER_REPORTED"


class CapabilityState(StrEnum):
    """§15.4 / §11.2. Features report their real state; a missing credential never
    silently becomes a fixture (§24.5)."""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    UNAVAILABLE_ACCOUNT_FEATURE = "UNAVAILABLE_ACCOUNT_FEATURE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class ApiErrorCode(StrEnum):
    """§11.9. The complete required set; the API maps typed domain errors onto
    these once, at the boundary (§23.1)."""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_GRAPH = "INVALID_GRAPH"
    GRAPH_CYCLE = "GRAPH_CYCLE"
    INVALID_SOURCE = "INVALID_SOURCE"
    UPLOAD_INCOMPLETE = "UPLOAD_INCOMPLETE"
    IMPACT_PLAN_STALE = "IMPACT_PLAN_STALE"
    BUILD_NOT_RUNNABLE = "BUILD_NOT_RUNNABLE"
    NODE_NOT_RETRYABLE = "NODE_NOT_RETRYABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    ASSET_VERIFICATION_FAILED = "ASSET_VERIFICATION_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    RELEASE_NOT_READY = "RELEASE_NOT_READY"
    RELEASE_VERIFICATION_FAILED = "RELEASE_VERIFICATION_FAILED"
    B2_SIGNATURE_INVALID = "B2_SIGNATURE_INVALID"
    FEATURE_NOT_CONFIGURED = "FEATURE_NOT_CONFIGURED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
