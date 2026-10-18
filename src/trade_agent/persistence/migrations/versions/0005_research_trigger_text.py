"""research_reports.trigger as text

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 12:00:00.000000

The decision engine names a research cycle ``ciclo:`` plus the sorted names of the profiles
that brought candidates. With three profiles (``alt_atr``, ``momentum_alpha``, ``swing_trend``)
that is 40 characters, over the old varchar(32): the insert failed after the LLM call was
paid for and the cycle's reading was lost. The name grows with every profile, so the column
has no length limit now.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "research_reports",
        "trigger",
        existing_type=sa.String(length=32),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "research_reports",
        "trigger",
        existing_type=sa.Text(),
        type_=sa.String(length=32),
        existing_nullable=False,
        postgresql_using="left(trigger, 32)",  # longer names are cut, not the downgrade
    )
