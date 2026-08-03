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
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]

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
        # Incremental migrations — idempotent, safe on every startup.
        migrations = [
            "ALTER TABLE bots ADD COLUMN IF NOT EXISTS bot_type VARCHAR(32) NOT NULL DEFAULT 'filestore';",
            # Cricket tables are created by create_all above; these are safety guards
            # for existing deployments upgrading without a full recreate.
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'cricket_tours'
                ) THEN
                    CREATE TABLE cricket_tours (
                        id SERIAL PRIMARY KEY,
                        bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                        name VARCHAR(255) NOT NULL,
                        details TEXT,
                        prize_pool VARCHAR(128),
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                END IF;
            END $$;
            """,
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'cricket_players'
                ) THEN
                    CREATE TABLE cricket_players (
                        id SERIAL PRIMARY KEY,
                        bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                        tour_id INTEGER REFERENCES cricket_tours(id) ON DELETE SET NULL,
                        user_id BIGINT NOT NULL,
                        username VARCHAR(64),
                        full_name VARCHAR(128),
                        role VARCHAR(64),
                        is_captain BOOLEAN NOT NULL DEFAULT FALSE,
                        base_price VARCHAR(64),
                        status VARCHAR(16) NOT NULL DEFAULT 'pending',
                        answers TEXT,
                        registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX ON cricket_players(bot_id);
                    CREATE INDEX ON cricket_players(user_id);
                END IF;
            END $$;
            """,
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'cricket_questions'
                ) THEN
                    CREATE TABLE cricket_questions (
                        id SERIAL PRIMARY KEY,
                        bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                        key VARCHAR(64) NOT NULL,
                        label VARCHAR(256) NOT NULL,
                        input_type VARCHAR(16) NOT NULL DEFAULT 'text',
                        choices TEXT,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        required BOOLEAN NOT NULL DEFAULT FALSE,
                        order_index INTEGER NOT NULL DEFAULT 0
                    );
                END IF;
            END $$;
            """,
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'cricket_settings'
                ) THEN
                    CREATE TABLE cricket_settings (
                        bot_id INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
                        auto_approve BOOLEAN NOT NULL DEFAULT FALSE,
                        allow_captain_reg BOOLEAN NOT NULL DEFAULT TRUE,
                        max_players INTEGER NOT NULL DEFAULT 0,
                        max_captains INTEGER NOT NULL DEFAULT 0,
                        reg_end_date TIMESTAMPTZ
                    );
                END IF;
            END $$;
            """,
            # New columns added to cricket_settings
            "ALTER TABLE cricket_settings ADD COLUMN IF NOT EXISTS admin_gc BIGINT;",
            "ALTER TABLE cricket_settings ADD COLUMN IF NOT EXISTS welcome_image_disabled BOOLEAN NOT NULL DEFAULT FALSE;",
        ]
        for sql in migrations:
            await conn.execute(text(sql))
