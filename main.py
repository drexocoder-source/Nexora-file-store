"""Nexora File Store — entrypoint.

Starts the main bot (Nexora File Store) plus a Kurigram Client for every
already-registered clone bot, and keeps the process alive.
"""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client
from sqlalchemy import select

from bot_manager import manager
from clonebot.handlers import register_clone_handlers
from config import settings
from database.engine import AsyncSessionLocal, init_db
from database.models import Bot as BotModel
from mainbot.handlers import register_main_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

log = logging.getLogger("nexora.main")


async def start_existing_clones() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotModel).where(BotModel.active.is_(True)))
        bots = result.scalars().all()

    for bot_row in bots:
        try:
            await manager.start_clone(bot_row.id, bot_row.bot_token, register_clone_handlers)
        except Exception:
            log.exception("Failed to start clone bot %s (@%s)", bot_row.id, bot_row.bot_username)


async def main() -> None:
    log.info("Initializing database schema...")
    await init_db()

    main_app = Client(
        name="nexora_main",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,
    )
    register_main_handlers(main_app)

    await main_app.start()
    me = await main_app.get_me()
    log.info("Nexora File Store main bot started as @%s", me.username)

    await start_existing_clones()
    log.info("All systems running. Listening for updates...")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
