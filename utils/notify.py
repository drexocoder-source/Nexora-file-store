"""Shared utility: send activity notifications to the main owner's DM."""
from __future__ import annotations

import logging

from pyrogram import Client
from pyrogram.errors import RPCError

log = logging.getLogger("nexora.notify")

_main_client: Client | None = None


def set_main_client(client: Client) -> None:
    """Call once after the main bot client starts."""
    global _main_client
    _main_client = client


async def notify_owner(text: str) -> None:
    """Send a DM to the main owner from the main bot. Silently swallows errors."""
    from config import settings

    if _main_client is None:
        return
    try:
        await _main_client.send_message(
            settings.main_owner_id,
            text,
            disable_web_page_preview=True,
        )
    except RPCError:
        log.debug("notify_owner: failed to DM owner — %s", text[:60])
