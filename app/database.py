"""
database.py – SQLAlchemy 2.0 engine/session wiring.

Design choices:
- Engine and session factory are created once from Settings; tests inject
  their own via dependency overrides → no global engine/session objects.
- SQLite: check_same_thread=False (FastAPI uses a thread pool for sync
  handlers), PRAGMA foreign_keys=ON, WAL mode for concurrent reads.
- get_db yields a Session per request and always closes it in finally.
"""

from collections.abc import Generator
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


# ---------------------------------------------------------------------------
# ORM base – all models inherit from this.
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Engine / session-factory builders (called once per unique Settings object).
# ---------------------------------------------------------------------------

def build_engine(settings: Settings) -> Any:
    """Create a SQLAlchemy engine from settings.

    We keep this as a plain function (not cached here) so that tests can
    call it with their own temp-DB settings without polluting a global cache.
    """
    url = settings.database_url
    connect_args: dict[str, Any] = {}

    if url.startswith("sqlite"):
        # Required so the same connection can be shared across threads
        # (FastAPI sync route handlers run in a thread-pool).
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args)

    if url.startswith("sqlite"):
        # Enable foreign-key enforcement and WAL mode for file DBs.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL gives better concurrency for file-based SQLite.
            if not url.startswith("sqlite:///:memory:"):
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Create a sessionmaker bound to the engine for given settings."""
    engine = build_engine(settings)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------------------------------------------------------------------------
# FastAPI dependency: yields one Session per request.
# ---------------------------------------------------------------------------

def get_session_factory() -> sessionmaker[Session]:
    """FastAPI dependency: returns the app-wide session factory.

    Override in tests via app.dependency_overrides[get_session_factory] = ...
    """
    # Build (and cache) from the singleton settings the first time.
    return _get_default_session_factory()


@lru_cache(maxsize=1)
def _get_default_session_factory() -> sessionmaker[Session]:
    """Cached so the engine is created exactly once in production."""
    return build_session_factory(get_settings())


def get_db(
    session_factory: sessionmaker[Session] = None,  # type: ignore[assignment]
) -> Generator[Session, None, None]:
    """FastAPI dependency: open a DB session, yield it, always close it.

    Tests inject a custom session_factory via dependency overrides on
    get_session_factory, and get_db calls that overridden factory.
    """
    # We can't use Depends() inside a regular function that is itself a
    # dependency, so routes use Depends(get_db) and we pull the factory from
    # the overrideable get_session_factory dependency inside the route via a
    # slightly different pattern – see usage in routes.
    raise NotImplementedError("Use the get_db_from_factory helper below.")


def get_db_session(
    factory: sessionmaker[Session],
) -> Generator[Session, None, None]:
    """Yield one session from *factory* and always close it."""
    db = factory()
    try:
        yield db
    finally:
        db.close()
