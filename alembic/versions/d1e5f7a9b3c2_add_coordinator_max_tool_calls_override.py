"""add coordinator_max_tool_calls_override

Revision ID: d1e5f7a9b3c2
Revises: c0d4e6f8a2b1
Create Date: 2026-10-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e5f7a9b3c2"
down_revision: str | None = "c0d4e6f8a2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ticket", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "coordinator_max_tool_calls_override",
                sa.Integer(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket", schema=None) as batch_op:
        batch_op.drop_column("coordinator_max_tool_calls_override")
