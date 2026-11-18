"""Outbox (messages) replaces reminder_logs; reminder_steps remembers which step each bill has had

* messages: every message sent or waiting to be sent (automatic reminders, one-off reminders, send-now)
* old reminder_logs rows are copied in as kind 'reminder'
* reminder_steps: (bill, step) pairs already reminded. Back-filled from the old log so the new
  catch-up logic does not resend steps that already went out before the upgrade.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""
from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _d(v):
    return date.fromisoformat(v[:10]) if isinstance(v, str) else v


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now()

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("template_name", sa.String(length=100), nullable=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("invoice_nos", sa.Text(), nullable=False),
        sa.Column("amount_due", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("message_preview", sa.Text(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("is_promise", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("provider_msg_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["rule_id"], ["reminder_rules.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    with op.batch_alter_table("messages") as b:
        for col in ("customer_id", "kind", "run_date", "scheduled_for", "provider_msg_id", "status"):
            b.create_index(b.f(f"ix_messages_{col}"), [col], unique=False)

    op.create_table(
        "reminder_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["rule_id"], ["reminder_rules.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", "rule_id", name="uq_step_once"),
    )
    with op.batch_alter_table("reminder_steps") as b:
        b.create_index(b.f("ix_reminder_steps_invoice_id"), ["invoice_id"], unique=False)
        b.create_index(b.f("ix_reminder_steps_rule_id"), ["rule_id"], unique=False)

    # ---- copy the old log
    logs = sa.table("reminder_logs", *[sa.column(c) for c in (
        "id", "customer_id", "rule_id", "run_date", "invoice_nos", "amount_due", "message_preview", "sent_at",
        "provider_msg_id", "status", "error", "attempts")])
    rules = sa.table("reminder_rules", sa.column("id"), sa.column("offset_days"), sa.column("template_name"))
    invoices = sa.table("invoices", sa.column("id"), sa.column("customer_id"), sa.column("invoice_no"), sa.column("due_date"))
    messages = sa.table("messages", *[sa.column(c) for c in (
        "id", "customer_id", "kind", "rule_id", "template_name", "run_date", "invoice_nos", "amount_due",
        "message_preview", "is_promise", "sent_at", "provider_msg_id", "status", "error", "attempts",
        "idempotency_key", "created_at")])
    steps = sa.table("reminder_steps", sa.column("invoice_id"), sa.column("rule_id"), sa.column("message_id"), sa.column("created_at"))

    all_rules = bind.execute(sa.select(rules.c.id, rules.c.offset_days, rules.c.template_name)).all()
    template_of = {rid: t for rid, _, t in all_rules}
    done: set[tuple[int, int]] = set()
    for row in bind.execute(sa.select(logs)).mappings().all():
        bind.execute(messages.insert().values(
            id=row["id"], customer_id=row["customer_id"], kind="reminder", rule_id=row["rule_id"],
            template_name=template_of.get(row["rule_id"]), run_date=row["run_date"], invoice_nos=row["invoice_nos"] or "",
            amount_due=row["amount_due"] or 0, message_preview=row["message_preview"] or "", is_promise=False,
            sent_at=row["sent_at"], provider_msg_id=row["provider_msg_id"], status=row["status"] or "queued",
            error=row["error"], attempts=row["attempts"] or 0, idempotency_key=f"legacy:{row['id']}", created_at=now))
        # every step a listed bill had already reached by that day counts as done
        nos = [n for n in (row["invoice_nos"] or "").split(",") if n]
        if not nos:
            continue
        run_day = _d(row["run_date"])
        bills = bind.execute(sa.select(invoices.c.id, invoices.c.due_date)
                             .where(invoices.c.customer_id == row["customer_id"], invoices.c.invoice_no.in_(nos))).all()
        for iid, due in bills:
            overdue = (run_day - _d(due)).days
            for rid, offset, _ in all_rules:
                if offset <= overdue and (iid, rid) not in done:
                    done.add((iid, rid))
                    bind.execute(steps.insert().values(invoice_id=iid, rule_id=rid, message_id=row["id"], created_at=now))
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SELECT setval(pg_get_serial_sequence('messages', 'id'), COALESCE((SELECT MAX(id) FROM messages), 0) + 1, false)"))

    op.drop_table("reminder_logs")


def downgrade() -> None:
    raise NotImplementedError("Restore from a backup instead: the reminder log was merged into messages.")
