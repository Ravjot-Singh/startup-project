"""Bill PDFs: the one sent on WhatsApp is the bill exported from Busy if the owner uploaded it,
otherwise a one-page bill summary generated here (amount, paid so far, balance, UPI QR code).

The generated PDF is a payment summary, not a tax invoice: the GST invoice is the one issued in Busy.
"""

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import segno
from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, config, storage, ui
from app.models import Invoice, StoredFile

BILL_PDF = "bill_pdf"
PDF_MIME = "application/pdf"


@dataclass
class BillPdf:
    data: bytes
    filename: str
    from_busy: bool


def _latin1(text: str) -> str:
    """The built-in PDF fonts only cover Latin-1; anything else (e.g. Hindi) becomes '?'."""
    return (text or "").replace("₹", "Rs. ").encode("latin-1", "replace").decode("latin-1")


def pdf_filename(invoice: Invoice) -> str:
    return "Bill-" + re.sub(r"[^A-Za-z0-9._-]+", "-", invoice.invoice_no).strip("-") + ".pdf"


def upi_uri(amount: Decimal, note: str) -> str:
    from urllib.parse import quote

    return (f"upi://pay?pa={quote(config.BUSINESS_UPI_ID)}&pn={quote(config.BUSINESS_NAME)}"
            f"&am={amount:.2f}&cu=INR&tn={quote(note)}")


def _qr_image(text: str):
    """QR code as an RGB image (fpdf2 garbles segno's 1-bit PNG, so it is converted first)."""
    from PIL import Image

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=2)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def generate_summary_pdf(invoice: Invoice, today: date | None = None) -> bytes:
    today = today or clock.today()
    c = invoice.customer
    balance = max(invoice.balance, Decimal(0))
    pdf = FPDF(format="A5")
    pdf.set_auto_page_break(False)
    pdf.set_title(_latin1(f"Bill {invoice.invoice_no}"))
    pdf.set_author(_latin1(config.BUSINESS_NAME))
    pdf.add_page()
    pdf.set_margins(14, 14, 14)
    pdf.set_xy(14, 14)

    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 9, _latin1(config.BUSINESS_NAME), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90, 96, 105)
    pdf.cell(0, 6, _latin1(f"Bill summary · {today.strftime('%d %b %Y')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_text_color(31, 41, 51)
    rows = [
        ("Customer", c.display_name),
        ("Bill number", invoice.invoice_no),
        ("Bill date", invoice.invoice_date.strftime("%d %b %Y")),
        ("Due date", invoice.due_date.strftime("%d %b %Y")),
        ("Bill amount", f"Rs. {ui.inr(invoice.amount, decimals=True)}"),
        ("Paid so far", f"Rs. {ui.inr(invoice.amount_paid or 0, decimals=True)}"),
    ]
    for label, value in rows:
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(42, 8, _latin1(label))
        pdf.cell(0, 8, _latin1(value), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(226, 224, 218)
    pdf.line(14, pdf.get_y() + 1, 134, pdf.get_y() + 1)
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(42, 10, "Balance due")
    pdf.cell(0, 10, _latin1(f"Rs. {ui.inr(balance, decimals=True)}" if balance > 0 else "Nil - paid in full, thank you"),
             new_x="LMARGIN", new_y="NEXT")

    if balance > 0 and config.BUSINESS_UPI_ID:
        pdf.ln(4)
        pdf.set_font("Helvetica", "", 10)
        top = pdf.get_y()
        pdf.multi_cell(70, 5.5, _latin1(f"Pay by UPI: scan the code, or pay to {config.BUSINESS_UPI_ID}"),
                       align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.image(_qr_image(upi_uri(balance, f"Bill {invoice.invoice_no}")), x=88, y=top, w=42, h=42)

    pdf.set_xy(14, 186)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 96, 105)
    pdf.multi_cell(120, 4, _latin1("This summary is from our payment records. Your tax invoice is the bill "
                                   "issued at the time of sale. Please ignore the balance if already paid."))
    return bytes(pdf.output())


def bill_pdf(db: Session, invoice: Invoice) -> BillPdf:
    """The Busy PDF if one was uploaded for this bill, otherwise a generated summary."""
    if invoice.bill_file_id:
        f = storage.load(db, invoice.bill_file_id)
        if f is not None:
            return BillPdf(data=f.data, filename=pdf_filename(invoice), from_busy=True)
    return BillPdf(data=generate_summary_pdf(invoice), filename=pdf_filename(invoice), from_busy=False)


# ---------- matching uploaded Busy PDFs to bills ----------

@dataclass
class UploadResult:
    matched: list[tuple[str, Invoice]] = field(default_factory=list)  # (file name, bill)
    unmatched: list[tuple[str, str]] = field(default_factory=list)    # (file name, why)


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _tokens(stem: str) -> set[str]:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem.upper()) if p]
    out = set(parts)
    out |= {p.lstrip("0") for p in parts if p.isdigit() and p.lstrip("0")}
    return out


def find_bill_for_file(filename: str, invoices: list[Invoice]) -> tuple[Invoice | None, str | None]:
    """Which bill a Busy PDF belongs to, from its file name ('INV1001.pdf', 'Sales Bill 1001 Sharma.pdf'...)."""
    stem = filename.rsplit(".", 1)[0]
    whole, tokens = _norm(stem), _tokens(stem)
    hits = []
    for inv in invoices:
        no = _norm(inv.invoice_no)
        if not no:
            continue
        if whole == no or no in tokens or (no.isdigit() and no.lstrip("0") in tokens):
            hits.append(inv)
    if not hits:
        return None, "no bill with that number"
    latest_fy = max(i.fiscal_year for i in hits)
    hits = [i for i in hits if i.fiscal_year == latest_fy]  # same number in older years: the newest bill
    if len({i.id for i in hits}) > 1:
        return None, "matches more than one bill: " + ", ".join(sorted(i.invoice_no for i in hits))
    return hits[0], None


def attach_busy_pdfs(db: Session, files: list[tuple[str, bytes]]) -> UploadResult:
    """Store uploaded Busy bill PDFs against their bills (a newer upload replaces an older one)."""
    invoices = list(db.scalars(select(Invoice)).all())
    result = UploadResult()
    for filename, data in files:
        if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
            result.unmatched.append((filename, "not a PDF file"))
            continue
        inv, why = find_bill_for_file(filename, invoices)
        if inv is None:
            result.unmatched.append((filename, why))
            continue
        try:
            stored = storage.save(db, BILL_PDF, filename, PDF_MIME, data)
        except storage.StorageError as e:
            result.unmatched.append((filename, str(e)))
            continue
        old = inv.bill_file_id
        inv.bill_file_id = stored.id
        if old:
            db.flush()
            old_file = db.get(StoredFile, old)
            if old_file is not None:
                db.delete(old_file)
        result.matched.append((filename, inv))
    db.commit()
    return result
