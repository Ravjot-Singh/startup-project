"""Leads: people who aren't customers yet, added so promotions can reach them.

A lead is a Customer row with is_lead=True (so phone numbers, consent, STOP replies and the outbox all
work the same). Every lead needs recorded consent to marketing messages: how they agreed, and when.
When a Busy upload has a bill for them, they become a normal customer.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.importers.outstanding import ImportError_, _cell_text, _getter, clean_name, normalize_phone, read_table
from app.models import Customer

TAGS_COLUMNS = ["tags", "tag", "group", "segment"]
CONSENT_COLUMNS = ["consent", "how agreed", "agreed", "opt in", "optin"]


class LeadError(ValueError):
    """Shown to the owner as-is."""


def normalize_tags(text: str | None) -> str | None:
    """'Hotel, wholesale ;Hotel' -> 'hotel,wholesale'."""
    tags = []
    for t in re.split(r"[,;]", text or ""):
        t = " ".join(t.lower().split())
        if t and t not in tags:
            tags.append(t[:40])
    return ",".join(tags) or None


def merge_tags(existing: str | None, new: str | None) -> str | None:
    return normalize_tags(",".join(filter(None, [existing, new])))


def add_or_update(db: Session, name: str, phone: str, consent_source: str, area: str = "", tags: str = "") -> tuple[Customer, bool]:
    """Add a lead (or update the customer/lead with that number). Returns (person, created)."""
    normalized = normalize_phone(phone)
    if not normalized:
        raise LeadError(f"“{phone}” doesn't look like a mobile number")
    if not (consent_source or "").strip():
        raise LeadError("Say how they agreed to receive offers (e.g. filled our form at the trade fair)")
    person = db.scalar(select(Customer).where(Customer.phone == normalized))
    created = person is None
    if created:
        if not (name or "").strip():
            raise LeadError("Give the lead a name")
        person = Customer(name=name.strip()[:200], shop_name=name.strip()[:200], phone=normalized, is_lead=True, whatsapp_opt_in=True)
        db.add(person)
    if area.strip() and not person.area:
        person.area = area.strip()[:100]
    person.tags = merge_tags(person.tags, tags)
    if not person.marketing_opt_in:
        person.marketing_opt_in = True
        person.marketing_consent_at = clock.now()
        person.marketing_consent_source = consent_source.strip()[:200]
    db.flush()
    return person, created


@dataclass
class LeadImportResult:
    added: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def import_leads(db: Session, filename: str, content: bytes, consent_source: str) -> LeadImportResult:
    """CSV/Excel with Name and Mobile columns (Area, Tags and a per-row Consent column are optional)."""
    _, rows, cmap, header_line = read_table(filename, content, ("customer_name", "phone"))
    headers = {h.strip().lower(): h for r in rows if r for h in r}
    tags_col = next((headers[c] for c in TAGS_COLUMNS if c in headers), None)
    consent_col = next((headers[c] for c in CONSENT_COLUMNS if c in headers), None)
    if not consent_col and not (consent_source or "").strip():
        raise ImportError_("Say how these people agreed to receive offers (or add a Consent column to the file)")
    get = _getter(cmap)
    result = LeadImportResult()
    for line_no, row in enumerate(rows, start=header_line + 1):
        if row is None:
            continue
        try:
            consent = _cell_text(row.get(consent_col)) if consent_col else ""
            _, created = add_or_update(
                db, clean_name(get(row, "customer_name")), _cell_text(get(row, "phone")), consent or consent_source,
                area=_cell_text(get(row, "area")), tags=_cell_text(row.get(tags_col)) if tags_col else "",
            )
            result.added += created
            result.updated += not created
        except LeadError as e:
            result.errors.append(f"Row {line_no}: {e}")
    db.commit()
    return result
