"""The exact requests sent to the Meta WhatsApp Cloud API (mocked with respx; nothing leaves the machine)."""

import json

import httpx
import pytest
import respx

from app import config
from app.whatsapp import provider as prov
from app.whatsapp.provider import MediaFile, MetaCloudProvider

BASE = "https://graph.facebook.com/v21.0/PHONE123"


@pytest.fixture
def meta():
    with respx.mock(assert_all_called=False) as router:
        client = httpx.Client(headers={"Authorization": "Bearer TOKEN"})
        yield MetaCloudProvider("TOKEN", "PHONE123", "en", client=client), router


def _ok_send(router):
    return router.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.XYZ"}]}))


def test_text_template_payload(meta):
    p, router = meta
    send = _ok_send(router)
    res = p.send_template("919800000001", "payment_overdue", ["Sharma", "ABC"])
    assert res.ok and res.message_id == "wamid.XYZ"
    body = json.loads(send.calls.last.request.content)
    assert body == {
        "messaging_product": "whatsapp", "to": "919800000001", "type": "template",
        "template": {"name": "payment_overdue", "language": {"code": "en"},
                     "components": [{"type": "body", "parameters": [{"type": "text", "text": "Sharma"}, {"type": "text", "text": "ABC"}]}]},
    }
    assert send.calls.last.request.headers["Authorization"] == "Bearer TOKEN"


def test_document_header_uploads_then_sends(meta):
    p, router = meta
    upload = router.post(f"{BASE}/media").mock(return_value=httpx.Response(200, json={"id": "MEDIA42"}))
    send = _ok_send(router)
    res = p.send_template("919800000001", "bill_document", ["A"], media=MediaFile(b"%PDF-1.4 x", "application/pdf", "Bill-INV1.pdf"))
    assert res.ok
    up = upload.calls.last.request
    assert b'name="messaging_product"' in up.content and b"whatsapp" in up.content
    assert b'name="type"' in up.content and b"application/pdf" in up.content
    assert b'filename="Bill-INV1.pdf"' in up.content and b"%PDF-1.4 x" in up.content
    components = json.loads(send.calls.last.request.content)["template"]["components"]
    assert components[0] == {"type": "header", "parameters": [
        {"type": "document", "document": {"id": "MEDIA42", "filename": "Bill-INV1.pdf"}}]}
    assert components[1]["type"] == "body"


def test_image_header(meta):
    p, router = meta
    router.post(f"{BASE}/media").mock(return_value=httpx.Response(200, json={"id": "IMG7"}))
    send = _ok_send(router)
    p.send_template("919800000001", "offer_image", ["A"], media=MediaFile(b"\x89PNG", "image/png", "offer.png"))
    header = json.loads(send.calls.last.request.content)["template"]["components"][0]
    assert header == {"type": "header", "parameters": [{"type": "image", "image": {"id": "IMG7"}}]}


def test_upload_failure_means_nothing_is_sent(meta):
    p, router = meta
    router.post(f"{BASE}/media").mock(return_value=httpx.Response(400, json={"error": {"code": 131053, "message": "Media upload error"}}))
    send = _ok_send(router)
    res = p.send_template("919800000001", "bill_document", ["A"], media=MediaFile(b"%PDF", "application/pdf", "b.pdf"))
    assert not res.ok and res.error == "file upload failed: 131053: Media upload error"
    assert send.call_count == 0


def test_network_error_and_api_error(meta):
    p, router = meta
    router.post(f"{BASE}/messages").mock(side_effect=httpx.ConnectError("no route"))
    res = p.send_template("919800000001", "payment_overdue", ["A"])
    assert not res.ok and res.error.startswith("network error")
    router.post(f"{BASE}/messages").mock(return_value=httpx.Response(400, json={"error": {"code": 132001, "message": "Template does not exist"}}))
    res = p.send_template("919800000001", "payment_overdue", ["A"])
    assert res.error == "132001: Template does not exist"


def test_meta_provider_is_shared_between_calls(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(config, "META_WA_TOKEN", "T")
    monkeypatch.setattr(config, "META_WA_PHONE_NUMBER_ID", "P")
    prov._meta_provider.cache_clear()
    try:
        assert prov.get_provider() is prov.get_provider()
    finally:
        prov._meta_provider.cache_clear()
