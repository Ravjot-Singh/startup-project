from datetime import date
from decimal import Decimal

from app.models import Customer, InboundMessage, ReminderLog, ReminderRule
from app.routes.webhooks import handle_payload


def _payload(statuses=None, messages=None):
    return {"entry": [{"changes": [{"value": {"statuses": statuses or [], "messages": messages or []}}]}]}


def test_status_updates_and_never_go_backwards(db):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.flush()
    rule = db.query(ReminderRule).first()
    log = ReminderLog(customer_id=c.id, rule_id=rule.id, run_date=date(2026, 11, 10), invoice_nos="X",
                      amount_due=Decimal(1), message_preview="hi", provider_msg_id="wamid.1", status="sent")
    db.add(log)
    db.commit()

    handle_payload(db, _payload(statuses=[{"id": "wamid.1", "status": "read"}]))
    handle_payload(db, _payload(statuses=[{"id": "wamid.1", "status": "delivered"}]))
    assert log.status == "read"


def test_inbound_reply_linked_to_customer(db):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.commit()
    handle_payload(db, _payload(messages=[{"from": "919800000001", "type": "text", "text": {"body": "PAID"}}]))
    m = db.query(InboundMessage).one()
    assert m.customer_id == c.id and m.text == "PAID"
