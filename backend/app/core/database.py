"""SQLAlchemy engine, session factory, and declarative base.

Uses SQLAlchemy 2.0 style. The engine URL comes from settings, so tests can
override it with an in-memory SQLite database.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def connection_ceiling() -> int:
    """The most connections one process may hold at once.

    Read by ``app.main`` to bound the request threadpool. Handlers and
    connections are two halves of the same limit — a request that is admitted
    and then cannot reach the database is worse than one that waited — and
    keeping the number in one place is what stops them drifting apart.
    """
    return settings.db_pool_size + settings.db_max_overflow


def _make_engine(url: str):
    # SQLite (tests) needs a special connect arg for multithreaded access, and
    # takes none of the pool sizing below: an in-memory database is served by a
    # single-connection pool that has no notion of overflow.
    if url.startswith("sqlite"):
        return create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args={"check_same_thread": False},
        )
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        # See `settings.db_pool_size` for why the defaults were not survivable.
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle_seconds,
    )


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
