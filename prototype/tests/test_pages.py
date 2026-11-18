"""Smoke tests for every dashboard page and form action, through the real HTTP routes."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app import clock
from app.models import Customer, InboundMessage, Invoice, Payment, Receipt, ReminderRule
from tests.conftest import csrf_from

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample_data" / "outstanding_sample.csv"

PAGES = ["/", "/customers", "/customers?q=sharma", "/customers?missing=phone", "/import", "/messages", "/call-list",
         "/call-list?days=0", "/settings", "/rules"]


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
    url = f"/customers/{data['customer'].id}/payments"
    assert logged_in.post(url, data={"amount": "100"}).status_code == 403
    assert logged_in.post(url, data={"amount": "100", "csrf_token": "wrong"}).status_code == 403


def test_login_without_csrf_token_is_rejected(client):
    client.get("/login")
    assert client.post("/login", data={"username": "owner", "password": "test-password-123"}).status_code == 403


def test_record_payment(logged_in, db, data):
    inv, c = data["invoice"], data["customer"]
    r = logged_in.post(f"/customers/{c.id}/payments", data={
        "amount": "2,000", "mode": "upi", "received_on": clock.today().isoformat(), f"alloc_{inv.id}": "2000",
        "csrf_token": logged_in.csrf})
    assert r.status_code == 303
    db.refresh(inv)
    assert inv.amount_paid == Decimal("2000") and inv.status == "partial"
    assert db.query(Payment).count() == 1 and db.query(Receipt).one().mode == "upi"


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


# ---------- Phase 1: new layout, Today page, settings ----------

def test_old_reminders_url_moves_to_messages(logged_in):
    r = logged_in.get("/reminders")
    assert r.status_code == 301 and r.headers["location"] == "/messages"


def test_today_empty_state_welcomes_and_points_to_upload(logged_in):
    html = logged_in.get("/").text
    assert "Let's bring in your bills" in html and 'href="/import"' in html
    assert "Needs you" not in html


def test_today_shows_important_things_first(logged_in, data):
    html = logged_in.get("/").text
    order = [html.index(s) for s in ("Collected, last 7 days", "Due to you", "Overdue", "Needs you", "Going out today", "More detail")]
    assert order == sorted(order)
    assert "₹5,000" in html  # Indian formatting, no paise for whole rupees
    assert "Will pay Friday" in html  # unhandled reply surfaces in Needs you


def test_today_all_clear_when_nothing_needs_attention(logged_in, db):
    from datetime import timedelta

    c = Customer(name="Fine", shop_name="Fine", phone="919800000005")
    db.add(c)
    db.flush()
    db.add(Invoice(customer=c, invoice_no="F1", invoice_date=clock.today(), due_date=clock.today() + timedelta(days=10),
                   amount=Decimal(100), amount_paid=Decimal(0), status="open"))
    db.commit()
    assert "All clear" in logged_in.get("/").text


def test_nav_marks_current_section(logged_in, data):
    for url, label in [("/", "Today"), ("/customers", "Customers"), ("/call-list", "Customers"),
                       ("/messages", "Messages"), ("/settings", "Settings"), ("/rules", "Settings"), ("/import", "Settings")]:
        html = logged_in.get(url).text
        current = html.split('aria-current="page"', 1)[1][:400]
        assert f"<span>{label}</span>" in current, url


def test_mark_reply_done_from_today_returns_to_today(logged_in, db, data):
    reply = db.query(InboundMessage).first()
    r = logged_in.post(f"/replies/{reply.id}/handled", data={"next": "/", "csrf_token": logged_in.csrf})
    assert r.headers["location"] == "/"


def test_reply_redirect_cannot_leave_the_app(logged_in, db, data):
    reply = db.query(InboundMessage).first()
    for evil in ("//evil.example", "https://evil.example"):
        r = logged_in.post(f"/replies/{reply.id}/handled", data={"next": evil, "csrf_token": logged_in.csrf})
        assert r.headers["location"] == "/messages"


def test_pause_and_resume_sending(logged_in, db):
    from app import settings

    logged_in.post("/settings/pause", data={"csrf_token": logged_in.csrf})
    assert settings.get_bool(db, "sending_paused") is True
    page = logged_in.get("/settings").text
    assert "Nothing goes out until you resume" in page and "Resume automatic sending" in page
    logged_in.post("/settings/pause", data={"csrf_token": logged_in.csrf})
    assert settings.get_bool(db, "sending_paused") is False


def test_flash_message_shows_once(logged_in, data):
    inv = data["invoice"]
    page = logged_in.post(f"/customers/{data['customer'].id}/payments", data={
        "amount": "1000", f"alloc_{inv.id}": "1000", "csrf_token": logged_in.csrf}, follow_redirects=True)
    assert "₹1,000 received." in page.text
    assert "₹1,000 received." not in logged_in.get(f"/customers/{data['customer'].id}").text


def test_customers_missing_phone_filter(logged_in, db, data):
    from datetime import timedelta

    nophone = Customer(name="Gupta Traders", shop_name="Gupta Traders")
    idle = Customer(name="Idle No Phone", shop_name="Idle No Phone")
    db.add_all([nophone, idle])
    db.flush()
    db.add(Invoice(customer=nophone, invoice_no="G1", invoice_date=clock.today(), due_date=clock.today() + timedelta(days=5),
                   amount=Decimal(100), amount_paid=Decimal(0), status="open"))
    db.commit()
    html = logged_in.get("/customers?missing=phone").text
    assert "Gupta Traders" in html and "Idle No Phone" not in html and "Sharma Store" not in html


def test_pwa_manifest_and_icons_served(client):
    import json

    manifest = json.loads(client.get("/static/manifest.webmanifest").text)
    for icon in manifest["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200 and r.headers["content-type"] == icon["type"], icon["src"]
    assert {"192x192", "512x512"} <= {i["sizes"] for i in manifest["icons"]}
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])


def test_every_form_has_labels_for_its_inputs(logged_in, data):
    """Every visible input/select on every page has a <label for=...> or is wrapped in a label."""
    import re

    for url in PAGES + [f"/customers/{data['customer'].id}"]:
        html = logged_in.get(url).text
        labelled = set(re.findall(r'<label[^>]*for="([^"]+)"', html))
        for tag in re.findall(r"<(?:input|select)\b[^>]*>", html):
            if 'type="hidden"' in tag or 'type="checkbox"' in tag:  # checkboxes are wrapped in their label
                continue
            m = re.search(r'id="([^"]+)"', tag)
            assert m and m.group(1) in labelled, f"{url}: unlabelled field {tag}"


# ---------- Phase 2: payments on the customer page ----------

def _pay(client, cid, **fields):
    return client.post(f"/customers/{cid}/payments", data={"csrf_token": client.csrf, **fields})


def test_payment_mistake_shows_message_and_keeps_what_was_typed(logged_in, db, data):
    c, inv = data["customer"], data["invoice"]
    r = _pay(logged_in, c.id, amount="9000", note="cheque 4411", **{f"alloc_{inv.id}": "6000"})
    assert r.status_code == 400
    assert "only has ₹5,000.00 left" in r.text
    assert 'value="9000"' in r.text and 'value="cheque 4411"' in r.text and 'value="6000"' in r.text
    assert db.query(Receipt).count() == 0


def test_payment_date_in_future_refused(logged_in, data):
    from datetime import timedelta

    tomorrow = (clock.today() + timedelta(days=1)).isoformat()
    r = _pay(logged_in, data["customer"].id, amount="100", received_on=tomorrow)
    assert r.status_code == 400 and "be in the future" in r.text


def test_leftover_becomes_credit_then_used_on_a_bill(logged_in, db, data):
    c, inv = data["customer"], data["invoice"]
    page = _pay(logged_in, c.id, amount="6000", **{f"alloc_{inv.id}": "4000"})
    assert page.status_code == 303
    html = logged_in.get(f"/customers/{c.id}").text
    assert "₹2,000 kept as credit" in html and "Use ₹2,000 credit" in html
    r = logged_in.post(f"/customers/{c.id}/credit", data={"csrf_token": logged_in.csrf, f"alloc_{inv.id}": "1000"})
    assert r.status_code == 303
    db.refresh(inv)
    assert inv.status == "paid" and inv.balance == 0


def test_undo_payment_from_customer_page(logged_in, db, data):
    c, inv = data["customer"], data["invoice"]
    _pay(logged_in, c.id, amount="1500", **{f"alloc_{inv.id}": "1500"})
    rid = db.query(Receipt).one().id
    r = logged_in.post(f"/receipts/{rid}/undo", data={"csrf_token": logged_in.csrf}, follow_redirects=True)
    assert "Payment of ₹1,500 removed." in r.text
    db.refresh(inv)
    assert inv.amount_paid == 0 and inv.status == "open"


def test_collected_on_today_page_counts_new_payments(logged_in, data):
    c, inv = data["customer"], data["invoice"]
    _pay(logged_in, c.id, amount="1234", **{f"alloc_{inv.id}": "1234"})
    html = logged_in.get("/").text
    assert html.index("₹1,234") < html.index("Due to you")
