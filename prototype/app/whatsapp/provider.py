"""WhatsApp sending behind one interface so the provider can be swapped (Meta Cloud API, Interakt, AiSensy ...)."""

import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from app import config


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    status: str = "sent"


class WhatsAppProvider(Protocol):
    name: str

    def send_template(self, to: str, template_name: str, params: list[str]) -> SendResult: ...


class DryRunProvider:
    """Sends nothing. Use while testing with real data so the distributor can review messages first."""

    name = "dryrun"

    def __init__(self):
        self.sent: list[tuple[str, str, list[str]]] = []

    def send_template(self, to: str, template_name: str, params: list[str]) -> SendResult:
        self.sent.append((to, template_name, params))
        return SendResult(ok=True, message_id=f"dryrun-{uuid.uuid4().hex[:12]}", status="dryrun")


class MetaCloudProvider:
    """Meta WhatsApp Cloud API: https://developers.facebook.com/docs/whatsapp/cloud-api"""

    name = "meta"
    API_VERSION = "v21.0"

    def __init__(self, token: str, phone_number_id: str, lang: str = "en", client: httpx.Client | None = None):
        if not token or not phone_number_id:
            raise ValueError("META_WA_TOKEN and META_WA_PHONE_NUMBER_ID must be set to use the meta provider")
        self.url = f"https://graph.facebook.com/{self.API_VERSION}/{phone_number_id}/messages"
        self.lang = lang
        self.client = client or httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"})

    def send_template(self, to: str, template_name: str, params: list[str]) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": self.lang},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]},
                ],
            },
        }
        try:
            resp = self.client.post(self.url, json=payload)
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"network error: {e}", status="failed")
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code')}: {err.get('message')}"
            except ValueError:
                msg = resp.text[:300]
            return SendResult(ok=False, error=msg, status="failed")
        msg_id = resp.json().get("messages", [{}])[0].get("id")
        return SendResult(ok=True, message_id=msg_id, status="sent")


def get_provider() -> WhatsAppProvider:
    if config.WHATSAPP_PROVIDER == "meta":
        return MetaCloudProvider(config.META_WA_TOKEN, config.META_WA_PHONE_NUMBER_ID, config.META_WA_TEMPLATE_LANG)
    return DryRunProvider()
