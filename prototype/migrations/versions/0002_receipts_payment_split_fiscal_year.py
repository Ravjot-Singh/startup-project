"""Receipts and payment split, bill identity by financial year, customer events

* receipts: money received; payments now split a receipt across bills (paid_on/mode/note move to the receipt)
* existing payments become one manual receipt each
* bills already marked paid by older uploads (amount_paid without payment rows) get an 'opening' receipt
* invoices: fiscal_year + busy_paid; unique (invoice_no, fiscal_year) replaces unique invoice_no
* customer_events: e.g. 'fully_paid'

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fy(d) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _drop_old_invoice_no_unique(bind) -> None:
    if bind.dialect.name == "sqlite":
        # SQLite keeps it unnamed inside CREATE TABLE; batch mode rebuilds the table, naming it so it can be dropped
        with op.batch_alter_table("invoices", recreate="always",
                                  naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as b:
            b.drop_constraint("uq_invoices_invoice_no", type_="unique")
        return
    for uc in sa.inspect(bind).get_unique_constraints("invoices"):
        if uc["column_names"] == ["invoice_no"]:
            op.drop_constraint(uc["name"], "invoices", type_="unique")


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now()

    op.create_table(
        "customer_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("customer_events") as b:
        b.create_index(b.f("ix_customer_events_customer_id"), ["customer_id"], unique=False)
        b.create_index(b.f("ix_customer_events_kind"), ["kind"], unique=False)

    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("receipts") as b:
        b.create_index(b.f("ix_receipts_customer_id"), ["customer_id"], unique=False)
        b.create_index(b.f("ix_receipts_received_on"), ["received_on"], unique=False)

    # ---- invoices: financial year + what Busy last said was paid
    with op.batch_alter_table("invoices") as b:
        b.add_column(sa.Column("fiscal_year", sa.String(length=7), nullable=True))
        b.add_column(sa.Column("busy_paid", sa.Numeric(precision=12, scale=2), nullable=True))
    invoices = sa.table("invoices", sa.column("id", sa.Integer), sa.column("invoice_date", sa.Date),
                        sa.column("fiscal_year", sa.String), sa.column("customer_id", sa.Integer),
                        sa.column("amount_paid", sa.Numeric))
    for iid, inv_date in bind.execute(sa.select(invoices.c.id, invoices.c.invoice_date)).all():
        bind.execute(invoices.update().where(invoices.c.id == iid).values(fiscal_year=_fy(inv_date)))
    _drop_old_invoice_no_unique(bind)
    with op.batch_alter_table("invoices") as b:
        b.alter_column("fiscal_year", existing_type=sa.String(length=7), nullable=False)
        b.create_index(b.f("ix_invoices_invoice_no"), ["invoice_no"], unique=False)
        b.create_unique_constraint("uq_invoice_no_fy", ["invoice_no", "fiscal_year"])

    # ---- payments become parts of receipts
    with op.batch_alter_table("payments") as b:
        b.add_column(sa.Column("receipt_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
    receipts = sa.table("receipts", sa.column("id", sa.Integer), sa.column("customer_id", sa.Integer),
                        sa.column("amount", sa.Numeric), sa.column("received_on", sa.Date), sa.column("mode", sa.String),
                        sa.column("note", sa.String), sa.column("source", sa.String), sa.column("created_at", sa.DateTime))
    payments = sa.table("payments", sa.column("id", sa.Integer), sa.column("invoice_id", sa.Integer),
                        sa.column("amount", sa.Numeric), sa.column("paid_on", sa.Date), sa.column("mode", sa.String),
                        sa.column("note", sa.String), sa.column("receipt_id", sa.Integer), sa.column("created_at", sa.DateTime))
    rows = bind.execute(
        sa.select(payments.c.id, payments.c.amount, payments.c.paid_on, payments.c.mode, payments.c.note, invoices.c.customer_id)
        .select_from(payments.join(invoices, payments.c.invoice_id == invoices.c.id))
    ).all()
    for pid, amount, paid_on, mode, note, customer_id in rows:
        rid = bind.execute(receipts.insert().values(
            customer_id=customer_id, amount=amount, received_on=paid_on, mode=mode or "cash", note=note,
            source="manual", created_at=now).returning(receipts.c.id)).scalar_one()
        bind.execute(payments.update().where(payments.c.id == pid).values(receipt_id=rid, created_at=now))
    with op.batch_alter_table("payments") as b:
        b.drop_column("paid_on")
        b.drop_column("note")
        b.drop_column("mode")
        b.alter_column("receipt_id", existing_type=sa.Integer(), nullable=False)
        b.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        b.create_index(b.f("ix_payments_receipt_id"), ["receipt_id"], unique=False)
        b.create_foreign_key("fk_payments_receipt_id", "receipts", ["receipt_id"], ["id"])

    # ---- paid amounts set by older uploads had no payment rows: record them as opening history
    payments2 = sa.table("payments", sa.column("invoice_id", sa.Integer), sa.column("amount", sa.Numeric),
                         sa.column("receipt_id", sa.Integer), sa.column("created_at", sa.DateTime))
    paid_sum = (sa.select(payments2.c.invoice_id, sa.func.sum(payments2.c.amount).label("s"))
                .group_by(payments2.c.invoice_id).subquery())
    gaps = bind.execute(
        sa.select(invoices.c.id, invoices.c.customer_id, invoices.c.amount_paid, sa.func.coalesce(paid_sum.c.s, 0))
        .select_from(invoices.outerjoin(paid_sum, paid_sum.c.invoice_id == invoices.c.id))
    ).all()
    for iid, customer_id, amount_paid, allocated in gaps:
        gap = Decimal(amount_paid or 0) - Decimal(allocated or 0)
        if gap > 0:
            rid = bind.execute(receipts.insert().values(
                customer_id=customer_id, amount=gap, received_on=date.today(), mode="busy", note=None,
                source="opening", created_at=now).returning(receipts.c.id)).scalar_one()
            bind.execute(payments2.insert().values(invoice_id=iid, amount=gap, receipt_id=rid, created_at=now))


def downgrade() -> None:
    raise NotImplementedError("Restore from a backup instead: payments were reshaped into receipts.")
