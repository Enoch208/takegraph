"""Enforce global policy uniqueness when organization_id is null.

Revision ID: 2d74b1a61f4c
Revises: 03e2f8807cda
Create Date: 2026-08-02 20:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "2d74b1a61f4c"
down_revision: str | None = "03e2f8807cda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_provider_policies_scope_key_version_nnd",
        "provider_policies",
        ["organization_id", "key", "version"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "uq_validation_policies_scope_key_version_nnd",
        "validation_policies",
        ["organization_id", "key", "version"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_validation_policies_scope_key_version_nnd",
        table_name="validation_policies",
    )
    op.drop_index(
        "uq_provider_policies_scope_key_version_nnd",
        table_name="provider_policies",
    )
