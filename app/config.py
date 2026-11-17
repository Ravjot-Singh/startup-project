import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./reminders.db")

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "ABC Frozen Foods")
BUSINESS_UPI_ID = os.getenv("BUSINESS_UPI_ID", "")

REMINDER_RUN_HOUR = _int("REMINDER_RUN_HOUR", 10)
SEND_WINDOW_START = _int("SEND_WINDOW_START", 9)
SEND_WINDOW_END = _int("SEND_WINDOW_END", 20)

WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "dryrun")
META_WA_TOKEN = os.getenv("META_WA_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "")
META_WA_TEMPLATE_LANG = os.getenv("META_WA_TEMPLATE_LANG", "en")
META_WA_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "change-me")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me")
