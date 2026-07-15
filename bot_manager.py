"""Starts, tracks, and stops the dynamically created clone bot clients."""
from __future__ import annotations

import logging
from typing import Callable

from pyrogram import Client

from config import settings

log = logging.getLogger("nexora.bot_manager")


class BotManager:
    def __init__(self) -> None:
        self.clients: dict[int, Client] = {}

    async def start_clone(self, bot_id: int, token: str, register: Callable[[Client], None]) -> Client:
        if bot_id in self.clients:
            return self.clients[bot_id]

        app = Client(
            name=f"clone_{bot_id}",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            bot_token=token,
            in_memory=True,
        )
        app.bot_db_id = bot_id  # type: ignore[attr-defined]
        register(app)
        await app.start()
        self.clients[bot_id] = app
        log.info("Started clone bot id=%s", bot_id)
        return app

    async def stop_clone(self, bot_id: int) -> None:
        app = self.clients.pop(bot_id, None)
        if app is not None:
            await app.stop()
            log.info("Stopped clone bot id=%s", bot_id)

    def get(self, bot_id: int) -> Client | None:
        return self.clients.get(bot_id)


manager = BotManager()
