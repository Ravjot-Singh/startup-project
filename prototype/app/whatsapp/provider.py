"""WhatsApp sending behind one interface so the provider can be swapped (Meta Cloud API, Interakt, AiSensy ...).

A template message can carry one file in its header: a document (e.g. a bill PDF) or an image (e.g. a
product photo). The provider uploads the file first and then sends the template pointing at it.
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx

from app import config


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    status: str = "sent"


@dataclass
class MediaFile:
    """A file to show at the top of a template message."""

    data: bytes
    mime: str      # "application/pdf", "image/jpeg", "image/png"
    filename: str  # shown to the customer for documents

    @property
    def header_type(self) -> str:
        return "image" if self.mime.startswith("image/") else "document"


class WhatsAppProvider(Protocol):
    name: str

    def send_template(self, to: str, template_name: str, params: list[str], media: MediaFile | None = None) -> SendResult: ...


class DryRunProvider:
    """Sends nothing. Use while testing with real data so the distributor can review messages first."""

    name = "dryrun"

    def __init__(self):
        self.sent: list[tuple[str, str, list[str]]] = []
        self.media: list[tuple[str, str, MediaFile]] = []  # (to, template, file) for messages with a file

    def send_template(self, to: str, template_name: str, params: list[str], media: MediaFile | None = None) -> SendResult:
        self.sent.append((to, template_name, params))
        if media is not None:
            self.media.append((to, template_name, media))
        return SendResult(ok=True, message_id=f"dryrun-{uuid.uuid4().hex[:12]}", status="dryrun")


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('code')}: {err.get('message')}"
    except ValueError:
        return resp.text[:300]


class MetaCloudProvider:
    """Meta WhatsApp Cloud API: https://developers.facebook.com/docs/whatsapp/cloud-api"""

    name = "meta"
    API_VERSION = "v21.0"

    def __init__(self, token: str, phone_number_id: str, lang: str = "en", client: httpx.Client | None = None):
        if not token or not phone_number_id:
            raise ValueError("META_WA_TOKEN and META_WA_PHONE_NUMBER_ID must be set to use the meta provider")
        base = f"https://graph.facebook.com/{self.API_VERSION}/{phone_number_id}"
        self.url = f"{base}/messages"
        self.media_url = f"{base}/media"
        self.lang = lang
        self.client = client or httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"})

    def upload_media(self, media: MediaFile) -> tuple[str | None, str | None]:
        """Upload a file to WhatsApp. Returns (media id, error). Media ids stay valid for 30 days."""
        try:
            resp = self.client.post(
                self.media_url,
                data={"messaging_product": "whatsapp", "type": media.mime},
                files={"file": (media.filename, media.data, media.mime)},
            )
        except httpx.HTTPError as e:
            return None, f"file upload failed: network error: {e}"
        if resp.status_code >= 400:
            return None, f"file upload failed: {_error_text(resp)}"
        media_id = resp.json().get("id")
        return (media_id, None) if media_id else (None, "file upload failed: no media id returned")

    def send_template(self, to: str, template_name: str, params: list[str], media: MediaFile | None = None) -> SendResult:
        components = []
        if media is not None:
            media_id, error = self.upload_media(media)
            if error:
                return SendResult(ok=False, error=error, status="failed")
            ref = {"id": media_id}
            if media.header_type == "document":
                ref["filename"] = media.filename
            components.append({"type": "header", "parameters": [{"type": media.header_type, media.header_type: ref}]})
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in params]})
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {"name": template_name, "language": {"code": self.lang}, "components": components},
        }
        try:
            resp = self.client.post(self.url, json=payload)
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"network error: {e}", status="failed")
        if resp.status_code >= 400:
            return SendResult(ok=False, error=_error_text(resp), status="failed")
        msg_id = resp.json().get("messages", [{}])[0].get("id")
        return SendResult(ok=True, message_id=msg_id, status="sent")


@lru_cache(maxsize=1)
def _meta_provider(token: str, phone_number_id: str, lang: str) -> MetaCloudProvider:
    # one shared HTTP client (connection pool) for the whole app, instead of a new one per send
    return MetaCloudProvider(token, phone_number_id, lang)


class TestGroupProvider:
    """For the first live days: real WhatsApp messages only to a few chosen numbers.

    Everyone else is handled exactly like test mode: the message is written and listed, but not sent.
    """

    def __init__(self, real: WhatsAppProvider, phones: set[str]):
        self.real, self.phones, self.dry = real, phones, DryRunProvider()
        self.name = real.name

    def send_template(self, to: str, template_name: str, params: list[str], media: MediaFile | None = None) -> SendResult:
        target = self.real if to in self.phones else self.dry
        return target.send_template(to, template_name, params, media=media)


def get_provider(db=None) -> WhatsAppProvider:
    """The sender to use. Pass the database session so the owner's "test group only" setting applies."""
    if config.WHATSAPP_PROVIDER != "meta":
        return DryRunProvider()
    real = _meta_provider(config.META_WA_TOKEN, config.META_WA_PHONE_NUMBER_ID, config.META_WA_TEMPLATE_LANG)
    if db is not None:
        from app import settings

        if settings.get_bool(db, "test_group_only"):
            return TestGroupProvider(real, settings.test_group_phones(db))
    return real
