"""A/B tape: decisions and quotes for the paper replay (D-048)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tape_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile", sa.String(length=48), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("research", sa.Text(), nullable=False),
        sa.Column("exposure_multiplier", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tape_decisions")),
    )
    op.create_index(op.f("ix_tape_decisions_at"), "tape_decisions", ["at"], unique=False)
    op.create_table(
        "tape_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("bid", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("ask", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tape_quotes")),
    )
    op.create_index(op.f("ix_tape_quotes_at"), "tape_quotes", ["at"], unique=False)
    op.create_index("ix_tape_quotes_symbol_at", "tape_quotes", ["symbol", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tape_quotes_symbol_at", table_name="tape_quotes")
    op.drop_index(op.f("ix_tape_quotes_at"), table_name="tape_quotes")
    op.drop_table("tape_quotes")
    op.drop_index(op.f("ix_tape_decisions_at"), table_name="tape_decisions")
    op.drop_table("tape_decisions")
