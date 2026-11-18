"""Webhook signatures, production config checks, settings and the business clock."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from app import clock, config, settings
from app.models import InboundMessage
from app.routes.webhooks import signature_ok

SECRET = "app-secret-123"
BODY = json.dumps({"entry": [{"changes": [{"value": {"messages": [
    {"from": "919800000009", "type": "text", "text": {"body": "hello"}}
]}}]}]}).encode()
JSON = {"Content-Type": "application/json"}


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_ok():
    assert signature_ok(BODY, _sign(BODY), SECRET)
    assert not signature_ok(BODY, _sign(BODY, "other"), SECRET)
    assert not signature_ok(BODY + b" ", _sign(BODY), SECRET)
    assert not signature_ok(BODY, None, SECRET)
    assert not signature_ok(BODY, _sign(BODY).removeprefix("sha256="), SECRET)


def test_webhook_accepts_valid_signature(client, db, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", SECRET)
    r = client.post("/webhooks/whatsapp", content=BODY, headers=JSON | {"X-Hub-Signature-256": _sign(BODY)})
    assert r.status_code == 200
    assert db.query(InboundMessage).count() == 1


@pytest.mark.parametrize("header", [None, "sha256=deadbeef", _sign(BODY, "wrong-secret")])
def test_webhook_rejects_bad_signature(client, db, monkeypatch, header):
    monkeypatch.setattr(config, "META_APP_SECRET", SECRET)
    headers = JSON | ({"X-Hub-Signature-256": header} if header else {})
    assert client.post("/webhooks/whatsapp", content=BODY, headers=headers).status_code == 401
    assert db.query(InboundMessage).count() == 0


def test_webhook_without_secret_refused_in_production(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    assert client.post("/webhooks/whatsapp", content=BODY, headers=JSON).status_code == 503


def test_webhook_without_secret_allowed_in_development(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    assert client.post("/webhooks/whatsapp", content=BODY, headers=JSON).status_code == 200


def test_webhook_rejects_invalid_json(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", SECRET)
    body = b"not json"
    assert client.post("/webhooks/whatsapp", content=body, headers=JSON | {"X-Hub-Signature-256": _sign(body)}).status_code == 400


def test_webhook_verify_handshake(client, monkeypatch):
    monkeypatch.setattr(config, "META_WA_VERIFY_TOKEN", "my-verify")
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "my-verify", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def _strong(monkeypatch):
    for name, value in {
        "ADMIN_PASSWORD": "a-strong-password", "SESSION_SECRET": "s" * 40, "TICK_TOKEN": "t" * 30,
        "DATABASE_URL": "postgresql+psycopg://u:p@host/db", "WHATSAPP_PROVIDER": "meta",
        "META_APP_SECRET": SECRET, "META_WA_VERIFY_TOKEN": "my-verify-token",
    }.items():
        monkeypatch.setattr(config, name, value)


def test_production_config_ok_when_strong(monkeypatch):
    _strong(monkeypatch)
    assert config.production_problems() == []


@pytest.mark.parametrize("name,value,fragment", [
    ("ADMIN_PASSWORD", "change-me", "ADMIN_PASSWORD"),
    ("SESSION_SECRET", "dev-secret-change-me", "SESSION_SECRET"),
    ("TICK_TOKEN", "", "TICK_TOKEN"),
    ("DATABASE_URL", "sqlite:///./reminders.db", "Postgres"),
    ("META_APP_SECRET", "", "META_APP_SECRET"),
    ("META_WA_VERIFY_TOKEN", "change-me", "META_WA_VERIFY_TOKEN"),
])
def test_production_config_flags_weak_values(monkeypatch, name, value, fragment):
    _strong(monkeypatch)
    monkeypatch.setattr(config, name, value)
    problems = config.production_problems()
    assert len(problems) == 1 and fragment in problems[0]


def test_normalize_db_url():
    assert config.normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert config.normalize_db_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert config.normalize_db_url("sqlite:///x.db") == "sqlite:///x.db"


def test_settings_defaults_and_overrides(db):
    assert settings.get_int(db, "reminder_run_hour") == config.REMINDER_RUN_HOUR
    assert settings.get_bool(db, "sending_paused") is False
    settings.set_value(db, "sending_paused", True)
    settings.set_value(db, "reminder_run_hour", 11)
    assert settings.get_bool(db, "sending_paused") is True
    assert settings.get_int(db, "reminder_run_hour") == 11
    with pytest.raises(KeyError):
        settings.get(db, "no_such_setting")


def test_clock_is_india_time_regardless_of_server(monkeypatch):
    fixed = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)  # 20:00 UTC = 01:30 next day in India

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", FakeDatetime)
    assert clock.now() == datetime(2026, 11, 11, 1, 30)
    assert str(clock.today()) == "2026-11-11"
