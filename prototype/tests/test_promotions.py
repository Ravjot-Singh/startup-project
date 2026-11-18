"""Products, catalogues, leads and promotions."""

import io
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app import catalogue, leads, promotions, settings, storage
from app.importers.outstanding import import_outstanding
from app.jobs import run_tick
from app.models import Campaign, Customer, InboundMessage, Message, Product
from app.routes.webhooks import handle_payload
from app.whatsapp.provider import DryRunProvider, SendResult

NOW = datetime(2026, 11, 10, 11, 0)
PDF = b"%PDF-1.4 catalogue"


def png(w=40, h=30, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def person(db, name, n, lead=False, consent=True, **kw):
    c = Customer(name=name, shop_name=name, phone=kw.pop("phone", f"9198000010{n:02d}"), is_lead=lead,
                 marketing_opt_in=consent, **kw)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def crowd(db):
    """6 customers and 3 leads with a mix of areas, tags and consent."""
    people = [
        person(db, "Sharma Store", 1, area="Model Town", salesman="Ravi", tags="grocery"),
        person(db, "Punjab Grill", 2, area="Civil Lines", salesman="Ravi", tags="hotel"),
        person(db, "Gupta Kirana", 3, area="Model Town", salesman="Aman", tags="grocery,wholesale"),
        person(db, "No Consent Mart", 4, consent=False, area="Model Town"),
        person(db, "No Number Dhaba", 5, phone=None, area="Model Town"),
        person(db, "Said Stop Foods", 6, whatsapp_opt_in=False, area="Model Town"),
        person(db, "Lead Hotel", 7, lead=True, area="Civil Lines", tags="hotel"),
        person(db, "Lead Cafe", 8, lead=True, area="Model Town", tags="cafe"),
        person(db, "Paused Lead", 9, lead=True, reminders_paused=True),
    ]
    db.commit()
    return people


def names(aud):
    return sorted(c.name for c in aud.recipients)


# ---------- audience ----------

def test_audience_kinds_and_exclusions(db, crowd):
    aud = promotions.audience(db, "customers")
    assert names(aud) == ["Gupta Kirana", "Punjab Grill", "Sharma Store"]
    assert aud.matched == 6
    assert dict(aud.left_out) == {"haven't agreed to offers": 1, "no WhatsApp number": 1, "said STOP to WhatsApp messages": 1}
    assert names(promotions.audience(db, "leads")) == ["Lead Cafe", "Lead Hotel"]
    assert promotions.audience(db, "everyone").count == 5


@pytest.mark.parametrize("filters,expected", [
    ({"area": "model town"}, ["Gupta Kirana", "Sharma Store"]),
    ({"salesman": "Ravi"}, ["Punjab Grill", "Sharma Store"]),
    ({"tag": "Grocery"}, ["Gupta Kirana", "Sharma Store"]),
    ({"tag": "wholesale", "area": "Model Town"}, ["Gupta Kirana"]),
])
def test_audience_filters(db, crowd, filters, expected):
    assert names(promotions.audience(db, "customers", **filters)) == expected


def test_hotel_tag_across_customers_and_leads(db, crowd):
    assert names(promotions.audience(db, "everyone", tag="hotel")) == ["Lead Hotel", "Punjab Grill"]


@pytest.mark.parametrize("rate,n,expected", [("0.80", 250, "200.00"), ("0.7846", 3, "2.35"), ("0", 99, "0.00")])
def test_cost_estimate(db, rate, n, expected):
    settings.set_value(db, "marketing_message_cost", rate)
    assert promotions.estimate_cost(db, n) == Decimal(expected)


# ---------- creating ----------

def _catalogue_campaign(db, **kw):
    f = catalogue.add_catalogue(db, "Nov.pdf", PDF)
    return promotions.create(db, kw.pop("name", "Nov"), "catalogue", kw.pop("line", "New winter range is in."),
                             kw.pop("audience", "customers"), catalogue_file_id=f.id, **kw)


def test_create_needs_the_right_content(db):
    with pytest.raises(promotions.PromotionError, match="Choose a catalogue"):
        promotions.create(db, "X", "catalogue", "", "customers")
    with pytest.raises(promotions.PromotionError, match="Write the offer"):
        promotions.create(db, "X", "offer", "", "customers", image=("a.png", png()))
    with pytest.raises(promotions.PromotionError, match="Add a photo"):
        promotions.create(db, "X", "offer", "10% off", "customers")
    with pytest.raises(promotions.PromotionError, match="not a photo"):
        promotions.create(db, "X", "offer", "10% off", "customers", image=("a.png", b"not an image"))
    c = promotions.create(db, "X", "offer", "10% off paneer", "customers", image=("offer.png", png()))
    assert storage.load(db, c.image_file_id).content_type == "image/jpeg"


def test_offer_can_use_a_product_photo(db):
    p = catalogue.save_product(db, Product(is_active=True), "Paneer", "Dairy", "1 kg", "320", ("paneer.png", png()))
    c = promotions.create(db, "X", "offer", "10% off", "leads", product_photo_id=p.photo_file_id)
    assert c.image_file_id == p.photo_file_id


# ---------- sending ----------

def _promo_sends(p):
    return [(to, t) for to, t, _ in p.sent if t in ("catalogue_promo", "offer_image")]


def test_queue_once_and_send_in_batches(db, crowd):
    settings.set_value(db, "promo_batch_size", 2)
    c = _catalogue_campaign(db, audience="everyone")
    assert promotions.queue(db, c, now=NOW) == 5
    with pytest.raises(promotions.PromotionError, match="already been sent"):
        promotions.queue(db, c, now=NOW)
    p = DryRunProvider()
    sizes = []
    for i in range(4):
        before = len(p.sent)
        run_tick(db, p, now=NOW + timedelta(minutes=10 * i))
        sizes.append(len(p.sent) - before)
    assert sizes == [2, 2, 1, 0]
    assert len(set(_promo_sends(p))) == 5 == len(_promo_sends(p))  # everyone once, nobody twice
    db.refresh(c)
    assert c.status == "done"
    media = p.media[0][2]
    assert media.data == PDF and media.filename == "Nov.pdf" and p.sent[0][2][2] == "New winter range is in."


def test_interrupted_tick_carries_on_without_duplicates(db, crowd):
    c = _catalogue_campaign(db, audience="everyone")
    promotions.queue(db, c, now=NOW)

    class Crashes(DryRunProvider):
        def send_template(self, to, template, params, media=None):
            if len(self.sent) == 2:
                raise RuntimeError("server restarted")
            return super().send_template(to, template, params, media)

    crash = Crashes()
    with pytest.raises(RuntimeError):
        run_tick(db, crash, now=NOW)
    db.rollback()
    p = DryRunProvider()
    run_tick(db, p, now=NOW + timedelta(minutes=10))
    delivered = [to for to, _ in _promo_sends(crash)] + [to for to, _ in _promo_sends(p)]
    assert sorted(delivered) == sorted(set(delivered)) and len(delivered) == 5


def test_consent_withdrawn_after_queueing_is_respected(db, crowd):
    c = _catalogue_campaign(db, audience="leads")
    promotions.queue(db, c, now=NOW)
    lead = db.scalar(select(Customer).where(Customer.name == "Lead Hotel"))
    handle_payload(db, {"entry": [{"changes": [{"value": {"messages": [
        {"from": lead.phone, "type": "text", "text": {"body": "STOP"}}]}}]}]})
    p = DryRunProvider()
    run_tick(db, p, now=NOW)
    assert [to for to, _ in _promo_sends(p)] == [db.scalar(select(Customer.phone).where(Customer.name == "Lead Cafe"))]
    assert promotions.report(db, c).not_sent == 1


def test_scheduled_promotion_waits_then_sends(db, crowd):
    c = _catalogue_campaign(db)
    promotions.queue(db, c, when=NOW + timedelta(days=1), now=NOW)
    assert c.status == "scheduled"
    p = DryRunProvider()
    run_tick(db, p, now=NOW + timedelta(hours=5))
    assert _promo_sends(p) == []
    run_tick(db, p, now=NOW + timedelta(days=1))
    assert len(_promo_sends(p)) == 3
    with pytest.raises(promotions.PromotionError, match="future"):
        promotions.queue(db, _catalogue_campaign(db, name="Y"), when=NOW - timedelta(minutes=1), now=NOW)


def test_cancel_stops_the_rest(db, crowd):
    settings.set_value(db, "promo_batch_size", 1)
    c = _catalogue_campaign(db)
    promotions.queue(db, c, now=NOW)
    p = DryRunProvider()
    run_tick(db, p, now=NOW)
    assert promotions.cancel(db, c) == 2
    run_tick(db, p, now=NOW + timedelta(minutes=10))
    assert len(_promo_sends(p)) == 1 and c.status == "cancelled"


def test_pause_all_holds_promotions(db, crowd):
    c = _catalogue_campaign(db)
    promotions.queue(db, c, now=NOW)
    settings.set_value(db, "sending_paused", True)
    p = DryRunProvider()
    run_tick(db, p, now=NOW)
    assert _promo_sends(p) == []


def test_report_counts_statuses_and_replies(db, crowd):
    c = _catalogue_campaign(db)
    promotions.queue(db, c, now=NOW)

    class OneFails(DryRunProvider):
        def send_template(self, to, template, params, media=None):
            if to.endswith("02"):
                return SendResult(ok=False, error="131026: not on WhatsApp", status="failed")
            return super().send_template(to, template, params, media)

    for i in range(3):  # the failing one is retried until it gives up
        run_tick(db, OneFails(), now=NOW + timedelta(minutes=10 * i))
    msgs = db.scalars(select(Message).where(Message.campaign_id == c.id, Message.status == "dryrun")).all()
    msgs[0].status = "read"
    db.add(InboundMessage(customer_id=msgs[0].customer_id, phone="x", text="Rate for paneer?", received_at=NOW + timedelta(hours=1)))
    db.commit()
    r = promotions.report(db, c)
    assert (r.total, r.sent, r.read, r.delivered, r.failed, r.replies, r.waiting) == (3, 2, 1, 1, 1, 1, 0)


def test_empty_audience_cannot_be_sent(db):
    c = _catalogue_campaign(db, audience="leads")
    with pytest.raises(promotions.PromotionError, match="Nobody"):
        promotions.queue(db, c, now=NOW)


# ---------- products and the catalogue PDF ----------

def test_product_photo_is_shrunk_to_jpeg(db):
    f = catalogue.save_photo(db, "big.png", png(3000, 1500))
    img = Image.open(io.BytesIO(f.data))
    assert f.content_type == "image/jpeg" and img.format == "JPEG" and max(img.size) == 1000


def test_catalogue_pdf_lists_active_products_with_photos(db):
    catalogue.save_product(db, Product(is_active=True), "Malai Paneer", "Dairy", "1 kg pack", "320", ("p.png", png()))
    catalogue.save_product(db, Product(is_active=True), "Frozen Peas", "Vegetables", "500 g", "1,250.50")
    gone = catalogue.save_product(db, Product(is_active=True), "Old Item", "Dairy", "", "")
    gone.is_active = False
    db.commit()
    pdf = PdfReader(io.BytesIO(catalogue.build_catalogue_pdf(db)))
    text = " ".join(p.extract_text() for p in pdf.pages)
    for expected in ("Malai Paneer", "1 kg pack", "Rs. 320", "Frozen Peas", "Rs. 1,250.50", "DAIRY", "VEGETABLES"):
        assert expected in text, expected
    assert "Old Item" not in text
    assert len(pdf.pages[0].images) == 1


def test_catalogue_spreads_over_pages(db):
    for i in range(14):
        catalogue.save_product(db, Product(is_active=True), f"Item {i:02d}", "Frozen", "", str(10 + i))
    pdf = PdfReader(io.BytesIO(catalogue.build_catalogue_pdf(db)))
    assert len(pdf.pages) >= 2 and "Item 13" in pdf.pages[-1].extract_text()


def test_catalogue_needs_products(db):
    with pytest.raises(storage.StorageError, match="Add some products"):
        catalogue.build_catalogue_pdf(db)


@pytest.mark.parametrize("price", ["abc", "-5"])
def test_bad_product_price(db, price):
    with pytest.raises(storage.StorageError):
        catalogue.save_product(db, Product(is_active=True), "X", "", "", price)


def test_catalogue_used_by_a_promotion_is_kept(db, crowd):
    c = _catalogue_campaign(db)
    with pytest.raises(storage.StorageError, match="uses this catalogue"):
        catalogue.delete_catalogue(db, c.catalogue_file_id)


# ---------- leads ----------

def test_add_lead_and_merge_by_number(db):
    lead, created = leads.add_or_update(db, "Hotel Blue", "98111 00011", "trade fair form", area="Civil Lines", tags="Hotel")
    assert created and lead.is_lead and lead.phone == "919811100011" and lead.marketing_opt_in
    assert lead.marketing_consent_source == "trade fair form" and lead.marketing_consent_at
    again, created = leads.add_or_update(db, "Hotel Blue Ltd", "+91 98111-00011", "phone call", tags="wholesale, hotel")
    assert not created and again.id == lead.id and again.tag_list == ["hotel", "wholesale"]
    assert again.marketing_consent_source == "trade fair form"  # first consent record kept


@pytest.mark.parametrize("phone,consent,message", [("123", "form", "mobile number"), ("9811100011", " ", "how they agreed")])
def test_lead_needs_number_and_consent(db, phone, consent, message):
    with pytest.raises(leads.LeadError, match=message):
        leads.add_or_update(db, "X", phone, consent)


def test_existing_customer_found_by_number_gets_consent_not_duplicated(db):
    c = person(db, "Sharma Store", 1, consent=False)
    db.commit()
    same, created = leads.add_or_update(db, "Sharma", c.phone[2:], "asked at shop", tags="grocery")
    assert not created and same.id == c.id and not same.is_lead and same.marketing_opt_in


def test_import_leads_csv(db):
    csv = ("Name,Mobile,Area,Tags,Consent\n"
           "Cafe One,9811100001,Model Town,cafe,signed up online\n"
           "Cafe Two,9811100002,,cafe;hotel,\n"
           "Bad Number,12345,,,\n")
    res = leads.import_leads(db, "leads.csv", csv.encode(), "trade fair")
    assert (res.added, res.updated) == (2, 0) and len(res.errors) == 1 and "Row 4" in res.errors[0]
    one = db.scalar(select(Customer).where(Customer.name == "Cafe One"))
    two = db.scalar(select(Customer).where(Customer.name == "Cafe Two"))
    assert one.marketing_consent_source == "signed up online" and two.marketing_consent_source == "trade fair"
    assert two.tag_list == ["cafe", "hotel"]


def test_import_leads_without_any_consent_is_refused(db):
    from app.importers.outstanding import ImportError_

    with pytest.raises(ImportError_, match="agreed"):
        leads.import_leads(db, "leads.csv", b"Name,Mobile\nA,9811100001\n", "")


def test_lead_becomes_customer_when_billed(db):
    lead, _ = leads.add_or_update(db, "Hotel Blue", "9811100011", "trade fair")
    db.commit()
    import_outstanding(db, "busy.csv", b"Bill No,Party Name,Bill Date,Bill Amount,Pending Amount\nB1,Hotel Blue,01-11-2026,900,900\n",
                       full_outstanding_report=False)
    db.refresh(lead)
    assert lead.is_lead is False and lead.marketing_opt_in


@pytest.mark.parametrize("text,expected", [("Hotel, wholesale ;Hotel", "hotel,wholesale"), ("", None), (" , ", None)])
def test_normalize_tags(text, expected):
    assert leads.normalize_tags(text) == expected
