"""Database engine and session management.

A single lazily-created engine is shared process-wide. Sessions are short-lived
and always obtained through `session_scope()` (transactional) or `get_session()`
(FastAPI dependency), so no code path leaks a connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create a new engine from settings (not cached)."""
    settings = settings or get_settings()
    logger.info(
        "database.engine.create",
        url=settings.safe_database_url(),
        pool_size=settings.db_pool_size,
    )
    return create_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        echo=settings.db_echo,
        future=True,
    )


def get_engine() -> Engine:
    """Process-wide engine singleton."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


def set_session_factory(factory: sessionmaker[Session]) -> None:
    """Override the factory (used by the test suite)."""
    global _session_factory
    _session_factory = factory


def dispose_engine() -> None:
    """Dispose of the engine and reset the factory."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session() -> Iterator[Session]:
    """Read-only session: never commits."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    with session_scope() as session:
        yield session


def check_database_health() -> tuple[bool, str | None]:
    """Cheap liveness probe used by /health."""
    try:
        with read_session() as session:
            session.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # pragma: no cover - exercised only when DB is down
        logger.warning("database.health.failed", error=str(exc))
        return False, str(exc)
