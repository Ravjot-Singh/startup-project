"""Bill PDFs: the generated summary, matching Busy PDFs to bills, and sending bills on WhatsApp."""

import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import segno
from pypdf import PdfReader
from sqlalchemy import select

from app import bills, config, settings
from app.importers.outstanding import import_outstanding
from app.models import Customer, Invoice, Message, StoredFile
from app.payments import record_payment
from app.reminders.engine import BILL_SEND_DELAY, send_bill_now, send_pending
from app.whatsapp.provider import DryRunProvider, SendResult
from app.whatsapp.templates import TEMPLATES

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 11, 0)
FAKE_PDF = b"%PDF-1.4 busy bill\n%%EOF"


def cust(db, name="Sharma General Store", **kw):
    c = Customer(name=name, shop_name=name, phone=kw.pop("phone", "919800000001"), **kw)
    db.add(c)
    db.flush()
    return c


def bill(db, c, no="INV1001", amount=8200, paid=0, invoice_date=TODAY, fy=None):
    inv = Invoice(customer=c, invoice_no=no, invoice_date=invoice_date, due_date=invoice_date + timedelta(days=15),
                  amount=Decimal(amount), amount_paid=Decimal(paid), **({"fiscal_year": fy} if fy else {}))
    inv.refresh_status()
    db.add(inv)
    db.commit()
    return inv


def text_of(pdf: bytes) -> str:
    return " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


# ---------- the generated bill summary ----------

def test_summary_pdf_has_the_bill_and_balance(db, monkeypatch):
    monkeypatch.setattr(config, "BUSINESS_UPI_ID", "abcfrozen@okaxis")
    inv = bill(db, cust(db), "INV0990", amount=4500, paid=1500)
    pdf = bills.generate_summary_pdf(inv, TODAY)
    assert pdf.startswith(b"%PDF")
    text = text_of(pdf)
    for expected in (config.BUSINESS_NAME, "Sharma General Store", "INV0990", "Rs. 4,500.00", "Rs. 1,500.00",
                     "Rs. 3,000.00", "abcfrozen@okaxis", "tax invoice"):
        assert expected in text, expected
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1


