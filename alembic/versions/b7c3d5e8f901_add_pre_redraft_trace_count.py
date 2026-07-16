"""add pre_redraft_trace_count

Revision ID: b7c3d5e8f901
Revises: a6024a92b969
Create Date: 2026-07-16 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — required for AutoString etc.


# revision identifiers, used by Alembic.
revision: str = "b7c3d5e8f901"
down_revision: Union[str, None] = "a6024a92b969"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ticket", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "pre_redraft_trace_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket", schema=None) as batch_op:
        batch_op.drop_column("pre_redraft_trace_count")
