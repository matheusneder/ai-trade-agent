"""clock_offset_ms as bigint

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 18:00:00.000000

``telemetry_snapshots.clock_offset_ms`` was int4, which only holds offsets up to about
24.8 days in milliseconds; a larger offset made the insert fail and took the risk check
(``check_risk``) down with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "telemetry_snapshots",
        "clock_offset_ms",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "telemetry_snapshots",
        "clock_offset_ms",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using=(  # offsets that do not fit int4 are dropped, not the downgrade
            "CASE WHEN clock_offset_ms BETWEEN -2147483648 AND 2147483647"
            " THEN clock_offset_ms::integer END"
        ),
    )