def test_qr_code_in_pdf_is_the_upi_payment_link(db, monkeypatch):
    """Read the embedded QR image back module by module and compare it with the UPI link's QR."""
    monkeypatch.setattr(config, "BUSINESS_UPI_ID", "abcfrozen@okaxis")
    inv = bill(db, cust(db), "INV7", amount=1200)
    img = PdfReader(io.BytesIO(bills.generate_summary_pdf(inv, TODAY))).pages[0].images[0].image.convert("L")
    expected = segno.make(bills.upi_uri(Decimal(1200), "Bill INV7"), error="m")
    scale, border = 8, 2
    for y, row in enumerate(expected.matrix):
        for x, dark in enumerate(row):
            pixel = img.getpixel(((x + border) * scale + scale // 2, (y + border) * scale + scale // 2))
            assert (pixel < 128) == bool(dark), (x, y)


def test_paid_bill_summary_has_no_qr_and_says_paid(db, monkeypatch):
    monkeypatch.setattr(config, "BUSINESS_UPI_ID", "abcfrozen@okaxis")
    inv = bill(db, cust(db), amount=500, paid=500)
    pdf = bills.generate_summary_pdf(inv, TODAY)
    assert "paid in full" in text_of(pdf)
    assert len(PdfReader(io.BytesIO(pdf)).pages[0].images) == 0


def test_names_outside_latin1_do_not_break_the_pdf(db):
    inv = bill(db, cust(db, "शर्मा स्टोर"))
    assert bills.generate_summary_pdf(inv, TODAY).startswith(b"%PDF")


# ---------- matching Busy PDFs ----------

@pytest.mark.parametrize("filename,expected", [
    ("INV1001.pdf", "INV1001"),
    ("inv1001.PDF", "INV1001"),
    ("Sales Bill INV1001 Sharma.pdf", "INV1001"),
    ("INV-1001.pdf", "INV1001"),
    ("1002.pdf", "1002"),
    ("Bill_0001002.pdf", "1002"),
    ("INV10011.pdf", None),
    ("random.pdf", None),
])
def test_find_bill_for_file(db, filename, expected):
    c = cust(db)
    invoices = [bill(db, c, "INV1001"), bill(db, c, "1002")]
    inv, why = bills.find_bill_for_file(filename, invoices)
    assert (inv.invoice_no if inv else None) == expected
    if expected is None:
        assert why


def test_same_number_in_two_years_goes_to_the_newer_bill(db):
    c = cust(db)
    old = bill(db, c, "101", invoice_date=date(2025, 6, 1))
    new = bill(db, c, "101", invoice_date=date(2026, 6, 1))
    assert bills.find_bill_for_file("101.pdf", [old, new])[0] is new


def test_attach_busy_pdfs_reports_what_did_not_match(db):
    c = cust(db)
    inv = bill(db, c, "INV1001")
    res = bills.attach_busy_pdfs(db, [("INV1001.pdf", FAKE_PDF), ("INV9999.pdf", FAKE_PDF), ("INV1001.txt", b"hello")])
    assert [(n, i.invoice_no) for n, i in res.matched] == [("INV1001.pdf", "INV1001")]
    assert dict(res.unmatched) == {"INV9999.pdf": "no bill with that number", "INV1001.txt": "not a PDF file"}
    assert bills.bill_pdf(db, inv).from_busy and bills.bill_pdf(db, inv).data == FAKE_PDF


def test_a_newer_busy_pdf_replaces_the_old_one(db):
    inv = bill(db, cust(db), "INV1001")
    bills.attach_busy_pdfs(db, [("INV1001.pdf", FAKE_PDF)])
    bills.attach_busy_pdfs(db, [("INV1001 (corrected).pdf", FAKE_PDF + b" v2")])
    assert db.query(StoredFile).count() == 1
    assert bills.bill_pdf(db, inv).data.endswith(b"v2")


def test_pdf_too_large_is_refused(db, monkeypatch):
    from app import storage

    bill(db, cust(db), "INV1001")
    monkeypatch.setattr(storage, "MAX_BYTES", 10)
    res = bills.attach_busy_pdfs(db, [("INV1001.pdf", FAKE_PDF)])
    assert res.matched == [] and "larger than" in res.unmatched[0][1]


# ---------- sending bills ----------

def test_send_bill_now_attaches_the_summary_pdf(db):
    inv = bill(db, cust(db), "INV1001", amount=8200)
    p = DryRunProvider()
    msg = send_bill_now(db, p, inv, now=NOW)
    assert msg.is_sent and msg.kind == "bill"
    (to, template, media), = p.media
    assert template == "bill_document" and media.mime == "application/pdf" and media.filename == "Bill-INV1001.pdf"
    assert media.data.startswith(b"%PDF") and "INV1001" in text_of(media.data)
    assert p.sent[0][2][2:6] == ["INV1001", "10-Nov-2026", "8,200", "8,200"]
    assert "[Attached: Bill-INV1001.pdf, bill summary]" in msg.message_preview


def test_send_bill_uses_the_busy_pdf_when_uploaded(db):
    inv = bill(db, cust(db), "INV1001")
    bills.attach_busy_pdfs(db, [("INV1001.pdf", FAKE_PDF)])
    p = DryRunProvider()
    msg = send_bill_now(db, p, inv, now=NOW)
    assert p.media[0][2].data == FAKE_PDF and "from Busy" in msg.message_preview


def test_bill_goes_out_even_if_reminders_are_paused_but_not_if_opted_out(db):
    c = cust(db, reminders_paused=True)
    inv = bill(db, c)
    p = DryRunProvider()
    assert send_bill_now(db, p, inv, now=NOW).is_sent
    c.whatsapp_opt_in = False
    db.commit()
    assert send_bill_now(db, p, inv, now=NOW).status == "skipped"


def test_file_upload_failure_is_retried_not_lost(db):
    class FlakyUpload:
        name = "flaky"
        calls = 0

        def send_template(self, to, template, params, media=None):
            FlakyUpload.calls += 1
            if FlakyUpload.calls == 1:
                return SendResult(ok=False, error="file upload failed: 131053: Media upload error", status="failed")
            return SendResult(ok=True, message_id="wamid.ok", status="sent")

    inv = bill(db, cust(db))
    msg = send_bill_now(db, FlakyUpload(), inv, now=NOW)
    assert msg.status == "queued" and "Media upload error" in msg.error
    send_pending(db, FlakyUpload(), NOW + timedelta(minutes=10))
    assert msg.status == "sent" and msg.attempts == 2


# ---------- new bills from a Busy upload ----------

HEAD = "Bill No,Party Name,Bill Date,Bill Amount,Pending Amount,Mobile\n"


def _upload(db, rows, today=TODAY):
    return import_outstanding(db, "busy.csv", (HEAD + rows).encode(), full_outstanding_report=False, today=today)


def _bill_messages(db):
    return list(db.scalars(select(Message).where(Message.kind == "bill")).all())


def test_upload_queues_one_pdf_per_new_recent_bill(db, monkeypatch):
    from app import clock

    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 18, 11, 0))  # time of day the upload happens
    res = _upload(db, "NEW1,Sharma,09-11-2026,1000,1000,9800000001\n"
                      "NEW2,Sharma,10-11-2026,2000,2000,9800000001\n"
                      "OLD1,Sharma,01-10-2026,3000,3000,9800000001\n"     # too old to send automatically
                      "PAID1,Gupta,10-11-2026,500,0,9800000002\n")        # already paid
    msgs = _bill_messages(db)
    assert res.bills_queued == 2 and sorted(m.invoice.invoice_no for m in msgs) == ["NEW1", "NEW2"]
    assert all(m.scheduled_for == datetime(2026, 11, 10, 11, 0) + BILL_SEND_DELAY and m.status == "queued" for m in msgs)
    _upload(db, "NEW1,Sharma,09-11-2026,1000,1000,9800000001\n")  # the same bill again: nothing new
    assert len(_bill_messages(db)) == 2


