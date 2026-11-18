from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app import config

ROOT = Path(__file__).resolve().parent.parent


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    url = config.normalize_db_url(url)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Neon's free tier suspends idle databases; pre-ping replaces connections that died meanwhile.
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=2)


engine = make_engine(config.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None):
    """Create all tables directly from the models. Used by tests; the app itself uses migrate()."""
    from app import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(bind=bind or engine)


def alembic_config(url: str | None = None):
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.normalize_db_url(url or config.DATABASE_URL).replace("%", "%%"))
    return cfg


def migrate(url: str | None = None) -> None:
    """Bring the database schema up to date (run on every start).

    A database created by the old create_all() code has tables but no alembic_version: it is
    topped up with any missing tables and stamped, so later migrations apply normally.
    """
    from alembic import command

    from app import models  # noqa: F401

    cfg = alembic_config(url)
    eng = engine if url is None else make_engine(url)
    tables = set(inspect(eng).get_table_names())
    if "customers" in tables and "alembic_version" not in tables:
        Base.metadata.create_all(bind=eng)
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
    if url is not None:
        eng.dispose()
