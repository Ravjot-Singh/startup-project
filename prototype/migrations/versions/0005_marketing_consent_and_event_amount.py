"""Marketing consent on customers; amount on customer events

* customers.marketing_opt_in (+ when and how it was given): separate from agreeing to payment messages.
  Existing customers start without it; nobody is assumed to want marketing messages.
* customer_events.amount: the payment that cleared a customer's dues (for the thank-you message)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as b:
        b.add_column(sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("marketing_consent_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("marketing_consent_source", sa.String(length=200), nullable=True))
    with op.batch_alter_table("customer_events") as b:
        b.add_column(sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer_events") as b:
        b.drop_column("amount")
    with op.batch_alter_table("customers") as b:
        b.drop_column("marketing_consent_source")
        b.drop_column("marketing_consent_at")
        b.drop_column("marketing_opt_in")