def test_queued_bill_waits_for_busy_pdfs_then_sends_them(db):
    _upload(db, "NEW1,Sharma,10-11-2026,1000,1000,9800000001\n")
    msg = _bill_messages(db)[0]
    p = DryRunProvider()
    send_pending(db, p, msg.scheduled_for - timedelta(minutes=1))
    assert p.sent == []
    bills.attach_busy_pdfs(db, [("NEW1.pdf", FAKE_PDF)])
    send_pending(db, p, msg.scheduled_for)
    assert msg.is_sent and p.media[0][2].data == FAKE_PDF


def test_automatic_bill_not_sent_if_paid_before_its_time(db):
    _upload(db, "NEW1,Sharma,10-11-2026,1000,1000,9800000001\n")
    msg = _bill_messages(db)[0]
    record_payment(db, msg.customer, Decimal(1000), TODAY, allocations={msg.invoice_id: Decimal(1000)})
    send_pending(db, DryRunProvider(), msg.scheduled_for)
    assert msg.status == "cancelled" and msg.error == "Already paid"


def test_switching_off_automatic_bills(db):
    settings.set_value(db, "send_bills_automatically", False)
    res = _upload(db, "NEW1,Sharma,10-11-2026,1000,1000,9800000001\n")
    assert res.bills_queued == 0 and _bill_messages(db) == []


def test_pause_all_holds_automatic_bills(db):
    from app.jobs import run_tick

    _upload(db, "NEW1,Sharma,10-11-2026,1000,1000,9800000001\n")
    msg = _bill_messages(db)[0]
    settings.set_value(db, "sending_paused", True)
    p = DryRunProvider()
    run_tick(db, p, now=msg.scheduled_for + timedelta(minutes=5))
    assert p.media == []


# ---------- the template list for Meta stays in step with the code ----------

def test_template_document_lists_every_template_text():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "whatsapp-templates.md").read_text(encoding="utf-8")
    for name, text in TEMPLATES.items():
        assert f"`{name}`" in doc, name
        assert text in doc, f"text of {name} differs from docs/whatsapp-templates.md"
