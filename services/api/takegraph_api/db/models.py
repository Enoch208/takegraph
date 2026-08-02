"""Persistence models (PRD §8.2).

Scope note: this covers the tables the demo path needs end to end — tenancy,
projects and revisions, the compiled graph, change sets and impact plans, builds
and attempts, assets, validations, releases, the durable work queue, and the
event/audit log. Tables deferred until their feature lands are named in the
module docstring of the migration.

Two invariants worth stating up front because they shape almost every table here:

- Evidence is never deleted through the product. Attempt, asset, audit, manifest,
  validation and release rows use ON DELETE RESTRICT (§8.1). Only project archive
  is soft state.
- A selected output must be durable and validated before it satisfies a
  dependency (§5.4 FR-BUILD-007). `build_nodes.selected_attempt_id` therefore
  points at an attempt, and an attempt is only SUCCEEDED once bytes are stored
  and hashed (§10.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from takegraph_api.db.base import (
    Base,
    CreatedAt,
    Json,
    Sequence,
    Sha256,
    UuidPk,
    Version,
)

# --------------------------------------------------------------------------- #
# Tenancy and identity (§5.1)
# --------------------------------------------------------------------------- #


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UuidPk]
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[CreatedAt]


class User(Base):
    __tablename__ = "users"

    id: Mapped[UuidPk]
    external_subject: Mapped[str] = mapped_column(String(255), unique=True)
    """Subject claim from the identity provider. Never an email — §21.1 forbids
    using email as the primary correlation key."""
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[CreatedAt]


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Projects, revisions and sources (§5.2)
# --------------------------------------------------------------------------- #


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    # Pointers are nullable and set after the referenced rows exist; the FK is
    # deferred via use_alter to avoid a circular create order.
    active_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    active_release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    """§8.3.11: fault rules are rejected unless the project is explicitly demo or
    test scoped and ALLOW_FAILURE_INJECTION is on."""
    version: Mapped[Version]
    created_at: Mapped[CreatedAt]


class ProjectRevision(Base):
    """Immutable user specification. §8.3.3: a revision never changes after creation."""

    __tablename__ = "project_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "revision_no"),
        UniqueConstraint("project_id", "canonical_hash"),
    )

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    revision_no: Mapped[int] = mapped_column(Integer)
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    spec_json: Mapped[Json]
    canonical_hash: Mapped[Sha256]
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[CreatedAt]


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("project_id", "stable_key"),)

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    stable_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[CreatedAt]


class SourceVersion(Base):
    """§5.2 FR-PROJ-005: replacing a source creates a new version and never
    overwrites an earlier object."""

    __tablename__ = "source_versions"

    id: Mapped[UuidPk]
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_revisions.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    normalized_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[Sha256]
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[CreatedAt]


class UploadIntent(Base):
    """Short-lived quarantine record binding a presigned key to its expectations."""

    __tablename__ = "upload_intents"
    __table_args__ = (
        UniqueConstraint("object_key"),
        Index("ix_upload_intents_status_expires", "status", "expires_at"),
    )

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    source_stable_key: Mapped[str] = mapped_column(String(128))
    original_file_name: Mapped[str] = mapped_column(String(200))
    expected_size_bytes: Mapped[int] = mapped_column(Integer)
    declared_mime_type: Mapped[str] = mapped_column(String(128))
    client_sha256: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="INITIATED")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    completed_source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_versions.id", ondelete="RESTRICT")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[CreatedAt]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Compiled graph (§12.1)
# --------------------------------------------------------------------------- #


class GraphTemplateRow(Base):
    __tablename__ = "graph_templates"
    __table_args__ = (UniqueConstraint("key", "version"),)

    id: Mapped[UuidPk]
    key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(8), default="1")
    definition_json: Mapped[Json]
    created_at: Mapped[CreatedAt]


class GraphRevision(Base):
    """§8.3.2: a graph revision is immutable after compilation."""

    __tablename__ = "graph_revisions"

    id: Mapped[UuidPk]
    project_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project_revisions.id", ondelete="RESTRICT"), unique=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_templates.id", ondelete="RESTRICT")
    )
    canonical_hash: Mapped[Sha256]
    compiler_version: Mapped[str] = mapped_column(String(16))
    compiled_at: Mapped[CreatedAt]


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    __table_args__ = (UniqueConstraint("graph_revision_id", "stable_key"),)

    id: Mapped[UuidPk]
    graph_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_revisions.id", ondelete="RESTRICT")
    )
    stable_key: Mapped[str] = mapped_column(String(128))
    node_type: Mapped[str] = mapped_column(String(32))
    spec_json: Mapped[Json]
    spec_hash: Mapped[Sha256]
    provider_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    validation_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(String(200))


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("graph_revision_id", "to_node_id", "input_slot", "ordinal"),
        # §8.3.1 partially: a self-edge is rejected in SQL as well as in the
        # compiler, so no code path can introduce one.
        CheckConstraint("from_node_id <> to_node_id", name="no_self_edge"),
    )

    id: Mapped[UuidPk]
    graph_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_revisions.id", ondelete="RESTRICT")
    )
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_nodes.id", ondelete="RESTRICT")
    )
    to_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("graph_nodes.id", ondelete="RESTRICT"))
    input_slot: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    asset_role: Mapped[str] = mapped_column(String(32), default="primary")


class ProviderPolicy(Base):
    __tablename__ = "provider_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", "version"),
        Index(
            "uq_provider_policies_scope_key_version_nnd",
            "organization_id",
            "key",
            "version",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[Json]
    canonical_hash: Mapped[Sha256]
    created_at: Mapped[CreatedAt]


class ValidationPolicy(Base):
    __tablename__ = "validation_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", "version"),
        Index(
            "uq_validation_policies_scope_key_version_nnd",
            "organization_id",
            "key",
            "version",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[Json]
    canonical_hash: Mapped[Sha256]
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Change sets and impact plans (§5.3)
# --------------------------------------------------------------------------- #


class ChangeSet(Base):
    """§5.3 FR-IMPACT-001: drafts are side-effect free. Persisting one performs no
    revision bump, no provider call and no B2 write."""

    __tablename__ = "change_sets"

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    base_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project_revisions.id", ondelete="RESTRICT")
    )
    patch_json: Mapped[Json]
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]


class ImpactPlanRow(Base):
    """Immutable preview evidence. `plan_hash` is unique so a commit can bind to
    exactly one plan (§11.5)."""

    __tablename__ = "impact_plans"

    id: Mapped[UuidPk]
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="RESTRICT")
    )
    graph_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_revisions.id", ondelete="RESTRICT")
    )
    plan_json: Mapped[Json]
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Builds, nodes and attempts (§5.4)
# --------------------------------------------------------------------------- #


class Build(Base):
    __tablename__ = "builds"
    __table_args__ = (Index("ix_builds_project_created", "project_id", "created_at"),)

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    project_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project_revisions.id", ondelete="RESTRICT")
    )
    graph_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_revisions.id", ondelete="RESTRICT")
    )
    impact_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_build_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """§12.7: resume creates a new build linked to the failed one. It never
    mutates history."""
    status: Mapped[str] = mapped_column(String(24), default="PLANNED")
    total_nodes: Mapped[int] = mapped_column(Integer, default=0)
    reused_nodes: Mapped[int] = mapped_column(Integer, default=0)
    rebuilt_nodes: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    provider_reported_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    """§14.7 keeps provider-reported cost separate from our estimate; the UI
    labels which is which."""
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[Version]
    created_at: Mapped[CreatedAt]


class BuildNode(Base):
    __tablename__ = "build_nodes"
    __table_args__ = (
        UniqueConstraint("build_id", "graph_node_id"),
        Index("ix_build_nodes_build_status", "build_id", "status"),
        # §8.4: reuse lookup by fingerprint. Tenant scoping happens through the
        # join to builds -> projects, so this index stays narrow.
        Index("ix_build_nodes_fingerprint_status", "fingerprint", "status"),
    )

    id: Mapped[UuidPk]
    build_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("builds.id", ondelete="RESTRICT"))
    graph_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_nodes.id", ondelete="RESTRICT")
    )
    stable_key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[Sha256]
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    resolution: Mapped[str | None] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(48))
    reason: Mapped[str | None] = mapped_column(Text)
    selected_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    selected_asset_set_hash: Mapped[str | None] = mapped_column(String(64))
    reuse_proof_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """§12.3 requires the proof to be persisted, not merely computed."""
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[Version]
    created_at: Mapped[CreatedAt]


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("build_node_id", "attempt_no"),
        # §5.4 FR-BUILD-004: one billable submission per idempotency key. This
        # unique index is the enforcement, not a convention.
        UniqueConstraint("idempotency_key"),
        Index("ix_attempts_node_no", "build_node_id", "attempt_no"),
        Index("ix_attempts_genblaze_run", "genblaze_run_id"),
    )

    id: Mapped[UuidPk]
    build_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("build_nodes.id", ondelete="RESTRICT")
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """Lineage for retakes and cross-provider fallbacks (§14.3)."""
    mechanism: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    genblaze_run_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    error_class: Mapped[str | None] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    is_injected_fault: Mapped[bool] = mapped_column(Boolean, default=False)
    """§4.4: an injected failure is labelled TEST FAULT in the UI. The flag lives
    here so the label comes from evidence rather than a UI guess."""
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    provider_reported_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Assets and validation (§5.7, §5.6)
# --------------------------------------------------------------------------- #


class Asset(Base):
    """Canonical durable bytes. §5.7 FR-ASSET-002 deduplicates by SHA-256 within a
    tenant, which is what makes cross-build reuse cheap."""

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("organization_id", "sha256"),)

    id: Mapped[UuidPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    sha256: Mapped[Sha256]
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(128))
    media_kind: Mapped[str] = mapped_column(String(24))
    b2_bucket: Mapped[str] = mapped_column(String(128))
    b2_key: Mapped[str] = mapped_column(String(1024))
    storage_version_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    derived_from_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """§5.7 FR-ASSET-005: thumbnails and proxies reference their source rather
    than replacing it."""
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """§8.3.6: not durable until a B2 HEAD confirmed expected size and metadata."""
    created_at: Mapped[CreatedAt]


class AttemptAsset(Base):
    """Every output an attempt produced, including rejected ones (§5.7 FR-ASSET-003)."""

    __tablename__ = "attempt_assets"
    __table_args__ = (UniqueConstraint("attempt_id", "role", "ordinal"),)

    id: Mapped[UuidPk]
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    role: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)


class Validation(Base):
    __tablename__ = "validations"
    __table_args__ = (
        Index("ix_validations_node_gate_created", "build_node_id", "gate_key", "created_at"),
    )

    id: Mapped[UuidPk]
    build_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("build_nodes.id", ondelete="RESTRICT")
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    gate_key: Mapped[str] = mapped_column(String(64))
    gate_version: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    score: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[CreatedAt]


class Approval(Base):
    """§5.6 FR-QA-005: a manual decision records actor and reason, immutably."""

    __tablename__ = "approvals"

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Releases (§5.8)
# --------------------------------------------------------------------------- #


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("project_id", "version_label"),)

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    build_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("builds.id", ondelete="RESTRICT"))
    version_label: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")
    manifest_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    verification_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_mode: Mapped[str | None] = mapped_column(String(24))
    """§15.5: read back from B2 after publication, or explicitly NOT_CONFIGURED.
    Never assumed."""
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]


class ReleaseAsset(Base):
    __tablename__ = "release_assets"
    __table_args__ = (UniqueConstraint("release_id", "logical_path"),)

    id: Mapped[UuidPk]
    release_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("releases.id", ondelete="RESTRICT"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    logical_path: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32))


# --------------------------------------------------------------------------- #
# Durable work queue (§10.5, §13.1)
# --------------------------------------------------------------------------- #


class WorkItem(Base):
    """Durable jobs live in PostgreSQL, not Redis (§6.1), so a Redis outage cannot
    lose queued work.

    §8.3.10: an item is claimable only when `available_at <= now()` and its lease
    is absent or expired. The partial index below is what makes that claim query
    cheap under load.
    """

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index(
            "ix_work_items_claimable",
            text("priority desc"),
            "available_at",
            # Column order mirrors the claim query's ORDER BY (priority desc,
            # available_at) so the index serves the sort as well as the filter.
            #
            # LEASED is in the predicate because an expired lease is claimable
            # (§8.3.10). The claim query must therefore filter on exactly this
            # status set — an OR-shaped predicate defeats Postgres's ability to
            # prove the query implies the index, and it falls back to a seq scan.
            # DONE/DEAD/CANCELLED, which dominate over time, stay out.
            postgresql_where=text("status in ('QUEUED', 'RETRY_WAIT', 'LEASED')"),
        ),
    )

    id: Mapped[UuidPk]
    kind: Mapped[str] = mapped_column(String(48))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    build_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[CreatedAt]
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# Events and audit (§5.9)
# --------------------------------------------------------------------------- #


class DomainEvent(Base):
    """Authoritative event stream and outbox. §6.3: PostgreSQL events are the
    truth; Redis is a delivery accelerator that can be rebuilt from here."""

    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("event_id"),
        Index("ix_domain_events_build_seq", "build_id", "sequence"),
        Index("ix_domain_events_project_seq", "project_id", "sequence"),
        Index(
            "ix_domain_events_unpublished",
            "sequence",
            postgresql_where=text("realtime_published_at is null"),
        ),
    )

    sequence: Mapped[Sequence]
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    build_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Json]
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[CreatedAt]
    realtime_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttemptEvent(Base):
    """Normalised provider events. §13.5: raw SDK objects are never pickled or
    used as durable wire data."""

    __tablename__ = "attempt_events"
    __table_args__ = (Index("ix_attempt_events_attempt_seq", "attempt_id", "sequence"),)

    sequence: Mapped[Sequence]
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="RESTRICT"))
    provider_event_type: Mapped[str] = mapped_column(String(64))
    provider_event_json: Mapped[Json]
    received_at: Mapped[CreatedAt]
    provider_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. §19.8: written in the same transaction as the mutation it
    describes, and never containing secret values."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_project_time", "project_id", "occurred_at"),)

    id: Mapped[Sequence]
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_kind: Mapped[str] = mapped_column(String(16))
    effective_role: Mapped[str | None] = mapped_column(String(16))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before_ref: Mapped[str | None] = mapped_column(String(128))
    after_ref: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# B2 event ingestion (§11.8) and fault injection (§8.3.11)
# --------------------------------------------------------------------------- #


class B2WebhookMessage(Base):
    """Raw-message dedupe and audit without retaining the secret. §11.8 requires
    verification over the raw body before JSON parsing."""

    __tablename__ = "b2_webhook_messages"
    __table_args__ = (UniqueConstraint("body_sha256"),)

    id: Mapped[UuidPk]
    signature_version: Mapped[str] = mapped_column(String(8))
    body_sha256: Mapped[Sha256]
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    received_at: Mapped[CreatedAt]
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class B2ObjectEvent(Base):
    __tablename__ = "b2_object_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("ix_b2_object_events_status_time", "status", "event_timestamp"),
    )

    id: Mapped[UuidPk]
    dedupe_key: Mapped[str] = mapped_column(String(256))
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("b2_webhook_messages.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(1024))
    object_version_id: Mapped[str | None] = mapped_column(String(128))
    object_size: Mapped[int | None] = mapped_column(Integer)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="RECEIVED")
    trigger_source: Mapped[str] = mapped_column(String(24), default="B2_EVENT")
    """§15.4: the UI must not claim a B2 event triggered work the internal
    post-storage path triggered."""


class FaultRule(Base):
    """Explicit demo and test fault injection only. §8.3.11: rejected unless
    ALLOW_FAILURE_INJECTION=true and the project is demo/test scoped."""

    __tablename__ = "fault_rules"

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    node_stable_key: Mapped[str] = mapped_column(String(128))
    fault_type: Mapped[str] = mapped_column(String(32))
    remaining_uses: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[CreatedAt]
