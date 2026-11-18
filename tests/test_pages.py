"""Smoke tests for every dashboard page and form action, through the real HTTP routes."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app import clock
from app.models import Customer, InboundMessage, Invoice, Payment, ReminderRule
from tests.conftest import csrf_from

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample_data" / "outstanding_sample.csv"

PAGES = ["/", "/customers", "/customers?q=sharma", "/import", "/reminders", "/call-list", "/call-list?days=0", "/rules"]


@pytest.fixture
def data(db):
    today = clock.today()
    c = Customer(name="Sharma Store", shop_name="Sharma Store", phone="919800000001", area="Model Town")
    db.add(c)
    db.flush()
    inv = Invoice(customer=c, invoice_no="INV-1", invoice_date=today - timedelta(days=40),
                  due_date=today - timedelta(days=20), amount=Decimal("5000"), amount_paid=Decimal("0"))
    inv.refresh_status()
    db.add_all([inv, InboundMessage(customer=c, phone=c.phone, text="Will pay Friday")])
    db.commit()
    return {"customer": c, "invoice": inv}


def test_pages_redirect_to_login_when_logged_out(client):
    for url in PAGES:
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_login_page_and_wrong_password(client):
    page = client.get("/login")
    assert page.status_code == 200
    r = client.post("/login", data={"username": "owner", "password": "nope", "csrf_token": csrf_from(page.text)})
    assert r.status_code == 401 and "Wrong username or password" in r.text


@pytest.mark.parametrize("url", PAGES)
def test_every_page_renders_empty(logged_in, url):
    assert logged_in.get(url).status_code == 200


@pytest.mark.parametrize("url", PAGES)
def test_every_page_renders_with_data(logged_in, data, url):
    assert logged_in.get(url).status_code == 200


def test_customer_detail_and_missing_customer(logged_in, data):
    r = logged_in.get(f"/customers/{data['customer'].id}")
    assert r.status_code == 200 and "INV-1" in r.text
    assert logged_in.get("/customers/99999").status_code == 404


def test_post_without_csrf_token_is_rejected(logged_in, data):
    url = f"/invoices/{data['invoice'].id}/payment"
    assert logged_in.post(url, data={"amount": "100"}).status_code == 403
    assert logged_in.post(url, data={"amount": "100", "csrf_token": "wrong"}).status_code == 403


def test_login_without_csrf_token_is_rejected(client):
    client.get("/login")
    assert client.post("/login", data={"username": "owner", "password": "test-password-123"}).status_code == 403


def test_record_payment(logged_in, db, data):
    inv = data["invoice"]
    r = logged_in.post(f"/invoices/{inv.id}/payment", data={"amount": "2,000", "mode": "upi", "csrf_token": logged_in.csrf})
    assert r.status_code == 303
    db.refresh(inv)
    assert inv.amount_paid == Decimal("2000") and inv.status == "partial"
    assert db.query(Payment).count() == 1


def test_dispute_toggle(logged_in, db, data):
    inv = data["invoice"]
    logged_in.post(f"/invoices/{inv.id}/dispute", data={"csrf_token": logged_in.csrf})
    db.refresh(inv)
    assert inv.status == "disputed"
    logged_in.post(f"/invoices/{inv.id}/dispute", data={"csrf_token": logged_in.csrf})
    db.refresh(inv)
    assert inv.status == "open"


def test_edit_customer(logged_in, db, data):
    c = data["customer"]
    r = logged_in.post(f"/customers/{c.id}/edit", data={
        "phone": "98111 22233", "credit_days": "30", "area": "Civil Lines", "salesman": "",
        "whatsapp_opt_in": "true", "csrf_token": logged_in.csrf,
    })
    assert r.status_code == 303
    db.refresh(c)
    assert (c.phone, c.credit_days, c.area, c.reminders_paused) == ("919811122233", 30, "Civil Lines", False)


def test_toggle_rule_and_mark_reply_handled(logged_in, db, data):
    rule = db.query(ReminderRule).first()
    logged_in.post(f"/rules/{rule.id}/toggle", data={"csrf_token": logged_in.csrf})
    db.refresh(rule)
    assert rule.is_active is False
    reply = db.query(InboundMessage).first()
    logged_in.post(f"/replies/{reply.id}/handled", data={"csrf_token": logged_in.csrf})
    db.refresh(reply)
    assert reply.handled is True


def test_run_reminders_now(logged_in, data):
    assert logged_in.post("/reminders/run", data={"csrf_token": logged_in.csrf}).status_code == 303
    assert logged_in.get("/").status_code == 200


def test_import_upload(logged_in, db):
    with SAMPLE_CSV.open("rb") as f:
        r = logged_in.post("/import", data={"csrf_token": logged_in.csrf},
                           files={"file": ("outstanding_sample.csv", f, "text/csv")})
    assert r.status_code == 200
    assert db.query(Invoice).count() > 0


def test_import_rejects_wrong_file_type(logged_in):
    r = logged_in.post("/import", data={"csrf_token": logged_in.csrf}, files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 200 and "Please upload an Excel" in r.text


def test_logout(logged_in):
    assert logged_in.post("/logout", data={"csrf_token": logged_in.csrf}).status_code == 303
    assert logged_in.get("/").status_code == 303
