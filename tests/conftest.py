import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, init_db
from app.seed import seed_rules


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    seed_rules(session)
    yield session
    session.close()
    Base.metadata.drop_all(engine)
