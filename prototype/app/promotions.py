"""Promotions: one marketing message (a catalogue PDF, or a photo with an offer) to many people.

* Audience: customers, leads or everyone, optionally narrowed by area, salesman or tag. Only people who
  agreed to offers (recorded consent), have a number, haven't said STOP and aren't paused are included.
* Queueing puts one outbox message per person (never twice: one per campaign and person). The outbox
  then sends promotion messages in batches of `promo_batch_size` per tick, so a big promotion goes out
  over a while instead of all at once, and simply carries on after an interruption.
* The cost shown is an estimate: recipients x Meta's marketing message rate (set on Settings).
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, config, settings, storage
from app.models import (
    CAMPAIGN_CANCELLED,
    CANCELLED,
    CONTENT_CATALOGUE,
    CONTENT_OFFER,
    DONE,
    DRAFT,
    KIND_PROMO,
    QUEUED,
    SCHEDULED,
    SENDING,
    SENT_STATUSES,
    Campaign,
    Customer,
    InboundMessage,
    Message,
)
from app.whatsapp.provider import MediaFile, WhatsAppProvider
from app.whatsapp.templates import CATALOGUE_PROMO_TEMPLATE, OFFER_TEMPLATE, render

AUDIENCES = {"customers": "Customers", "leads": "Leads", "everyone": "Customers and leads"}


class PromotionError(ValueError):
    """Shown to the owner as-is."""


# ---------- who gets it ----------

@dataclass
class Audience:
    recipients: list[Customer]
    matched: int  # people matching the filters, before consent checks
    left_out: Counter = field(default_factory=Counter)  # reason -> how many

    @property
    def count(self) -> int:
        return len(self.recipients)


def _why_left_out(c: Customer) -> str | None:
    if not c.phone:
        return "no WhatsApp number"
    if not c.whatsapp_opt_in:
        return "said STOP to WhatsApp messages"
    if not c.marketing_opt_in:
        return "haven't agreed to offers"
    if c.reminders_paused:
        return "messages paused by you"
    return None


def audience(db: Session, kind: str = "customers", area: str | None = None, salesman: str | None = None,
             tag: str | None = None) -> Audience:
    stmt = select(Customer).where(Customer.is_active).order_by(Customer.name)
    if kind == "customers":
        stmt = stmt.where(Customer.is_lead.is_(False))
    elif kind == "leads":
        stmt = stmt.where(Customer.is_lead.is_(True))
    if area:
        stmt = stmt.where(func.lower(Customer.area) == area.strip().lower())
    if salesman:
        stmt = stmt.where(func.lower(Customer.salesman) == salesman.strip().lower())
    people = list(db.scalars(stmt).all())
    if tag:
        tag = tag.strip().lower()
        people = [c for c in people if tag in c.tag_list]
    result = Audience(recipients=[], matched=len(people))
    for c in people:
        why = _why_left_out(c)
        if why:
            result.left_out[why] += 1
        else:
            result.recipients.append(c)
    return result


def campaign_audience(db: Session, c: Campaign) -> Audience:
    return audience(db, c.audience, c.area, c.salesman, c.tag)


def estimate_cost(db: Session, recipients: int) -> Decimal:
    rate = Decimal(settings.get(db, "marketing_message_cost") or "0")
    return (rate * recipients).quantize(Decimal("0.01"))


# ---------- creating and sending ----------

def create(db: Session, name: str, content: str, message_line: str, audience_kind: str,
           area: str = "", salesman: str = "", tag: str = "", catalogue_file_id: int | None = None,
           image: tuple[str, bytes] | None = None, product_photo_id: int | None = None) -> Campaign:
    from app import catalogue

    if not name.strip():
        raise PromotionError("Give the promotion a name (only you see it)")
    if audience_kind not in AUDIENCES:
        raise PromotionError("Choose who it goes to")
    if content not in (CONTENT_CATALOGUE, CONTENT_OFFER):
        raise PromotionError("Choose what to send")
    line = " ".join(message_line.split())[:300]
    c = Campaign(name=name.strip()[:200], content=content, message_line=line, audience=audience_kind,
                 area=area.strip() or None, salesman=salesman.strip() or None, tag=(tag.strip().lower() or None))
    if content == CONTENT_CATALOGUE:
        f = storage.load(db, catalogue_file_id) if catalogue_file_id else None
        if f is None or f.kind != catalogue.CATALOGUE:
            raise PromotionError("Choose a catalogue (build or upload one first)")
        c.catalogue_file_id = f.id
    else:
        if not line:
            raise PromotionError("Write the offer, e.g. “10% off all paneer this week”")
        if image and image[1]:
            try:
                c.image_file_id = catalogue.save_photo(db, image[0], image[1], kind=catalogue.OFFER_IMAGE).id
            except storage.StorageError as e:
                raise PromotionError(str(e)) from None
        elif product_photo_id and (f := storage.load(db, product_photo_id)) and f.kind == catalogue.PRODUCT_PHOTO:
            c.image_file_id = f.id
        else:
            raise PromotionError("Add a photo for the offer (upload one or pick a product)")
    db.add(c)
    db.commit()
    return c


def queue(db: Session, c: Campaign, when: datetime | None = None, now: datetime | None = None) -> int:
    """Put the promotion in the outbox for everyone in its audience. Returns how many."""
    now = now or clock.now()
    if c.status != DRAFT:
        raise PromotionError("This promotion has already been sent or scheduled")
    if when is not None and when <= now:
        raise PromotionError("Choose a time in the future, or send now")
    at = when or now
    people = campaign_audience(db, c).recipients
    if not people:
        raise PromotionError("Nobody in this audience has agreed to offers yet")
    for person in people:
        db.add(Message(customer=person, kind=KIND_PROMO, campaign=c, run_date=at.date(), scheduled_for=at,
                       status=QUEUED, idempotency_key=f"promo:{c.id}:{person.id}"))
    c.status, c.scheduled_for, c.queued_count = (SCHEDULED if when else SENDING), at, len(people)
    db.commit()
    return len(people)


def cancel(db: Session, c: Campaign) -> int:
    if c.status not in (DRAFT, SCHEDULED, SENDING):
        raise PromotionError("This promotion has already finished")
    pending = db.scalars(select(Message).where(Message.campaign_id == c.id, Message.status == QUEUED)).all()
    for m in pending:
        m.status, m.error = CANCELLED, "Promotion cancelled"
    c.status = CAMPAIGN_CANCELLED
    db.commit()
    return len(pending)


def refresh_statuses(db: Session, now: datetime) -> None:
    """Scheduled -> sending when its time comes; sending -> done when nothing is left to send."""
    for c in db.scalars(select(Campaign).where(Campaign.status.in_([SCHEDULED, SENDING]))).all():
        left = db.scalar(select(func.count(Message.id)).where(Message.campaign_id == c.id, Message.status == QUEUED))
        if not left:
            c.status = DONE
        elif c.status == SCHEDULED and c.scheduled_for and c.scheduled_for <= now:
            c.status = SENDING
    db.commit()


@dataclass
class Report:
    total: int
    waiting: int
    sent: int       # accepted by WhatsApp (incl. test mode)
    delivered: int  # reached the phone (delivered or read)
    read: int
    failed: int
    not_sent: int   # skipped (e.g. consent withdrawn meanwhile) or cancelled
    replies: int


def report(db: Session, c: Campaign) -> Report:
    counts = Counter(s for (s,) in db.execute(select(Message.status).where(Message.campaign_id == c.id)).all())
    replies = 0
    if c.scheduled_for:
        ids = select(Message.customer_id).where(Message.campaign_id == c.id)
        replies = db.scalar(select(func.count(InboundMessage.id)).where(
            InboundMessage.customer_id.in_(ids), InboundMessage.received_at >= c.scheduled_for)) or 0
    return Report(
        total=sum(counts.values()), waiting=counts[QUEUED],
        sent=sum(counts[s] for s in SENT_STATUSES), delivered=counts["delivered"] + counts["read"], read=counts["read"],
        failed=counts["failed"], not_sent=counts["skipped"] + counts[CANCELLED], replies=replies,
    )


# ---------- the message ----------

def send(db: Session, provider: WhatsAppProvider, msg: Message, now: datetime) -> str:
    from app.reminders.engine import _record_result

    c = msg.campaign
    if c is None or c.status == CAMPAIGN_CANCELLED:
        msg.status, msg.error = CANCELLED, "Promotion cancelled"
        db.commit()
        return "cancelled"
    file_id = c.catalogue_file_id if c.content == CONTENT_CATALOGUE else c.image_file_id
    f = storage.load(db, file_id) if file_id else None
    if f is None:
        msg.status, msg.error = CANCELLED, "The catalogue or photo was removed"
        db.commit()
        return "cancelled"
    template = CATALOGUE_PROMO_TEMPLATE if c.content == CONTENT_CATALOGUE else OFFER_TEMPLATE
    params = [msg.customer.display_name, config.BUSINESS_NAME, c.message_line or "Have a look at what's new."]
    msg.template_name = template
    msg.message_preview = render(template, params) + f"\n[Attached: {f.filename}]"
    result = provider.send_template(msg.customer.phone, template, params,
                                    media=MediaFile(data=f.data, mime=f.content_type, filename=f.filename))
    return _record_result(db, msg, result, now)
