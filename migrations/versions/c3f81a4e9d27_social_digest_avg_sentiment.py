"""social_digest.avg_sentiment (graded Stage-1 sentiment)

Nullable on purpose: rows written before the graded scorer existed — and rows
written while the word-list fallback is selected — have no mean intensity, and
NULL must stay distinguishable from a genuine 0.0 (neutral) reading.

Revision ID: c3f81a4e9d27
Revises: b1d7c0a5e2f4
Create Date: 2026-07-28 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f81a4e9d27"
down_revision: str | None = "b1d7c0a5e2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("social_digest", schema=None) as batch_op:
        batch_op.add_column(sa.Column("avg_sentiment", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("social_digest", schema=None) as batch_op:
        batch_op.drop_column("avg_sentiment")
