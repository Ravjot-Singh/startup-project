"""Shared fixtures.

Tests run on in-memory SQLite by default. Set TEST_DATABASE_URL (e.g. a Postgres URL in CI) to run the
same suite against that database instead; its tables are dropped and recreated for every test.
"""

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db import Base, get_db, init_db, make_engine
from app.seed import seed_rules

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
ADMIN_USER, ADMIN_PASS = "owner", "test-password-123"


def _engine():
    if TEST_DATABASE_URL:
        engine = make_engine(TEST_DATABASE_URL)
        Base.metadata.drop_all(engine)
        return engine
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


@pytest.fixture
def db():
    engine = _engine()
    init_db(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    seed_rules(session)
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient wired to the test database (the app's startup/lifespan is not run)."""
    from app.main import app

    monkeypatch.setattr(config, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "dryrun")
    app.dependency_overrides[get_db] = lambda: db
    # Not used as a context manager on purpose: that would run the app's startup (migrations,
    # scheduler) against the real database.
    c = TestClient(app, follow_redirects=False)
    yield c
    app.dependency_overrides.clear()


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "page has no CSRF token"
    return m.group(1)


@pytest.fixture
def logged_in(client):
    """A client that is logged in. Use client.csrf for POST forms."""
    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"username": ADMIN_USER, "password": ADMIN_PASS, "csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/"
    client.csrf = token
    return client
