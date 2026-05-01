from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config.settings import settings
from db.models import Base


_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args: dict = {"timeout": 30} if _is_sqlite else {}

# Production lesson (2026-05-01): without pool_pre_ping the bot would
# happily try to reuse a Postgres connection that the server had
# already closed (idle timeout / Docker network reset / etc.). The
# next .commit() raised `asyncpg.InterfaceError: connection is closed`,
# the session rolled back, and Publication.status stayed QUEUED.
# Result: the publisher kept re-trying the same already-rejected post
# every 15 min, blocking the rest of the queue for hours.
#
# pool_pre_ping=True issues a cheap SELECT 1 before handing out a
# pooled connection; pool_recycle=300s recycles connections older
# than 5 minutes proactively. SQLite has no pool so we skip both.
_engine_kwargs: dict = {
    "echo": False,
    "connect_args": _connect_args,
}
if not _is_sqlite:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 300

engine = create_async_engine(settings.database_url, **_engine_kwargs)


if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        """Enable WAL mode and busy timeout for SQLite to avoid 'database is locked'."""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)


async def _run_migrations(conn) -> None:
    """Add columns that may be missing from existing tables.

    Backend-aware: PostgreSQL gets `ADD COLUMN IF NOT EXISTS` in a single
    statement; SQLite (no IF NOT EXISTS for ADD COLUMN) falls back to
    one-statement-per-savepoint to swallow "duplicate column" errors.
    """
    _alters = [
        ("posts", "latitude", "FLOAT"),
        ("posts", "longitude", "FLOAT"),
        ("posts", "place_name", "VARCHAR(500)"),
        ("posts", "translations", "TEXT"),
        ("posts", "source_published_at", "TIMESTAMP"),
        ("posts", "pipeline_log", "TEXT"),
        ("posts", "poi_point_id", "INTEGER"),
        ("posts", "backend_event_id", "INTEGER"),
        ("posts", "ticket_url", "VARCHAR(2000)"),
        ("messages", "thread_id", "VARCHAR(500)"),
        ("messages", "view_count", "INTEGER DEFAULT 0"),
    ]
    if _is_sqlite:
        for table, col, col_type in _alters:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
    else:
        for table, col, col_type in _alters:
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
            ))


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
