"""One-shot migration from a local SQLite snapshot → live Postgres.

Designed to run **inside** the imin-bot container on the VPS so all
required drivers (aiosqlite + asyncpg) are already installed.

Usage (executed via docker exec):
  SOURCE_SQLITE=/tmp/social.db \
  python scripts/migrate_sqlite_to_postgres.py

The script:
  1. Connects to the source SQLite file (sync, sqlalchemy + sqlite3 stdlib).
  2. Connects to the target Postgres using the same DATABASE_URL
     env var the bot itself uses (asyncpg).
  3. For each ORM model in db/models.py copies all rows preserving PK ids.
  4. Bumps Postgres sequences past the imported max(id).
  5. Refuses to run if any target table already has rows (safety).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import Session

from config.settings import settings
from db.models import (
    Post,
    Publication,
    Message,
    RSSSource,
    ReactionSnapshot,
    TokenStore,
    KVStore,
    DailyStats,
    GeoResearchTask,
)

SOURCE = os.environ.get("SOURCE_SQLITE", "/data/migrate-source.db")
TARGET = settings.database_url
if not TARGET.startswith("postgresql"):
    sys.exit(f"Target must be Postgres, got: {TARGET}")

# Migrate models in dependency order (parents first so FK rows resolve)
MODELS = [
    Post,                # Publication.post_id → posts.id
    Publication,
    Message,
    RSSSource,
    ReactionSnapshot,
    TokenStore,
    KVStore,
    DailyStats,
    GeoResearchTask,
]


def model_columns(model):
    return [c.name for c in model.__table__.columns]


async def main():
    print(f"Source : {SOURCE}")
    print(f"Target : {TARGET.split('@')[-1]}")

    sqlite_engine = create_engine(f"sqlite:///{SOURCE}")
    pg_engine = create_async_engine(TARGET, future=True)

    # Phase 1 — read everything from SQLite (sync, fast)
    snapshots: dict[str, list[dict]] = {}
    with Session(sqlite_engine) as src:
        for model in MODELS:
            cols = model_columns(model)
            rows = src.execute(select(model)).scalars().all()
            snapshots[model.__tablename__] = [
                {c: getattr(r, c) for c in cols} for r in rows
            ]
            print(f"  read  {model.__tablename__:24s} {len(rows):>6d}")

    # Phase 2 — write into Postgres (async, with safety check)
    async with AsyncSession(pg_engine) as dst:
        for model in MODELS:
            res = await dst.execute(
                text(f"SELECT COUNT(*) FROM {model.__tablename__}")
            )
            n = res.scalar_one()
            if n:
                sys.exit(
                    f"REFUSING: {model.__tablename__} already has {n} rows; truncate first"
                )

        total = 0
        for model in MODELS:
            data = snapshots[model.__tablename__]
            if not data:
                print(f"  copy  {model.__tablename__:24s} (empty)")
                continue
            await dst.execute(model.__table__.insert(), data)
            await dst.commit()
            total += len(data)
            print(f"  copy  {model.__tablename__:24s} {len(data):>6d}")

        print(f"Imported {total} rows total.")

        # Phase 3 — bump sequences past max(id)
        for model in MODELS:
            cols = model.__table__.columns
            if "id" not in cols:
                continue
            id_col = cols["id"]
            if not id_col.primary_key:
                continue
            if str(id_col.type).lower() not in ("integer", "biginteger", "smallinteger"):
                continue
            tbl = model.__tablename__
            seq = f"{tbl}_id_seq"
            try:
                await dst.execute(
                    text(
                        f"SELECT setval('{seq}', "
                        f"GREATEST(1, (SELECT COALESCE(MAX(id),0) FROM {tbl})))"
                    )
                )
                await dst.commit()
                print(f"  seq   {seq}")
            except Exception as exc:
                print(f"  WARN: bump seq for {tbl} failed: {exc}")

    await pg_engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
