from datetime import date
from decimal import Decimal

from app.models import Customer, InboundMessage, Message, ReminderRule
from app.routes.webhooks import handle_payload


def _payload(statuses=None, messages=None):
    return {"entry": [{"changes": [{"value": {"statuses": statuses or [], "messages": messages or []}}]}]}


def test_status_updates_and_never_go_backwards(db):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.flush()
    rule = db.query(ReminderRule).first()
    log = Message(customer_id=c.id, rule_id=rule.id, run_date=date(2026, 11, 10), invoice_nos="X",
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



import pytest  # noqa: E402


@pytest.mark.parametrize("text,opted_in", [("STOP", False), ("Stop.", False), (" band karo ", False),
                                           ("please stop sending", True), ("PAID", True)])
def test_stop_reply_switches_messages_off(db, text, opted_in):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.commit()
    handle_payload(db, _payload(messages=[{"from": "919800000001", "type": "text", "text": {"body": text}}]))
    assert c.whatsapp_opt_in is opted_in
    assert db.query(InboundMessage).one().handled is False  # the owner still sees the reply


def test_start_reply_switches_messages_back_on(db):
    c = Customer(name="A", phone="919800000001", whatsapp_opt_in=False)
    db.add(c)
    db.commit()
    handle_payload(db, _payload(messages=[{"from": "919800000001", "type": "text", "text": {"body": "START"}}]))
    assert c.whatsapp_opt_in is True


def test_failed_status_marks_message_failed(db):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.flush()
    m = Message(customer_id=c.id, run_date=date(2026, 11, 10), provider_msg_id="wamid.9", status="sent")
    db.add(m)
    db.commit()
    handle_payload(db, _payload(statuses=[{"id": "wamid.9", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}]))
    assert m.status == "failed" and "131047" in m.error
