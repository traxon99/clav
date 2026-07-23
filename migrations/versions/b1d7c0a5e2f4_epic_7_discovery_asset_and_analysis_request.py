"""epic 7: asset universe + analysis_request tables

Revision ID: b1d7c0a5e2f4
Revises: 14ceeb145775
Create Date: 2026-07-23 21:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d7c0a5e2f4"
down_revision: str | None = "14ceeb145775"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("tradable", sa.Boolean(), nullable=False),
        sa.Column("fractionable", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_asset_symbol"), ["symbol"], unique=True)
        batch_op.create_index(batch_op.f("ix_asset_updated_at"), ["updated_at"], unique=False)

    op.create_table(
        "analysis_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["decision_id"], ["decision.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("analysis_request", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_analysis_request_symbol"), ["symbol"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_analysis_request_requested_at"), ["requested_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_analysis_request_status"), ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("analysis_request")
    op.drop_table("asset")
