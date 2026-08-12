"""Test fixtures.

Unit tests are pure and need no database. Integration tests run against a real
PostgreSQL database - the same engine the application targets - because the
schema uses JSONB, native UUID and ON CONFLICT, none of which a substitute
engine would exercise faithfully. There is no SQLite fallback anywhere in this
project, including its tests.

Point `TEST_DATABASE_URL` (or the usual POSTGRES_* variables) at a disposable
database; integration tests skip cleanly when none is reachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings, reset_settings_cache
from app.db.models import Base
from app.db.session import dispose_engine, set_session_factory

SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "seed")


def _test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    settings = get_settings()
    # Default to a `<database>_test` sibling so a stray test run can never
    # truncate the development data. Derived from the configured name rather
    # than hard-coded, so renaming the database does not silently point the
    # suite back at the real one.
    test_db = f"{settings.postgres_db}_test"
    url = settings.sqlalchemy_url
    for name in (f"/{settings.postgres_db}?", f"/{settings.postgres_db}"):
        if name in url:
            return url.replace(name, name.replace(settings.postgres_db, test_db), 1)
    return url


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = _test_database_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL is not reachable for integration tests: {exc}")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="session")
def seeded_engine(db_engine: Engine) -> Engine:
    """Engine whose database contains the deterministic seed corpus."""
    from scripts.seed_database import seed

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        seed(session)
        session.commit()
    return db_engine


@pytest.fixture
def db_session(seeded_engine: Engine) -> Iterator[Session]:
    """A session bound to the seeded database, rolled back after each test."""
    connection = seeded_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def app_session_factory(seeded_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Point the application's global session factory at the test database.

    Needed by anything that opens its own session internally - the MCP tools,
    the workflow's persistence node and the FastAPI dependency.
    """
    factory = sessionmaker(bind=seeded_engine, autoflush=False, expire_on_commit=False)
    set_session_factory(factory)
    yield factory
    dispose_engine()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Deterministic settings: demo mode on, no LLM, MCP in-memory."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("MCP_CLIENT_MODE", "inmemory")
    reset_settings_cache()
    yield get_settings()
    reset_settings_cache()
