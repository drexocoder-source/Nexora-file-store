from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings


def _to_async_url(raw: str) -> str:
    """Convert a standard postgres:// URL (e.g. from Neon) into the
    postgresql+asyncpg:// form, stripping query params asyncpg doesn't
    understand (it takes SSL via connect_args instead)."""
    url = raw
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]

    if "?" in url:
        base, _, query = url.partition("?")
        keep = [
            p
            for p in query.split("&")
            if p and not p.startswith("sslmode=") and not p.startswith("channel_binding=")
        ]
        url = base + ("?" + "&".join(keep) if keep else "")
    return url


engine = create_async_engine(
    _to_async_url(settings.database_url),
    pool_pre_ping=True,
    connect_args={"ssl": True},
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Incremental migrations — safe to run on every startup (all are idempotent).
        migrations = [
            # v2: bot type (filestore / linkprotect)
            """
            DO $ BEGIN
                ALTER TABLE bots ADD COLUMN bot_type VARCHAR(32) NOT NULL DEFAULT 'filestore';
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $;
            """,
        ]
        for sql in migrations:
            await conn.execute(text(sql))
