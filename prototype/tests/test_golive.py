"""Phase 7 (go-live): configuration, the test group, the tick lock, the setup check and the Help page."""

import httpx
import pytest
import respx

from app import check, config, settings
from app.whatsapp import provider as wa
from app.whatsapp.provider import DryRunProvider, SendResult, get_provider
from app.whatsapp.provider import TestGroupProvider as GroupOnly  # alias: pytest would try to collect Test*


# ---------- configuration ----------

@pytest.mark.parametrize("raw", [None, "", "   ", '""'])
def test_empty_database_url_means_local_sqlite(raw):
    assert config.resolve_database_url(raw) == config.LOCAL_SQLITE_URL


def test_neon_url_gets_the_psycopg_driver():
    url = config.resolve_database_url(" postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require ")
    assert url == "postgresql+psycopg://u:p@ep-x.neon.tech/neondb?sslmode=require"


def test_local_timer_is_off_when_a_copy_points_at_shared_postgres():
    assert config.default_local_scheduler(is_production=False, uses_sqlite=True) is True
    assert config.default_local_scheduler(is_production=False, uses_sqlite=False) is False
    assert config.default_local_scheduler(is_production=True, uses_sqlite=True) is False


def test_env_example_lists_every_setting_the_app_reads():
    names = {line.split("=", 1)[0].lstrip("# ").strip() for line in
             (config.PROJECT_DIR / ".env.example").read_text(encoding="utf-8").splitlines() if "=" in line and not line.startswith("#   ")}
    for name in ["APP_ENV", "TIMEZONE", "DATABASE_URL", "ADMIN_USERNAME", "ADMIN_PASSWORD", "SESSION_SECRET", "BUSINESS_NAME",
                 "BUSINESS_UPI_ID", "TICK_TOKEN", "LOCAL_SCHEDULER", "WHATSAPP_PROVIDER", "META_WA_TOKEN",
                 "META_WA_PHONE_NUMBER_ID", "META_WA_TEMPLATE_LANG", "META_WA_VERIFY_TOKEN", "META_APP_SECRET"]:
        assert name in names, name


# ---------- test group ----------

class RecordingProvider:
    name = "meta"

    def __init__(self):
        self.sent = []

    def send_template(self, to, template_name, params, media=None):
        self.sent.append(to)
        return SendResult(ok=True, message_id="wamid.1", status="sent")


def test_test_group_sends_real_messages_only_to_its_numbers():
    real = RecordingProvider()
    p = GroupOnly(real, {"919800000001"})
    assert p.send_template("919800000001", "t", []).status == "sent"
    assert p.send_template("919800000002", "t", []).status == "dryrun"
    assert real.sent == ["919800000001"]


@pytest.fixture
def meta_on(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(config, "META_WA_TOKEN", "token")
    monkeypatch.setattr(config, "META_WA_PHONE_NUMBER_ID", "123")
    wa._meta_provider.cache_clear()
    yield
    wa._meta_provider.cache_clear()


def test_get_provider_applies_the_owner_setting(db, meta_on):
    assert isinstance(get_provider(db), wa.MetaCloudProvider)
    settings.set_value(db, "test_group_only", True)
    settings.set_value(db, "test_group_phones", "919800000001")
    p = get_provider(db)
    assert isinstance(p, GroupOnly) and p.phones == {"919800000001"}


def test_test_mode_ignores_the_test_group(db):
    settings.set_value(db, "test_group_only", True)
    assert isinstance(get_provider(db), DryRunProvider)


def test_test_group_settings_form(logged_in, db, monkeypatch):
    r = logged_in.post("/settings/test-group", data={"csrf_token": logged_in.csrf, "mode": "group",
                                                     "phones": "98000 00001, +91 98000-00002\n98000 00001"}, follow_redirects=True)
    assert "Real messages go only to 2 test numbers." in r.text
    assert settings.test_group_phones(db) == {"919800000001", "919800000002"}
    assert settings.get_bool(db, "test_group_only")

    r = logged_in.post("/settings/test-group", data={"csrf_token": logged_in.csrf, "mode": "group", "phones": "12345"},
                       follow_redirects=True)
    assert "Not a mobile number: 12345" in r.text
    assert settings.test_group_phones(db) == {"919800000001", "919800000002"}  # unchanged

    r = logged_in.post("/settings/test-group", data={"csrf_token": logged_in.csrf, "mode": "group", "phones": ""},
                       follow_redirects=True)
    assert "Add at least one number" in r.text

    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "meta")
    assert "Test group only" in logged_in.get("/settings").text
    logged_in.post("/settings/test-group", data={"csrf_token": logged_in.csrf, "mode": "everyone", "phones": "9800000001"})
    assert not settings.get_bool(db, "test_group_only")
    page = logged_in.get("/settings").text
    assert "Test group only" not in page and '<span class="badge ok">Everyone</span>' in page


