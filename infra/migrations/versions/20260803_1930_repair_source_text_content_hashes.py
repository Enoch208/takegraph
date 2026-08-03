"""Repair source-text content hashes so reuse can match again.

The worker recorded a source node's output as the JCS hash of
``{"brief_text": ...}``; the impact engine expected the whitespace-normalised
text hash. Two defensible definitions that never agree, so a resolved source node
could never satisfy the reuse proof: every one reported CACHE_ASSET_MISSING,
which invalidated its dependents and theirs, and a one-word legal-line change
rebuilt sixteen of eighteen nodes instead of four.

The code now shares one definition. This repairs the rows the old one wrote.

Nothing here invents evidence. `selected_asset_set_hash` on a SOURCE_TEXT node is
a pure function of the brief its build ran against, and that text has not
changed — the stored digest is simply recomputed with the corrected function.
Rows already holding the correct value are left alone, so the migration is
idempotent, and nodes whose revision no longer resolves are skipped rather than
guessed at.

Revision ID: 7c1a4b9d2e58
Revises: 2d74b1a61f4c
"""

from __future__ import annotations

import hashlib
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "7c1a4b9d2e58"
down_revision = "2d74b1a61f4c"
branch_labels = None
depends_on = None

#: Kept local on purpose. A migration must keep doing what it did on the day it
#: was written, so it does not import a definition that later releases may change.
DEFAULT_BRIEF_TEXT_PARAM = "brief_text"


def _normalised_hash(value: str) -> str:
    return hashlib.sha256(" ".join(unicodedata.normalize("NFC", value).split()).encode()).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            select bn.id,
                   pr.spec_json -> 'parameters' ->> :param as brief,
                   bn.selected_asset_set_hash as stored
            from build_nodes bn
            join graph_nodes gn on gn.id = bn.graph_node_id
            join builds b on b.id = bn.build_id
            join project_revisions pr on pr.id = b.project_revision_id
            where gn.node_type = 'SOURCE_TEXT'
              and bn.selected_asset_set_hash is not null
            """
        ),
        {"param": DEFAULT_BRIEF_TEXT_PARAM},
    ).fetchall()

    repaired = 0
    for node_id, brief, stored in rows:
        if not isinstance(brief, str):
            # No resolvable text: leave the row exactly as it is rather than
            # substituting a default and calling it evidence.
            continue
        expected = _normalised_hash(brief)
        if expected == stored:
            continue
        bind.execute(
            sa.text(
                "update build_nodes set selected_asset_set_hash = :expected where id = :id"
            ),
            {"expected": expected, "id": node_id},
        )
        repaired += 1
    # Migrations report to the console by convention; alembic captures it.
    print(  # noqa: T201
        f"repaired {repaired} source-text content hashes of {len(rows)} examined"
    )


def downgrade() -> None:
    """Not reversible.

    The previous values were produced by a defective function and restoring them
    would reintroduce the cascade this migration exists to remove. The correct
    values are recomputable from the briefs at any time, so nothing is lost.
    """
