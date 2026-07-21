"""add diagnostic_event

Revision ID: c8d4e6f9a012
Revises: b7c3d5e8f901
Create Date: 2026-07-21 00:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — required for AutoString etc.


# revision identifiers, used by Alembic.
revision: str = "c8d4e6f9a012"
down_revision: Union[str, None] = "b7c3d5e8f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "diagnosticevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sqlmodel.AutoString(), nullable=False),
        sa.Column("repo_id", sqlmodel.AutoString(), nullable=False),
        sa.Column("category", sqlmodel.AutoString(), nullable=False),
        sa.Column("sub_category", sqlmodel.AutoString(), nullable=False),
        sa.Column("reason", sqlmodel.AutoString(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(datetime('now'))"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["ticket.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("diagnosticevent", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_diagnosticevent_category"),
            ["category"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_diagnosticevent_repo_id"),
            ["repo_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_diagnosticevent_sub_category"),
            ["sub_category"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_diagnosticevent_ticket_id"),
            ["ticket_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("diagnosticevent")