# ---------- tick lock ----------

def test_overlapping_ticks_on_postgres_do_not_both_run(db):
    from sqlalchemy import text

    from app.jobs import TICK_LOCK_KEY, run_tick

    engine = db.get_bind()
    if engine.dialect.name != "postgresql":
        pytest.skip("advisory locks are Postgres only")
    with engine.connect() as other:
        other.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": TICK_LOCK_KEY})
        assert run_tick(db, DryRunProvider())["reminders"] == "another tick is running"
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK_KEY})
        other.commit()
    assert run_tick(db, DryRunProvider())["reminders"] != "another tick is running"


def test_tick_runs_normally_on_sqlite(db):
    from app.jobs import run_tick

    if db.get_bind().dialect.name == "postgresql":
        pytest.skip("covered above")
    assert "reminders" in run_tick(db, DryRunProvider())


# ---------- python -m app.check ----------

def test_database_label_never_shows_the_password():
    label = check.database_label("postgresql+psycopg://user:secret-pw@ep-x.neon.tech/neondb?sslmode=require")
    assert label == "Postgres at ep-x.neon.tech (database neondb)" and "secret-pw" not in label


def test_check_reports_a_bad_database_without_crashing(tmp_path):
    report = check.Report()
    check.check_database(report, "postgresql+psycopg://u:p@127.0.0.1:1/nothing?connect_timeout=2")
    assert report.failed and any("Can't connect" in line for _, line in report.lines)


def test_check_on_a_fresh_sqlite_file(tmp_path):
    report = check.Report()
    check.check_database(report, f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert not report.failed and any("empty" in line for _, line in report.lines)


@respx.mock
def test_check_whatsapp_reads_the_number_from_meta(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(config, "META_WA_TOKEN", "token")
    monkeypatch.setattr(config, "META_WA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "META_APP_SECRET", "secret")
    respx.get(url__regex=r"https://graph\.facebook\.com/.*/123").mock(return_value=httpx.Response(
        200, json={"verified_name": "ABC Frozen Foods", "display_phone_number": "+91 98000 00000", "quality_rating": "GREEN"}))
    report = check.Report()
    check.check_whatsapp(report)
    assert not report.failed and "connected as ABC Frozen Foods" in report.lines[-1][1]

    respx.get(url__regex=r"https://graph\.facebook\.com/.*/123").mock(return_value=httpx.Response(
        401, json={"error": {"message": "Invalid OAuth access token"}}))
    report = check.Report()
    check.check_whatsapp(report)
    assert report.failed and "Invalid OAuth access token" in report.lines[-1][1]


def test_check_whatsapp_lists_missing_values(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(config, "META_WA_TOKEN", "")
    monkeypatch.setattr(config, "META_WA_PHONE_NUMBER_ID", "")
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    report = check.Report()
    check.check_whatsapp(report)
    assert report.failed and "META_WA_TOKEN" in report.lines[-1][1]


# ---------- Help page ----------

def test_help_page_shows_the_owner_guide(logged_in):
    page = logged_in.get("/help").text
    assert "<h1>How to use the app</h1>" in page
    assert "Your daily five minutes" in page and "<table>" in page


def test_help_needs_login(client):
    assert client.get("/help").status_code == 303
