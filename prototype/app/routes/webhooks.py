"""WhatsApp Cloud API webhook: delivery/read receipts and customer replies.

In Meta App Dashboard -> WhatsApp -> Configuration, set the callback URL to
https://<your-domain>/webhooks/whatsapp and the verify token to META_WA_VERIFY_TOKEN,
then subscribe to the 'messages' field.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.models import Customer, InboundMessage, ReminderLog

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("reminders.webhook")

STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == config.META_WA_VERIFY_TOKEN:
        return q.get("hub.challenge", "")
    raise HTTPException(status_code=403, detail="verification failed")


def handle_payload(db: Session, payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for st in value.get("statuses", []):
                log = db.scalar(select(ReminderLog).where(ReminderLog.provider_msg_id == st.get("id")))
                if not log:
                    continue
                new = st.get("status")
                if new == "failed":
                    log.status = "failed"
                    errs = st.get("errors") or [{}]
                    log.error = f"{errs[0].get('code')}: {errs[0].get('title') or errs[0].get('message')}"
                elif STATUS_ORDER.get(new, -1) > STATUS_ORDER.get(log.status, -1):
                    log.status = new  # receipts can arrive out of order; never go backwards
            for msg in value.get("messages", []):
                phone = msg.get("from", "")
                text = (msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or f"[{msg.get('type')}]"
                customer = db.scalar(select(Customer).where(Customer.phone == phone))
                db.add(InboundMessage(customer_id=customer.id if customer else None, phone=phone, text=text))
    db.commit()


def signature_ok(body: bytes, header: str | None, secret: str) -> bool:
    """Meta signs each webhook body: X-Hub-Signature-256: sha256=<hex HMAC of the raw body with the app secret>."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@router.post("/whatsapp")
async def receive(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if config.META_APP_SECRET:
        if not signature_ok(body, request.headers.get("X-Hub-Signature-256"), config.META_APP_SECRET):
            raise HTTPException(status_code=401, detail="bad signature")
    elif config.IS_PRODUCTION:
        # Without the secret anyone could post fake delivery receipts or replies.
        raise HTTPException(status_code=503, detail="webhook signature verification not configured")
    else:
        log.warning("META_APP_SECRET not set: accepting unsigned webhook (development only)")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    handle_payload(db, payload)
    return {"ok": True}
