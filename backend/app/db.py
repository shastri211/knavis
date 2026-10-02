"""Database engine and session.

SQLite is the default (a file in the data directory, nothing to install). Set ``DATABASE_URL`` to use PostgreSQL
instead, e.g. ``postgresql://knavis:secret@db:5432/knavis``. The schema is managed by Alembic (``backend/migrations``)
and brought up to date at start-up by ``init_db``.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    """The configured URL, with a driver named so SQLAlchemy uses psycopg 3 for PostgreSQL."""
    url = (settings.database_url or "").strip()
    if not url:
        return f"sqlite:///{(settings.data_dir / 'app.db').resolve()}"
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _make_engine():
    url = database_url()
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


engine = _make_engine()
IS_SQLITE = engine.dialect.name == "sqlite"


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # WAL lets readers and the single writer proceed together; the timeout makes a writer wait
        # for a busy database instead of failing immediately with "database is locked".
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI dependency: one database session per request."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def init_db():
    from . import models  # noqa: F401  (registers every table on Base.metadata)
    from .migrations import upgrade_database
    from .retrieval import fts
    upgrade_database(engine)
    with SessionLocal() as db:
        fts.ensure_table(db)
        fts.rebuild_if_stale(db)   # databases from before the keyword index existed
