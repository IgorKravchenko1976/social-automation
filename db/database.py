from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config.settings import settings
from db.models import Base


engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"timeout": 30},
)


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
    """Add columns that may be missing from existing tables."""
    _alters = [
        ("posts", "latitude", "FLOAT"),
        ("posts", "longitude", "FLOAT"),
        ("posts", "place_name", "VARCHAR(500)"),
        ("posts", "translations", "TEXT"),
        ("posts", "source_published_at", "DATETIME"),
        ("posts", "pipeline_log", "TEXT"),
        ("posts", "poi_point_id", "INTEGER"),
        ("messages", "thread_id", "VARCHAR(500)"),
        ("messages", "view_count", "INTEGER DEFAULT 0"),
    ]
    for table, col, col_type in _alters:
        try:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        except Exception:
            pass


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
